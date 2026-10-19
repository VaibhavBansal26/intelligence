from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.db.models import Base
from app.main import run_migrations


def test_alembic_head_matches_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/mig.db"
    run_migrations(url)
    insp = inspect(create_engine(url))
    migrated = {t for t in insp.get_table_names() if t != "alembic_version"}
    assert migrated == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        assert {c["name"] for c in insp.get_columns(name)} == {c.name for c in table.columns}, name
        pk = insp.get_pk_constraint(name)["constrained_columns"]
        assert set(pk) == {c.name for c in table.primary_key.columns}, name


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/mig2.db"
    run_migrations(url)
    run_migrations(url)
