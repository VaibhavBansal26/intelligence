"""Tolerant record models for the analytics contract. Unknown fields are ignored; bad rows are rejected one by one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def parse_ts(value: Any) -> int | None:
    """ISO-8601 (Z or offset, date or datetime) or epoch seconds/ms -> UTC epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("bad timestamp")
    if isinstance(value, int | float):
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    raise ValueError("bad timestamp")


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


class _Rec(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EventRec(_Rec):
    id: str
    ts: int
    type: str
    funnel_id: str | None = None
    step_id: str | None = None
    visitor_key: str | None = None
    contact_key: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    device: str | None = None
    country: str | None = None
    referrer_host: str | None = None
    amount: float | None = None
    currency: str | None = None
    unique_visit: bool = False

    @field_validator("id", "funnel_id", "step_id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)

    @field_validator("ts", mode="before")
    @classmethod
    def _ts(cls, v: Any) -> int:
        out = parse_ts(v)
        if out is None:
            raise ValueError("ts required")
        return out

    def to_row(self, account_id: str, source_id: str) -> dict[str, Any]:
        d = self.model_dump()
        d["ext_id"] = d.pop("id")
        return {"account_id": account_id, "source_id": source_id, **d}


class LeadRec(_Rec):
    id: str
    created_at: int
    funnel_id: str | None = None
    step_id: str | None = None
    contact_key: str
    source: str | None = None
    utm_source: str | None = None
    utm_campaign: str | None = None
    converted_to_order: bool = False
    order_total: float | None = None
    first_order_at: int | None = None

    @field_validator("id", "funnel_id", "step_id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)

    @field_validator("created_at", mode="before")
    @classmethod
    def _created(cls, v: Any) -> int:
        out = parse_ts(v)
        if out is None:
            raise ValueError("created_at required")
        return out

    @field_validator("first_order_at", mode="before")
    @classmethod
    def _first(cls, v: Any) -> int | None:
        return parse_ts(v)

    def to_row(self, account_id: str, source_id: str) -> dict[str, Any]:
        d = self.model_dump()
        d["ext_id"] = d.pop("id")
        return {"account_id": account_id, "source_id": source_id, **d}


class RoomRec(_Rec):
    id: str
    created_at: int
    closed_at: int | None = None
    contact_key: str | None = None
    channel_mix: dict[str, int] = Field(default_factory=dict)
    inbox_id: str | None = None
    assignee_key: str | None = None
    priority: int = 0
    status: str | None = None
    messages_in: int = 0
    messages_out: int = 0
    first_response_s: float | None = None
    resolution_s: float | None = None
    reopen_count: int = 0
    snoozed_count: int = 0
    ai_intent: str | None = None
    ai_sentiment: str | None = None
    ai_urgency: str | None = None
    csat: float | None = None
    hour_of_day_created: int = 0
    dow_created: int = 0

    @field_validator("id", "inbox_id", "ai_intent", "ai_sentiment", "ai_urgency", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)

    @field_validator("created_at", mode="before")
    @classmethod
    def _created(cls, v: Any) -> int:
        out = parse_ts(v)
        if out is None:
            raise ValueError("created_at required")
        return out

    @field_validator("closed_at", mode="before")
    @classmethod
    def _closed(cls, v: Any) -> int | None:
        return parse_ts(v)

    @field_validator("channel_mix", mode="before")
    @classmethod
    def _mix(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        return {str(k): int(x) for k, x in v.items() if isinstance(x, int | float)}

    def to_row(self, account_id: str, source_id: str) -> dict[str, Any]:
        d = self.model_dump()
        mix = d.pop("channel_mix")
        d["ext_id"] = d.pop("id")
        return {
            "account_id": account_id,
            "source_id": source_id,
            "mix_dm": mix.get("dm", 0),
            "mix_sms": mix.get("sms", 0),
            "mix_email": mix.get("email", 0),
            **d,
        }


class MessageMetaRec(_Rec):
    id: str
    room_id: str
    ts: int
    direction: str
    channel: str | None = None
    kind: str | None = None
    length_chars: int = 0
    delivery_status: str | None = None
    sender_type: str | None = None

    @field_validator("id", "room_id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)

    @field_validator("ts", mode="before")
    @classmethod
    def _ts(cls, v: Any) -> int:
        out = parse_ts(v)
        if out is None:
            raise ValueError("ts required")
        return out

    def to_row(self, account_id: str, source_id: str) -> dict[str, Any]:
        d = self.model_dump()
        d["ext_id"] = d.pop("id")
        return {"account_id": account_id, "source_id": source_id, **d}


class FunnelRec(_Rec):
    id: str
    name: str = ""
    status: str | None = None
    steps_count: int = 0
    visits: int = 0
    unique_visits: int = 0
    optins: int = 0
    orders: int = 0
    gross: float = 0.0
    currency: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)


class StepRec(_Rec):
    step_id: str
    name: str = ""
    order: int = 0
    visits: int = 0
    unique_visits: int = 0
    optins: int = 0
    orders: int = 0
    gross: float = 0.0
    exit_rate: float | None = None
    avg_time_on_page_s: float | None = None

    @field_validator("step_id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)


class StructureStepRec(_Rec):
    step_id: str
    name: str | None = None
    order: int = 0
    type: str | None = None
    blocks: dict[str, int] = Field(default_factory=dict)
    word_count: int = 0
    has_order_bump: bool = False
    has_upsell: bool = False

    @field_validator("step_id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)

    @field_validator("blocks", mode="before")
    @classmethod
    def _blocks(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        return {str(k): int(x) for k, x in v.items() if isinstance(x, int | float)}


class StructureFunnelRec(_Rec):
    funnel_id: str
    name: str | None = None
    steps: list[StructureStepRec] = Field(default_factory=list)

    @field_validator("funnel_id", mode="before")
    @classmethod
    def _str_ids(cls, v: Any) -> Any:
        return _s(v)


class PointRec(_Rec):
    t: int
    value: float

    @field_validator("t", mode="before")
    @classmethod
    def _t(cls, v: Any) -> int:
        out = parse_ts(v)
        if out is None:
            raise ValueError("t required")
        return out


EXPORT_MODELS: dict[str, type[_Rec]] = {
    "events": EventRec,
    "leads": LeadRec,
    "rooms": RoomRec,
    "messages_meta": MessageMetaRec,
}
