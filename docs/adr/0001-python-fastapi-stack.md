# ADR 0001: Python, FastAPI, SQLAlchemy 2, scikit-learn

Status: accepted (2026-10-19)

## Context
The product is ML-heavy and sits beside two Node apps that must stay decoupled. The Python ML ecosystem is the natural home for feature engineering and model training.

## Decision
A separate Python service: FastAPI + pydantic v2 (typed OpenAPI for free), SQLAlchemy 2 + Alembic (SQLite default, PostgreSQL via DATABASE_URL), pandas/numpy, scikit-learn HistGradientBoosting (LightGBM optional), httpx for connectors, stdlib JSON logging, prometheus-client.

## Alternatives considered
Node + ONNX models (no training story); Django (heavier, no benefit); Polars (nicer at scale but pandas keeps the dependency set and team familiarity simple); statsmodels/ruptures for anomalies (replaced by a small in-house detector).

## Consequences
Two runtimes in the org. Sync SQLAlchemy runs in FastAPI's threadpool; only connectors are async. Install stays light (no LightGBM, no torch).
