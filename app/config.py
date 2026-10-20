"""Typed configuration (environment driven, validated at boot)."""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed, public, INSECURE key used only when ENV != production and no KEK is configured.
DEV_KEK = base64.urlsafe_b64encode(b"dev-only-insecure-kek-0123456789").decode()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    env: Literal["dev", "test", "production"] = "dev"
    database_url: str = "sqlite:///./data/intelligence.db"
    auto_migrate: bool = True
    model_dir: str = "./data/models"
    log_level: str = "INFO"

    # secrets
    kek: str = Field(default="", description="comma separated urlsafe-base64 32-byte Fernet keys; first encrypts")
    admin_api_key: str = ""
    jwt_secret: str = ""
    jwt_ttl_s: int = 3600
    metrics_token: str = ""

    # http surface
    cors_origins: str = ""
    max_body_bytes: int = 1_000_000
    rate_limit_per_min: int = 600
    rate_limit_burst: int = 60

    # connectors
    allow_private_sources: bool = False
    connector_timeout_s: float = 30.0
    connector_max_attempts: int = 5
    connector_backoff_base_s: float = 0.5
    connector_backoff_max_s: float = 30.0
    connector_page_limit: int = 1000  # list endpoints (contract maximum)
    connector_export_page_limit: int = (
        5000  # NDJSON exports: fewer, bigger pages (10 requests/min/key); falls back to connector_page_limit on a 400
    )
    connector_max_retry_after_s: float = (
        120.0  # longest Retry-After honoured inside one request; longer fails the resource (cursor kept)
    )
    connector_max_bad_line_ratio: float = 0.05
    min_schema_version: int = 1
    max_schema_version: int = 1

    # ingest
    backfill_days: int = 180
    lookback_days: int = 30
    ingest_chunk_rows: int = 5000
    worker_enabled: bool = False
    worker_poll_s: float = 2.0
    sync_interval_s: int = 0  # 0 disables the periodic scheduler

    # scale and per-account budgets
    columnar_dir: str = ""  # Parquet event store root; empty disables the columnar tee
    columnar_buckets: int = 32
    account_max_concurrent_jobs: int = 2
    account_max_event_rows: int = 0  # 0 = unlimited landed events per account
    ml_max_training_rows: int = 500_000
    model_cache_size: int = 32
    batch_max_rows: int = 5000

    # ML
    seed: int = 1234
    label_horizon_days: int = 14
    min_leads_for_model: int = 300
    min_class_count: int = 30
    min_rooms_for_model: int = 300
    min_contacts_for_segments: int = 100
    sla_first_response_s: int = 3600
    vip_revenue_threshold: float = 500.0
    train_time_budget_s: float = 120.0
    psi_warn: float = 0.1
    psi_alert: float = 0.25

    # LLM
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    llm_timeout_s: float = 30.0
    llm_max_input_chars: int = 24_000
    llm_max_output_tokens: int = 1024
    llm_daily_call_cap: int = 200
    llm_daily_token_cap: int = 400_000
    llm_cache_ttl_s: int = 3600
    llm_redact_pii: bool = True
    llm_max_tool_steps: int = 4

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production":
            missing = [n for n in ("kek", "admin_api_key") if not getattr(self, n)]
            if missing:
                raise ValueError(f"missing required settings in production: {', '.join(missing)}")
            if self.allow_private_sources:
                raise ValueError("ALLOW_PRIVATE_SOURCES must be false in production")
        return self

    @property
    def kek_list(self) -> list[str]:
        raw = self.kek or DEV_KEK
        return [k.strip() for k in raw.split(",") if k.strip()]

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
