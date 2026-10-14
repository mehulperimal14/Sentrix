"""Postgres compatibility of the Portal models (tools/portal_pg_check.py;
deploy/portal). No server needed: DDL is compiled for the PostgreSQL dialect,
and the VARCHAR length guard Postgres would apply is emulated on SQLite."""

from __future__ import annotations

import sys

import pytest

from sentrix.paths import ROOT_DIR

if str(ROOT_DIR / "tools") not in sys.path:
    sys.path.append(str(ROOT_DIR / "tools"))

import portal_pg_check  # noqa: E402


@pytest.fixture
def length_guard():
    """The guard for one test only (the rest of the suite runs without it)."""
    portal_pg_check.install_length_guard()
    yield
    portal_pg_check.remove_length_guard()


def test_every_table_compiles_for_postgres():
    statements = portal_pg_check.compile_ddl()
    tables = [s for s in statements if s.lstrip().upper().startswith("CREATE TABLE")]
    from sentrix_portal.db import Base
    assert len(tables) == len(Base.metadata.sorted_tables) >= 20
    joined = "\n".join(statements)
    assert "TIMESTAMP WITH TIME ZONE" in joined and "BYTEA" in joined
    assert "AUTOINCREMENT" not in joined.upper()
    assert portal_pg_check.ddl_report() == []


def test_outbox_dedupe_compiles_to_on_conflict():
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in portal_pg_check.compile_outbox_upsert()


def test_length_guard_catches_what_postgres_would_reject(portal_app, length_guard):
    from sentrix_portal.models import AuditLog
    with pytest.raises(portal_pg_check.ValueTooLong):
        with portal_app.state.db.session() as db:
            db.add(AuditLog(event="x" * 49))
            db.flush()
    with portal_app.state.db.session() as db:
        db.add(AuditLog(event="x" * 48))


def test_the_demo_world_fits_postgres_columns(tmp_path, length_guard):
    import demo_reset
    world = demo_reset.reset(tmp_path / "demo", live_steps=0)
    assert world.info["serial"].startswith("SX5-")


def test_live_check_says_when_the_driver_is_missing(length_guard):
    try:
        import psycopg  # noqa: F401
    except ImportError:
        assert portal_pg_check.live_check("postgresql+psycopg://x@127.0.0.1/none") \
            .startswith("skipped")
    else:                                              # pragma: no cover - driver present
        pytest.skip("psycopg installed; the live check needs a real server")
