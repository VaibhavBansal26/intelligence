# ADR 0003: SQLite by default, PostgreSQL supported

Status: accepted (2026-10-19)

## Context
Zero-config start matters for evaluation and small customers; production multi-tenant use wants PostgreSQL.

## Decision
SQLAlchemy Core/ORM only through dialect-neutral constructs, plus dialect-aware insert-or-ignore and upsert helpers (SQLite and PostgreSQL). SQLite runs in WAL mode. Alembic migrations are authoritative; tests use create_all, and `tests/test_migrations.py` asserts the migrated schema equals the models.

## Alternatives considered
PostgreSQL only (needs Docker for every dev run); SQLite only (no real concurrency).

## Consequences
PostgreSQL is verified by construction, not by CI in this repository (no database was available when it was built): treat the first PostgreSQL deployment as a verification step.
