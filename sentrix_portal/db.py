"""Portal database (SQLAlchemy 2). SQLite in development and tests, Postgres 16
in production (``PORTAL_DATABASE_URL``). Tables: ``sentrix_portal.models``.
Schema changes after the first deploy go through Alembic (integration track).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False,
                                         autoflush=False, expire_on_commit=False)

    def init(self) -> None:
        """Create missing tables, then add missing nullable columns to
        existing ones (additive only; nothing is dropped or rewritten). After
        the first deployment schema changes go through Alembic."""
        from sentrix_portal import models  # noqa: F401  (registers the tables)
        Base.metadata.create_all(bind=self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> list[str]:
        added = []
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue
                present = {c["name"] for c in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in present or column.primary_key or not column.nullable:
                        continue
                    sql_type = column.type.compile(dialect=self.engine.dialect)
                    conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN '
                                         f'{column.name} {sql_type}')
                    added.append(f"{table.name}.{column.name}")
        return added

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def dispose(self) -> None:
        self.engine.dispose()


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed on success."""
    database: Database = request.app.state.db
    with database.session() as db:
        yield db


__all__ = ["Base", "Database", "make_engine", "get_db"]
