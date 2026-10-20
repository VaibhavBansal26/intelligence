"""SQLAlchemy 2 declarative schema. Timestamps in analytics tables are UTC epoch seconds (ints)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --- tenancy & auth -----------------------------------------------------------------------------
class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[int] = mapped_column(BigInteger)
    data_version: Mapped[int] = mapped_column(Integer, default=1)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("account_id", "name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # funnels | conversations
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_enc: Mapped[str] = mapped_column(Text)
    pepper_enc: Mapped[str] = mapped_column(Text)
    pepper_fp: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("source_id", "resource"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    resource: Mapped[str] = mapped_column(String(32))
    watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # max ts seen (epoch s)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {"c","from"}: resume point of an unfinished run
    last_run_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rows_total: Mapped[int] = mapped_column(BigInteger, default=0)
    bad_lines_total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContactLink(Base):
    """Identity bridge: a contact's key under one source's pepper -> the account's canonical key (see /v1/identity/bridge).

    The real apps each generate their own random identity pepper, so the same e-mail has a different `contact_key` in
    each. A customer-supplied contact list lets the service compute both keys (in memory; the e-mails are never stored)
    and record which pairs are the same person."""

    __tablename__ = "contact_links"
    __table_args__ = (Index("ix_links_acct_canonical", "account_id", "canonical_key"),)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    contact_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    canonical_key: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(16))  # sync | train
    status: Mapped[str] = mapped_column(String(16), index=True)  # queued|running|succeeded|failed
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


# --- landed raw data --------------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_acct_ts", "account_id", "ts"),
        Index("ix_events_acct_contact", "account_id", "contact_key"),
        Index("ix_events_acct_visitor", "account_id", "visitor_key"),
    )
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ext_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    ts: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(16))
    funnel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visitor_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    contact_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    utm_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(150), nullable=True)
    device: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    referrer_host: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    unique_visit: Mapped[bool] = mapped_column(Boolean, default=False)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_acct_created", "account_id", "created_at"),
        Index("ix_leads_acct_contact", "account_id", "contact_key"),
    )
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ext_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[int] = mapped_column(BigInteger)
    funnel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_key: Mapped[str] = mapped_column(String(80))
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(150), nullable=True)
    converted_to_order: Mapped[bool] = mapped_column(Boolean, default=False)
    order_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_order_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Funnel(Base):
    __tablename__ = "funnels"
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    funnel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    steps_count: Mapped[int] = mapped_column(Integer, default=0)
    visits: Mapped[int] = mapped_column(BigInteger, default=0)
    unique_visits: Mapped[int] = mapped_column(BigInteger, default=0)
    optins: Mapped[int] = mapped_column(BigInteger, default=0)
    orders: Mapped[int] = mapped_column(BigInteger, default=0)
    gross: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    synced_at: Mapped[int] = mapped_column(BigInteger)


class FunnelStep(Base):
    __tablename__ = "funnel_steps"
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    funnel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(300))
    ord: Mapped[int] = mapped_column(Integer)
    visits: Mapped[int] = mapped_column(BigInteger, default=0)
    unique_visits: Mapped[int] = mapped_column(BigInteger, default=0)
    optins: Mapped[int] = mapped_column(BigInteger, default=0)
    orders: Mapped[int] = mapped_column(BigInteger, default=0)
    gross: Mapped[float] = mapped_column(Float, default=0.0)
    exit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_time_on_page_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    synced_at: Mapped[int] = mapped_column(BigInteger)


class FunnelStructure(Base):
    __tablename__ = "funnel_structure"
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    funnel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    funnel_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    step_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)
    step_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    n_form: Mapped[int] = mapped_column(Integer, default=0)
    n_checkout: Mapped[int] = mapped_column(Integer, default=0)
    n_video: Mapped[int] = mapped_column(Integer, default=0)
    n_countdown: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    has_order_bump: Mapped[bool] = mapped_column(Boolean, default=False)
    has_upsell: Mapped[bool] = mapped_column(Boolean, default=False)


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        Index("ix_rooms_acct_created", "account_id", "created_at"),
        Index("ix_rooms_acct_contact", "account_id", "contact_key"),
    )
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ext_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[int] = mapped_column(BigInteger)
    closed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    contact_key: Mapped[str | None] = mapped_column(String(80), nullable=True)  # canonical key (bridged) or the source's own
    contact_key_raw: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )  # the key as exported; set once a bridge re-keys the row
    mix_dm: Mapped[int] = mapped_column(Integer, default=0)
    mix_sms: Mapped[int] = mapped_column(Integer, default=0)
    mix_email: Mapped[int] = mapped_column(Integer, default=0)
    inbox_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assignee_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    messages_in: Mapped[int] = mapped_column(Integer, default=0)
    messages_out: Mapped[int] = mapped_column(Integer, default=0)
    first_response_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    snoozed_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_sentiment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_urgency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    csat: Mapped[float | None] = mapped_column(Float, nullable=True)
    hour_of_day_created: Mapped[int] = mapped_column(Integer, default=0)
    dow_created: Mapped[int] = mapped_column(Integer, default=0)


class MessageMeta(Base):
    __tablename__ = "messages_meta"
    __table_args__ = (Index("ix_msgs_acct_room", "account_id", "room_id"),)
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ext_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    room_id: Mapped[str] = mapped_column(String(64))
    ts: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(4))
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    length_chars: Mapped[int] = mapped_column(Integer, default=0)
    delivery_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sender_type: Mapped[str | None] = mapped_column(String(24), nullable=True)


class Rollup(Base):
    __tablename__ = "rollups"
    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric: Mapped[str] = mapped_column(String(48), primary_key=True)
    scope: Mapped[str] = mapped_column(String(80), primary_key=True)  # funnel id or "all"
    interval: Mapped[str] = mapped_column(String(8), primary_key=True)
    t: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[float] = mapped_column(Float)


# --- ML / platform ----------------------------------------------------------------------------------
class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("account_id", "model_type", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    model_type: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # candidate|active|archived
    method: Mapped[str] = mapped_column(String(24))  # gbm | heuristic
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    card: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    trained_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    promoted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AnomalyAlert(Base):
    __tablename__ = "anomaly_alerts"
    __table_args__ = (UniqueConstraint("account_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(16))
    metric: Mapped[str] = mapped_column(String(48))
    scope: Mapped[str] = mapped_column(String(80))
    direction: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(16))  # point | shift
    start_t: Mapped[int] = mapped_column(BigInteger)
    end_t: Mapped[int] = mapped_column(BigInteger)
    severity: Mapped[float] = mapped_column(Float)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[int] = mapped_column(BigInteger)
    last_seen_at: Mapped[int] = mapped_column(BigInteger)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger)


class LlmCache(Base):
    __tablename__ = "llm_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
