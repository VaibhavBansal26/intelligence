"""Point-in-time lead features (as of opt-in) and labels for the conversion model."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m
from app.features.norm import flag_eq, map_unique, sentiment_value, urgency_value

NUMERIC_FEATURES = [
    "n_page_views_pre",
    "n_distinct_steps_pre",
    "hours_since_first_seen",
    "optin_hour",
    "optin_dow",
    "prior_orders",
    "prior_rooms",
    "prior_intent_purchase",
    "prior_sentiment_mean",
    "prior_urgency_max",
    "prior_first_response_mean_s",
    "prior_csat_mean",
]
CATEGORICAL_FEATURES = ["utm_source", "funnel_id", "device"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
CONVERSATION_FEATURES = [
    "prior_rooms",
    "prior_intent_purchase",
    "prior_sentiment_mean",
    "prior_urgency_max",
    "prior_first_response_mean_s",
    "prior_csat_mean",
]


@dataclass
class LeadFrame:
    df: pd.DataFrame
    as_of: int
    horizon_days: int
    has_conversations: bool


def data_as_of(session: Session, account_id: str) -> int:
    from sqlalchemy import func

    ev = session.execute(select(func.max(m.Event.ts)).where(m.Event.account_id == account_id)).scalar() or 0
    ld = session.execute(select(func.max(m.Lead.created_at)).where(m.Lead.account_id == account_id)).scalar() or 0
    return int(max(ev, ld))


def build_lead_frame(session: Session, account_id: str, *, horizon_days: int, as_of: int | None = None) -> LeadFrame:
    conn = session.connection()
    as_of = as_of or data_as_of(session, account_id)
    ld = m.Lead
    leads = pd.read_sql(
        select(
            ld.ext_id.label("lead_id"),
            ld.source_id,
            ld.contact_key,
            ld.created_at,
            ld.funnel_id,
            ld.step_id,
            ld.utm_source,
            ld.converted_to_order,
            ld.order_total,
            ld.first_order_at,
        ).where(ld.account_id == account_id),
        conn,
    )
    if leads.empty:
        return LeadFrame(leads, as_of, horizon_days, False)
    leads["lead_id"] = leads["source_id"] + ":" + leads["lead_id"]
    leads = leads.drop(columns=["source_id"])
    e = m.Event
    vmap = pd.read_sql(
        select(e.visitor_key, e.contact_key)
        .where(e.account_id == account_id, e.contact_key.is_not(None), e.visitor_key.is_not(None))
        .distinct(),
        conn,
    )
    vmap = vmap.drop_duplicates("visitor_key")
    if vmap.empty:
        ev = pd.DataFrame(columns=["visitor_key", "ts", "step_id", "type", "device", "contact_key"])
    else:
        sub = select(e.visitor_key).where(e.account_id == account_id, e.contact_key.is_not(None)).distinct()
        ev = pd.read_sql(
            select(e.visitor_key, e.ts, e.step_id, e.type, e.device).where(e.account_id == account_id, e.visitor_key.in_(sub)),
            conn,
        )
        ev = ev.merge(vmap, on="visitor_key", how="inner")
    leads = _funnel_pre_features(leads, ev)
    has_conv = bool(session.execute(select(m.Room.ext_id).where(m.Room.account_id == account_id).limit(1)).first())
    leads = _conversation_pre_features(leads, conn, account_id) if has_conv else _empty_conv(leads)
    leads["optin_hour"] = ((leads["created_at"] // 3600) % 24).astype(int)
    leads["optin_dow"] = (((leads["created_at"] // 86400) + 3) % 7).astype(int)
    horizon_s = horizon_days * 86400
    fo = leads["first_order_at"]
    leads["label"] = (fo.notna() & ((fo - leads["created_at"]) <= horizon_s) & ((fo - leads["created_at"]) >= 0)).astype(int)
    # leakage guards: only leads whose full label window has elapsed (before as_of) are trainable, and
    # converted-but-undated leads are excluded because the label timing is unknown.
    undated = leads["converted_to_order"].astype(bool) & fo.isna()
    leads["mature"] = ((leads["created_at"] + horizon_s) <= as_of) & ~undated
    return LeadFrame(leads, as_of, horizon_days, has_conv)


def _funnel_pre_features(leads: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    base = leads[["lead_id", "contact_key", "created_at"]]
    if ev.empty:
        for c in ("n_page_views_pre", "n_distinct_steps_pre", "hours_since_first_seen", "prior_orders"):
            leads[c] = np.nan
        leads["device"] = None
        return leads
    mrg = base.merge(ev, on="contact_key", how="left")
    mrg = mrg[mrg["ts"] <= mrg["created_at"]]
    mrg = mrg.sort_values(["lead_id", "ts"])
    g = mrg.groupby("lead_id")
    feats = pd.DataFrame(
        {
            "n_page_views_pre": g["type"].apply(lambda s: int((s == "page_view").sum())),
            "n_distinct_steps_pre": g["step_id"].nunique(),
            "first_ts": g["ts"].min(),
            "prior_orders": g["type"].apply(lambda s: int((s == "order").sum())),
            "device": g["device"].first(),
        }
    )
    out = leads.merge(feats, left_on="lead_id", right_index=True, how="left")
    out["hours_since_first_seen"] = (out["created_at"] - out["first_ts"]) / 3600.0
    return out.drop(columns=["first_ts"])


def _empty_conv(leads: pd.DataFrame) -> pd.DataFrame:
    for c in CONVERSATION_FEATURES:
        leads[c] = np.nan
    return leads


def _conversation_pre_features(leads: pd.DataFrame, conn: object, account_id: str) -> pd.DataFrame:
    r = m.Room
    rooms = pd.read_sql(
        select(
            r.contact_key,
            r.created_at.label("room_at"),
            r.closed_at,
            r.ai_intent,
            r.ai_sentiment,
            r.ai_urgency,
            r.first_response_s,
            r.csat,
        ).where(r.account_id == account_id, r.contact_key.is_not(None)),
        conn,
    )
    if rooms.empty:
        return _empty_conv(leads)
    rooms["sent"] = map_unique(rooms["ai_sentiment"], sentiment_value)
    rooms["urg"] = map_unique(rooms["ai_urgency"], urgency_value)
    rooms["purchase"] = flag_eq(rooms["ai_intent"], "purchase")
    base = leads[["lead_id", "contact_key", "created_at"]].merge(rooms, on="contact_key", how="inner")
    base = base[base["room_at"] < base["created_at"]]
    if base.empty:
        return _empty_conv(leads)
    # outcome-type fields are only known once the room has closed before the opt-in moment
    closed_known = base["closed_at"].notna() & (base["closed_at"] <= base["created_at"])
    base["fr_known"] = base["first_response_s"].where(closed_known)
    base["csat_known"] = base["csat"].where(closed_known)
    g = base.groupby("lead_id")
    feats = pd.DataFrame(
        {
            "prior_rooms": g.size(),
            "prior_intent_purchase": g["purchase"].max(),
            "prior_sentiment_mean": g["sent"].mean(),
            "prior_urgency_max": g["urg"].max(),
            "prior_first_response_mean_s": g["fr_known"].mean(),
            "prior_csat_mean": g["csat_known"].mean(),
        }
    )
    out = leads.merge(feats, left_on="lead_id", right_index=True, how="left")
    out["prior_rooms"] = out["prior_rooms"].fillna(0)
    out["prior_intent_purchase"] = out["prior_intent_purchase"].fillna(0)
    return out
