.PHONY: install dev test lint typecheck docs docs-config docs-api docs-check demo bench
install:
	uv sync
dev:
	ENV=dev ALLOW_PRIVATE_SOURCES=true uv run uvicorn app.main:app_factory --factory --reload
test:
	uv run pytest --cov=app --cov-report=term-missing
lint:
	uv run ruff check . && uv run ruff format --check .
typecheck:
	uv run mypy app
docs-config:
	uv run python -m app.tools.docs_config
docs-api:
	uv run python -m app.tools.export_openapi
docs: docs-config docs-api
docs-check:
	uv run python -m app.tools.docs_config --check && uv run python -m app.tools.export_openapi --check && uv run pytest tests/test_docs.py
demo:
	uv run python -m app.synthetic.fake_sources --port 9101
bench:
	uv run python -m benchmarks.ingest_bench --rows 2000000
