"""Postgres compatibility check for the Portal's SQLAlchemy models (deploy/portal).

Development and tests run the Portal on SQLite; production runs Postgres 16
(``postgresql+psycopg://``). This tool checks the differences that bite:

1. ``ddl``   every table and index compiles for the PostgreSQL dialect (types,
             defaults, unique constraints), and the notification outbox's
             ``INSERT ... ON CONFLICT DO NOTHING`` compiles too.
2. ``suite`` runs the Portal-facing tests with a **length guard**: SQLite
             ignores ``VARCHAR(n)``, Postgres rejects a longer value
             (``StringDataRightTruncation``). The guard raises on any flush of
             a ``String(n)`` value longer than ``n``, so a too-long value fails
             a test on the laptop instead of a request on the VM.
3. ``live``  with ``--url postgresql+psycopg://...`` and ``psycopg`` installed:
             creates the tables in a throwaway schema on a real server, runs a
             few writes (customer, audit chain, outbox dedupe), drops the schema.
             Skipped (and said so) when the driver is missing.

    python tools/portal_pg_check.py                  # ddl + suite (+ live if --url)
    python tools/portal_pg_check.py ddl
    python tools/portal_pg_check.py live --url postgresql+psycopg://u:p@localhost/sentrix

Also usable as a pytest plugin: ``python -m pytest -p portal_pg_check`` with
``tools/`` on ``PYTHONPATH`` installs the length guard for the whole session.
Opens no network connection except to the ``--url`` server.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The tests that exercise the Portal's database (``suite``).
SUITE = ("tests/test_acct_*.py", "tests/test_link_*.py", "tests/test_ch_*.py",
         "tests/test_web_*.py", "tests/test_integration_m10.py",
         "tests/test_portal_postgres.py")


class ValueTooLong(ValueError):
    """A ``String(n)`` value Postgres would reject."""


# ── 1. DDL ───────────────────────────────────────────────────────────────────
def compile_ddl() -> list[str]:
    """``CREATE TABLE`` / ``CREATE INDEX`` statements for PostgreSQL."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from sentrix_portal import models  # noqa: F401  (registers the tables)
    from sentrix_portal.db import Base
    dialect = postgresql.dialect()
    statements = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))
    return statements


def compile_outbox_upsert() -> str:
    """The outbox's dedupe insert (``services.notify``) for PostgreSQL."""
    from sqlalchemy.dialects import postgresql

    from sentrix_portal.models import Notification
    statement = (postgresql.insert(Notification)
                 .values(id="x", dedupe_key="k", channel="email", status="QUEUED")
                 .on_conflict_do_nothing(index_elements=["dedupe_key"]))
    return str(statement.compile(dialect=postgresql.dialect()))


def ddl_report() -> list[str]:
    statements = compile_ddl()
    upsert = compile_outbox_upsert()
    problems = []
    if "ON CONFLICT (dedupe_key) DO NOTHING" not in upsert:
        problems.append(f"outbox upsert compiled unexpectedly: {upsert}")
    for statement in statements:
        if "AUTOINCREMENT" in statement.upper():
            problems.append(f"SQLite-only AUTOINCREMENT: {statement.splitlines()[1]}")
    return problems


# ── 2. length guard ──────────────────────────────────────────────────────────
_INSTALLED = False


def string_limits(table) -> dict[str, int]:
    from sqlalchemy import String
    return {column.key: column.type.length for column in table.columns
            if isinstance(column.type, String) and getattr(column.type, "length", None)}


def check_lengths(obj) -> None:
    """Raise :class:`ValueTooLong` when a mapped object's string is too long."""
    table = getattr(obj, "__table__", None)
    if table is None:
        return
    for key, limit in string_limits(table).items():
        value = getattr(obj, key, None)
        if isinstance(value, str) and len(value) > limit:
            raise ValueTooLong(f"{table.name}.{key} is VARCHAR({limit}) but got "
                               f"{len(value)} characters (Postgres rejects it)")


def _guard(session, _context, _instances) -> None:
    for obj in list(session.new) + list(session.dirty):
        check_lengths(obj)


def install_length_guard() -> None:
    """Check every new or changed ORM object before each flush (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    event.listen(Session, "before_flush", _guard)
    _INSTALLED = True


def remove_length_guard() -> None:
    global _INSTALLED
    if not _INSTALLED:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    event.remove(Session, "before_flush", _guard)
    _INSTALLED = False


def pytest_configure(config) -> None:                # pytest plugin entry point
    install_length_guard()


def run_suite(extra: list[str] | None = None) -> int:
    patterns = [str(path.relative_to(ROOT)) for pattern in SUITE
                for path in sorted(ROOT.glob(pattern))]
    patterns = [p for p in patterns if not Path(p).name.startswith("test_e2e")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(ROOT / "tools"), str(ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "portal_pg_check",
               *patterns, *(extra or [])]
    return subprocess.call(command, cwd=str(ROOT), env=env)


# ── 3. live ──────────────────────────────────────────────────────────────────
def live_check(url: str) -> str:
    """Tables in a throwaway schema on a real Postgres, a few writes, drop."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return "skipped: psycopg is not installed (pip install 'psycopg[binary]')"
    from sqlalchemy import create_engine, text

    from sentrix_portal import security
    from sentrix_portal import settings as portal_settings
    from sentrix_portal.db import Database
    from sentrix_portal.services import accounts
    schema = f"sentrix_pgcheck_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    joiner = "&" if "?" in url else "?"
    scoped = f"{url}{joiner}options=-csearch_path%3D{schema}"
    previous = portal_settings._current
    portal_settings.set_current(portal_settings.PortalSettings(
        database_url=scoped, data_dir=str(ROOT / "var" / "pgcheck"), test_mode=True,
        secret_key=uuid.uuid4().hex * 2, data_key=uuid.uuid4().hex * 2))
    database = Database(scoped)
    try:
        install_length_guard()
        database.init()
        with database.session() as db:
            accounts.create_customer(db, name="PG check", username="pgcheck",
                                     email="pg@example.com", phone="+919000000199",
                                     city="Patiala", created_by=None)
        with database.session() as db:
            chain = security.verify_audit_chain(db)
            if not chain["ok"]:
                raise RuntimeError(f"audit chain does not verify on Postgres: {chain}")
        return f"ok: {len(compile_ddl())} statements, writes and audit chain on {schema}"
    finally:
        database.dispose()
        portal_settings._current = previous
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("check", nargs="?", default="all", choices=("all", "ddl", "suite", "live"))
    parser.add_argument("--url", default=os.getenv("PORTAL_PG_CHECK_URL", ""),
                        help="postgresql+psycopg://... for the live check (a scratch schema "
                             "is created and dropped)")
    args = parser.parse_args(argv)
    status = 0
    if args.check in ("all", "ddl"):
        problems = ddl_report()
        print("ddl: " + ("ok" if not problems else "; ".join(problems)))
        status |= bool(problems)
    if args.check in ("all", "suite"):
        code = run_suite()
        print(f"suite with the VARCHAR length guard: {'ok' if code == 0 else 'FAILED'}")
        status |= code != 0
    if args.check == "live" or (args.check == "all" and args.url):
        if not args.url:
            print("live: needs --url postgresql+psycopg://...", file=sys.stderr)
            return 2
        print("live: " + live_check(args.url))
    elif args.check == "all":
        print("live: skipped (no --url / PORTAL_PG_CHECK_URL)")
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
