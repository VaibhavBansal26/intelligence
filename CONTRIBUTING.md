# Contributing

## Setup
```bash
uv sync                       # creates .venv with dev tools
cp .env.example .env
uv run pre-commit install     # optional
```

## Run
```bash
make dev                      # uvicorn with reload, ALLOW_PRIVATE_SOURCES=true
make demo                     # fake Funnels (:9101) and Conversations (:9102) contract servers
```

## Checks (also run in CI)
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest --cov=app                    # about 3 minutes
uv run pytest -m slow -s tests/perf        # 2M-row bounded-memory benchmark
make docs-check                            # config + OpenAPI freshness, links, Mermaid
```

## Branches and commits
Short-lived branches, [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
One logical change per commit; docs and tests belong in the same commit as the code.

## Updating docs
- Added or changed a setting: edit `app/config.py` **and** the `DOCS` map in `app/tools/docs_config.py`, then `make docs-config`.
- Changed a route or schema: `make docs-api` (rewrites `docs/openapi.json`) and update the examples in `docs/API.md`.
- New doc file: add it to `docs/README.md` (the docs test fails otherwise).
- Significant decision: add `docs/adr/NNNN-title.md`.
- User-visible change: add a line under `Unreleased` in `CHANGELOG.md`.

## Tests
See [docs/TESTING.md](docs/TESTING.md). Tests never touch the network: LLM and source apps are fakes.
