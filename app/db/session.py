"""Engine / session factory and dialect-aware bulk insert helpers."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.models import Base

SessionFactory = sessionmaker[Session]


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if ":memory:" not in url and url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 30}, poolclass=NullPool if ":memory:" not in url else None
        )

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(engine, expire_on_commit=False)


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@contextlib.contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _insert_for(session: Session, table: Any) -> Any:
    name = session.get_bind().dialect.name
    if name == "postgresql":
        return postgresql.insert(table)
    return sqlite.insert(table)


def insert_ignore(session: Session, table: Any, rows: Sequence[dict[str, Any]]) -> int:
    """Idempotent insert: duplicates (by primary key) are skipped. Returns rows attempted."""
    if not rows:
        return 0
    stmt = _insert_for(session, table).on_conflict_do_nothing()
    session.execute(stmt, list(rows))
    return len(rows)


def upsert(session: Session, table: Any, rows: Sequence[dict[str, Any]]) -> int:
    """Idempotent insert-or-update by primary key (mutable resources such as leads and rooms)."""
    if not rows:
        return 0
    pk = [c.name for c in table.primary_key.columns]
    base = _insert_for(session, table)
    update_cols = {c.name: base.excluded[c.name] for c in table.columns if c.name not in pk}
    stmt = base.on_conflict_do_update(index_elements=pk, set_=update_cols)
    session.execute(stmt, list(rows))
    return len(rows)
