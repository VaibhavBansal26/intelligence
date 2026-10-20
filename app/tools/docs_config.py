"""Generate docs/CONFIGURATION.md from the pydantic-settings model so it cannot drift.

uv run python -m app.tools.docs_config          # write
uv run python -m app.tools.docs_config --check  # exit 1 if stale
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic_core import PydanticUndefined

from app.config import Settings

OUT = Path(__file__).resolve().parents[2] / "docs" / "CONFIGURATION.md"
REQUIRED_IN_PRODUCTION = {"kek", "admin_api_key"}

# name -> (what it does, example, security note)
DOCS: dict[str, tuple[str, str, str]] = {
    "env": (
        "Deployment mode. `production` enforces required secrets, https sources and forbids private sources.",
        "production",
        "Never run production with `dev`.",
    ),
    "database_url": (
        "SQLAlchemy URL. SQLite by default; PostgreSQL via `postgresql+psycopg://...` (install the `postgres` extra).",
        "postgresql+psycopg://user:pass@db/intelligence",
        "Contains credentials for PostgreSQL: inject from a secret store.",
    ),
    "auto_migrate": (
        "Run Alembic `upgrade head` at startup (`env=test` uses `create_all`).",
        "true",
        "Disable when migrations are run by a release job.",
    ),
    "model_dir": (
        "Directory for joblib model artifacts (per account/type/version, sha256 verified on load).",
        "/var/lib/intelligence/models",
        "Must be writable only by the service: artifacts are pickles and are trusted input.",
    ),
    "log_level": (
        "Log level for the JSON logger.",
        "INFO",
        "Logs never contain secrets; keys named key/secret/token/password/pepper are redacted.",
    ),
    "kek": (
        "Comma separated urlsafe-base64 32-byte Fernet keys. The first encrypts, all decrypt (rotation). Encrypts source API keys and identity peppers at rest.",
        'generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
        "REQUIRED in production. Losing it makes stored source credentials unreadable. In dev a public insecure key is used.",
    ),
    "admin_api_key": (
        "Separate platform-operator key for `/admin` (header `X-Admin-Key`). Empty disables the admin API.",
        "a 32+ char random string",
        "REQUIRED in production. Not a tenant key; compared in constant time.",
    ),
    "jwt_secret": (
        "HS256 secret enabling `POST /v1/auth/token` (API key -> short-lived JWT). Empty disables JWT.",
        "a 32+ char random string",
        "Rotate by redeploying; existing tokens die at expiry.",
    ),
    "jwt_ttl_s": ("JWT lifetime in seconds.", "3600", "Keep short."),
    "metrics_token": (
        "If set, `/metrics` requires `Authorization: Bearer <token>`.",
        "a random string",
        "Set it unless `/metrics` is only reachable on a private network.",
    ),
    "cors_origins": (
        "Comma separated CORS allow-list. Empty means no CORS headers.",
        "https://app.example.com",
        "Never use `*`.",
    ),
    "max_body_bytes": ("Maximum accepted request body (Content-Length) in bytes.", "1000000", ""),
    "rate_limit_per_min": (
        "Token-bucket refill per API key (per IP is 2x). In-memory, per instance.",
        "600",
        "Behind several instances the effective limit multiplies.",
    ),
    "rate_limit_burst": ("Token-bucket capacity per API key (per IP is 2x).", "60", ""),
    "allow_private_sources": (
        "Allow source URLs that resolve to private, loopback or link-local addresses (SSRF guard off).",
        "false",
        "Dev/test only. Forbidden in production (startup fails).",
    ),
    "connector_timeout_s": ("Per-request timeout for source calls.", "30", ""),
    "connector_max_attempts": ("Attempts per request (retries use exponential backoff with full jitter).", "5", ""),
    "connector_backoff_base_s": ("Backoff base in seconds.", "0.5", ""),
    "connector_backoff_max_s": (
        "Backoff ceiling in seconds. A `Retry-After` above 60 s is surfaced and the job is rescheduled instead of sleeping.",
        "30",
        "",
    ),
    "connector_page_limit": ("Rows per list page (contract maximum 1000); also the fallback export page size.", "1000", ""),
    "connector_export_page_limit": (
        "Rows requested per NDJSON export page. Exports are limited to 10 requests per minute per key, so pages are large (Funnels allows 20,000, Conversations 5,000); a source that answers 400 gets `connector_page_limit` instead.",
        "5000",
        "",
    ),
    "connector_max_retry_after_s": (
        "Longest `Retry-After` honoured by sleeping inside one request. A longer penalty (the apps escalate to minutes after repeated 429s) fails that resource with its cursor kept, so the next sync resumes.",
        "120",
        "",
    ),
    "connector_max_bad_line_ratio": (
        "Fraction of malformed NDJSON lines tolerated per page before the sync fails with a schema error.",
        "0.05",
        "A truncated final line is always retried, never skipped.",
    ),
    "min_schema_version": ("Oldest source `schema_version` accepted.", "1", ""),
    "max_schema_version": ("Newest tested `schema_version`; newer sources only produce a warning (additive contract).", "1", ""),
    "backfill_days": ("History fetched on the first sync of a resource (contract max range is 366 days).", "180", ""),
    "lookback_days": ("Trailing window re-read for mutable resources (leads, rooms) and rollups on every sync.", "30", ""),
    "ingest_chunk_rows": (
        "Rows buffered before a DB write and cursor checkpoint. Bounds memory and resume granularity.",
        "5000",
        "",
    ),
    "worker_enabled": (
        "Run the in-process job worker (sync/train jobs).",
        "true",
        "Run exactly one worker per database unless the queue is replaced.",
    ),
    "worker_poll_s": ("Worker idle poll interval.", "2", ""),
    "sync_interval_s": (
        "Enqueue a sync for every account whose last sync is older than this. 0 disables the scheduler.",
        "900",
        "",
    ),
    "columnar_dir": (
        "Root of the partitioned Parquet event store (`<dir>/<account>/events/bucket=NN`). When set, every synced event chunk is also appended there and contact profiles can be refreshed incrementally and out-of-core.",
        "/var/lib/intelligence/columnar",
        "Same trust level as the model directory.",
    ),
    "columnar_buckets": (
        "Hash buckets (by contact key) of the Parquet store: independent units of memory bounding and worker fan-out. Fixed for the life of the store.",
        "32",
        "Changing it requires rebuilding the store.",
    ),
    "account_max_concurrent_jobs": (
        "Per-account cap on simultaneously running jobs; the queue picks the account with the fewest running jobs first (fair scheduling).",
        "2",
        "Noisy-neighbour protection.",
    ),
    "account_max_event_rows": (
        "Per-account cap on landed event rows (0 = unlimited). A sync that would exceed it fails with a budget error.",
        "0",
        "Protects storage and shared workers.",
    ),
    "ml_max_training_rows": (
        "Training uses at most this many most-recent labelled rows per model.",
        "500000",
        "Bounds training time and memory per account.",
    ),
    "model_cache_size": (
        "Loaded model artifacts kept in memory (LRU). A promote or rollback swaps the active model without a restart.",
        "32",
        "",
    ),
    "batch_max_rows": ("Maximum rows per `POST /v1/leads/score/batch` request.", "5000", ""),
    "seed": ("Base random seed for every model (reproducible training).", "1234", ""),
    "label_horizon_days": (
        "Lead conversion label window: order within N days of opt-in. Also the embargo between train and validation.",
        "14",
        "Per-account override via account settings `label_horizon_days`.",
    ),
    "min_leads_for_model": ("Below this many mature labelled leads the documented heuristic is used.", "300", ""),
    "min_class_count": ("Minimum examples of each class in train and validation for a GBM.", "30", ""),
    "min_rooms_for_model": ("Below this many labelled rooms the SLA heuristic is used.", "300", ""),
    "min_contacts_for_segments": ("Below this many contacts segments are rule-based.", "100", ""),
    "sla_first_response_s": (
        "Default first-response SLA in seconds. Per-account override: account settings `sla_first_response_s`.",
        "3600",
        "",
    ),
    "vip_revenue_threshold": (
        "Lifetime funnel revenue that marks a contact VIP in conversation ranking. Per-account override: `vip_revenue_threshold`.",
        "500",
        "",
    ),
    "train_time_budget_s": ("Soft training time budget; exceeding it is recorded in the model metrics.", "120", ""),
    "psi_warn": ("PSI at or above which drift is `warn`.", "0.1", ""),
    "psi_alert": ("PSI at or above which drift is `alert`.", "0.25", ""),
    "anthropic_api_key": (
        "Enables the LLM layer. Empty uses the deterministic TemplateNarrator and offline Q&A.",
        "sk-ant-...",
        "Secret. Prompts contain only aggregates and sanitised names, never contact keys or emails.",
    ),
    "anthropic_model": ("Model id used by the Anthropic client.", "claude-sonnet-5-5", ""),
    "llm_timeout_s": ("Per-call LLM timeout; failures fall back to templates.", "30", ""),
    "llm_max_input_chars": (
        "Input budget per LLM call; larger prompts are refused (template fallback).",
        "24000",
        "Cost control.",
    ),
    "llm_max_output_tokens": ("max_tokens per LLM call.", "1024", "Cost control."),
    "llm_daily_call_cap": ("Per-account daily LLM call cap (cache hits are free).", "200", "Cost control."),
    "llm_daily_token_cap": ("Per-account daily input+output token cap.", "400000", "Cost control."),
    "llm_cache_ttl_s": ("TTL of the identical-prompt response cache.", "3600", ""),
    "llm_redact_pii": ("Scrub emails, phone numbers and long hex identifiers from prompts.", "true", "Leave on."),
    "llm_max_tool_steps": ("Maximum tool-use rounds for `POST /v1/ask`.", "4", ""),
}


def _default(info: Any) -> str:
    if info.default is PydanticUndefined:
        return "(required)"
    return (
        "`" + (repr(info.default) if not isinstance(info.default, bool) else str(info.default).lower()) + "`"
        if info.default != ""
        else "(empty)"
    )


def render() -> str:
    lines = [
        "# Configuration reference",
        "",
        "<!-- GENERATED by `uv run python -m app.tools.docs_config`. Do not edit by hand. -->",
        "",
        "Every setting is an environment variable (upper-case name), read by `app/config.py` (pydantic-settings; a `.env` file is also read).",
        "Copy `.env.example` to start. Settings marked **required in production** make the service refuse to start without them.",
        "",
        "| Variable | Default | Required | Example | What it does | Security notes |",
        "|---|---|---|---|---|---|",
    ]
    for name, info in Settings.model_fields.items():
        what, example, sec = DOCS.get(name, ("UNDOCUMENTED", "", ""))
        req = "**yes (production)**" if name in REQUIRED_IN_PRODUCTION else "no"
        lines.append(
            f"| `{name.upper()}` | {_default(info)} | {req} | {('`' + example + '`') if example else ''} | {what} | {sec} |".replace(
                "\n", " "
            )
        )
    lines += [
        "",
        "Only `ENV`, `KEK` and `ADMIN_API_KEY` matter for a first production deploy; everything else has a working default.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/CONFIGURATION.md is stale: run `uv run python -m app.tools.docs_config`")
            return 1
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
