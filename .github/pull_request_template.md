## What and why

## Checklist
- [ ] Tests added or updated, `uv run pytest` passes
- [ ] `uv run ruff check . && uv run mypy app` clean
- [ ] Docs updated in this PR (README / docs/*, ADR for significant decisions)
- [ ] If config changed: `uv run python -m app.tools.docs_config` re-run
- [ ] If the API changed: `uv run python -m app.tools.export_openapi` re-run
- [ ] `CHANGELOG.md` has an entry under Unreleased
- [ ] No secrets, no real customer data in code, tests or fixtures
