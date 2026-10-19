"""Request/response models (OpenAPI source of truth)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class AccountOut(BaseModel):
    id: str
    name: str
    created_at: int
    settings: dict[str, Any]
    disabled: bool = False


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None


class SourceCreate(BaseModel):
    kind: Literal["funnels", "conversations"]
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=8, max_length=500, examples=["https://funnels.example.com"])
    api_key: str = Field(min_length=8, max_length=500)
    identity_pepper: str = Field(min_length=8, max_length=500)


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    identity_pepper: str | None = Field(default=None, max_length=500)
    status: Literal["active", "paused"] | None = None


class SourceOut(BaseModel):
    id: str
    kind: str
    name: str
    base_url: str
    status: str
    pepper_fingerprint: str
    last_schema_version: int | None
    created_at: int


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "analyst", "viewer"]
    scopes: list[str] = Field(default_factory=list)
    expires_at: int | None = None


class KeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    role: str
    scopes: list[str]
    expires_at: int | None
    revoked_at: int | None
    last_used_at: int | None
    created_at: int


class KeyCreated(KeyOut):
    api_key: str = Field(description="Plaintext key, shown once.")


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class SyncRequest(BaseModel):
    source_id: str | None = None
    full: bool = False


class JobOut(BaseModel):
    id: str
    type: str
    status: str
    attempts: int
    created_at: int
    started_at: int | None
    finished_at: int | None
    error: str | None
    result: dict[str, Any] | None


class SyncStatus(BaseModel):
    sources: list[dict[str, Any]]
    jobs: list[JobOut]


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class SourceList(Page):
    items: list[SourceOut]


class RankingResponse(BaseModel):
    method: str
    notes: list[str]
    currency: str | None = None
    funnels: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    priors: dict[str, Any] = Field(default_factory=dict)
    benchmarks: dict[str, Any] = Field(default_factory=dict)
    rerank: dict[str, Any] | None = None


class LeadScoresResponse(BaseModel):
    method: str
    model_version: int | None
    cold_start_reason: str | None
    as_of: str | None
    horizon_days: int
    conversation_features_used: bool
    total: int
    items: list[dict[str, Any]]


class LeadScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_key: str | None = Field(default=None, max_length=80)
    email: str | None = Field(
        default=None, max_length=320, description="Hashed in memory with the source pepper; never stored or logged."
    )
    features: dict[str, Any] | None = None


class BatchScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: list[dict[str, Any]] | None = Field(default=None, description="Feature vectors (same names as the model features).")
    contact_keys: list[str] | None = Field(default=None, description="Known contacts, scored from their latest lead features.")


class BatchScoreResponse(BaseModel):
    method: str
    model_version: int | None
    cold_start_reason: str | None
    count: int
    scores: list[float | None]
    not_found: list[int]


class OpenConversationsResponse(BaseModel):
    method: str
    model_version: int | None
    cold_start_reason: str | None
    as_of: str | None
    sla_seconds: float
    vip_revenue_threshold: float
    total_open: int
    items: list[dict[str, Any]]


class SegmentsResponse(BaseModel):
    method: str
    contacts: int
    model_version: int | None = None
    segments: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)


class AnomaliesResponse(BaseModel):
    status: str
    series_checked: int
    alerts: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)
    series_skipped_insufficient_history: int = 0


class InsightsResponse(BaseModel):
    findings: list[dict[str, Any]]
    narrative: dict[str, Any]
    evidence: dict[str, Any]


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class AskResponse(BaseModel):
    answer: str
    source: str
    tools_used: list[str]
    evidence: list[dict[str, Any]]
    fallback_reason: str | None = None


class ModelOut(BaseModel):
    id: str
    model_type: str
    version: int
    status: str
    method: str
    metrics: dict[str, Any]
    card: dict[str, Any]
    trained_rows: int
    seed: int
    created_at: int
    promoted_at: int | None


class TrainRequest(BaseModel):
    model_types: list[str] | None = None


class Health(BaseModel):
    status: str
    version: str


class Ready(BaseModel):
    status: str
    checks: dict[str, str]
