"""Room features known at creation time, SLA labels with censoring guards, and open-room frames."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import models as m
from app.features.norm import map_unique, sentiment_value, urgency_value

NUMERIC_FEATURES = [
    "hour",
    "dow",
    "priority",
    "sentiment",
    "urgency",
    "prior_rooms",
    "prior_avg_first_response_s",
    "funnel_revenue_before",
    "funnel_orders_before",
]
CATEGORICAL_FEATURES = ["inbox_id", "channel", "intent"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
# Deliberately NOT features (they only exist after/while the outcome unfolds): messages_in/out, resolution_s,
# first_response_s, reopen_count, snoozed_count, csat, status, closed_at, assignee_key.


@dataclass
class RoomFrame:
    df: pd.DataFrame
    as_of: int
    sla_s: float


def rooms_as_of(session: Session, account_id: str) -> int:
    return int(session.execute(select(func.max(m.Room.created_at)).where(m.Room.account_id == account_id)).scalar() or 0)


def build_room_frame(session: Session, account_id: str, *, sla_s: float, as_of: int | None = None) -> RoomFrame:
    conn = session.connection()
    as_of = as_of or max(rooms_as_of(session, account_id), _events_end(session, account_id))
    r = m.Room
    rooms = pd.read_sql(select(r).where(r.account_id == account_id).order_by(r.created_at, r.ext_id), conn)
    if rooms.empty:
        return RoomFrame(rooms, as_of, sla_s)
    rooms["room_id"] = rooms["source_id"] + ":" + rooms["ext_id"]
    rooms["hour"] = rooms["hour_of_day_created"]
    rooms["dow"] = rooms["dow_created"]
    rooms["sentiment"] = map_unique(rooms["ai_sentiment"], sentiment_value)
    rooms["urgency"] = map_unique(rooms["ai_urgency"], urgency_value)
    rooms["intent"] = rooms["ai_intent"]
    mix = rooms[["mix_dm", "mix_sms", "mix_email"]].to_numpy()
    rooms["channel"] = np.array(["dm", "sms", "email"])[mix.argmax(axis=1)]
    rooms["is_open"] = rooms["closed_at"].isna()
    # contact history strictly before this room
    ck = rooms["contact_key"]
    has_ck = ck.notna()
    rooms["prior_rooms"] = 0
    rooms.loc[has_ck, "prior_rooms"] = rooms[has_ck].groupby("contact_key").cumcount()
    rooms["_resp_at"] = rooms["created_at"] + rooms["first_response_s"]
    rooms["prior_avg_first_response_s"] = _prior_mean_response(rooms)
    rev, orders = _funnel_before(conn, account_id, rooms)
    rooms["funnel_revenue_before"], rooms["funnel_orders_before"] = rev, orders
    first = rooms["first_response_s"]
    age = as_of - rooms["created_at"]
    breach_known = first.notna() | (age > sla_s)
    rooms["breach"] = np.where(first.notna(), first > sla_s, True).astype(int)
    rooms["label_known"] = breach_known
    return RoomFrame(rooms, as_of, sla_s)


def _events_end(session: Session, account_id: str) -> int:
    return int(session.execute(select(func.max(m.Event.ts)).where(m.Event.account_id == account_id)).scalar() or 0)


def _prior_mean_response(rooms: pd.DataFrame) -> pd.Series:
    """Mean first-response time over the contact's earlier rooms that were already answered at creation."""
    out = pd.Series(np.nan, index=rooms.index, dtype="float64")
    # tiny per-contact groups: do an explicit pass only over contacts that have more than one room
    counts = rooms["contact_key"].value_counts()
    repeat_keys = counts[counts > 1].index
    for _, grp in rooms[rooms["contact_key"].isin(repeat_keys)].groupby("contact_key"):
        created = grp["created_at"].to_numpy()
        resp_at = grp["_resp_at"].to_numpy(dtype=float)
        resp = grp["first_response_s"].to_numpy(dtype=float)
        for i, idx in enumerate(grp.index):
            mask = (resp_at[:i] <= created[i]) & ~np.isnan(resp[:i])
            if mask.any():
                out.at[idx] = float(resp[:i][mask].mean())
    return out


def _funnel_before(conn: object, account_id: str, rooms: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    e = m.Event
    orders = pd.read_sql(
        select(e.contact_key, e.ts, e.amount)
        .where(e.account_id == account_id, e.type == "order", e.contact_key.is_not(None))
        .order_by(e.ts),
        conn,
    )
    zeros = pd.Series(0.0, index=rooms.index)
    if orders.empty:
        return zeros, zeros.copy()
    orders["amount"] = orders["amount"].fillna(0.0)
    orders["cum_rev"] = orders.groupby("contact_key")["amount"].cumsum()
    orders["cum_n"] = orders.groupby("contact_key").cumcount() + 1
    left = rooms[["contact_key", "created_at"]].copy()
    left["_i"] = np.arange(len(left))
    left = left[left["contact_key"].notna()].sort_values("created_at")
    right = orders.sort_values("ts")[["contact_key", "ts", "cum_rev", "cum_n"]]
    mrg = pd.merge_asof(
        left, right, left_on="created_at", right_on="ts", by="contact_key", direction="backward", allow_exact_matches=False
    )
    rev = np.zeros(len(rooms))
    cnt = np.zeros(len(rooms))
    rev[mrg["_i"].to_numpy()] = mrg["cum_rev"].fillna(0.0).to_numpy()
    cnt[mrg["_i"].to_numpy()] = mrg["cum_n"].fillna(0.0).to_numpy()
    return pd.Series(rev, index=rooms.index), pd.Series(cnt, index=rooms.index)
