"""Unified contact profiles joined across the Funnels and Conversations sources, plus join diagnostics."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Connection, case, distinct, func, select
from sqlalchemy.orm import Session

from app.db import models as m
from app.features.norm import flag_eq, map_unique, sentiment_value, urgency_value

PROFILE_COLUMNS = [
    "contact_key",
    "in_funnels",
    "in_conversations",
    "f_first_seen",
    "f_last_seen",
    "f_events",
    "f_page_views",
    "f_steps_reached",
    "f_funnels",
    "f_optins",
    "f_orders",
    "f_revenue",
    "n_leads",
    "utm_source",
    "c_rooms",
    "c_open_rooms",
    "c_avg_first_response_s",
    "c_avg_sentiment",
    "c_avg_urgency",
    "c_purchase_intent_rooms",
    "c_reopens",
    "c_avg_csat",
    "c_last_room_at",
]


def _read(conn: Connection, stmt: Any) -> pd.DataFrame:
    return pd.read_sql(stmt, conn)


def funnel_profile(conn: Connection, account_id: str) -> pd.DataFrame:
    e = m.Event
    stmt = (
        select(
            e.contact_key,
            func.min(e.ts).label("f_first_seen"),
            func.max(e.ts).label("f_last_seen"),
            func.count().label("f_events"),
            func.sum(case((e.type == "page_view", 1), else_=0)).label("f_page_views"),
            func.count(distinct(e.step_id)).label("f_steps_reached"),
            func.count(distinct(e.funnel_id)).label("f_funnels"),
            func.sum(case((e.type == "optin", 1), else_=0)).label("f_optins"),
            func.sum(case((e.type == "order", 1), else_=0)).label("f_orders"),
            func.coalesce(func.sum(case((e.type == "order", e.amount), else_=0.0)), 0.0).label("f_revenue"),
        )
        .where(e.account_id == account_id, e.contact_key.is_not(None))
        .group_by(e.contact_key)
    )
    df = _read(conn, stmt)
    lead = m.Lead
    leads = _read(conn, select(lead.contact_key, lead.created_at, lead.utm_source).where(lead.account_id == account_id))
    if not leads.empty:
        first = leads.sort_values("created_at").drop_duplicates("contact_key")[["contact_key", "utm_source"]]
        counts = leads.groupby("contact_key").size().rename("n_leads").reset_index()
        lp = first.merge(counts, on="contact_key")
        df = df.merge(lp, on="contact_key", how="outer")
    else:
        df["n_leads"] = 0
        df["utm_source"] = None
    return df


def conversation_profile(conn: Connection, account_id: str) -> pd.DataFrame:
    r = m.Room
    rooms = _read(
        conn,
        select(
            r.contact_key,
            r.created_at,
            r.closed_at,
            r.ai_intent,
            r.ai_sentiment,
            r.ai_urgency,
            r.first_response_s,
            r.reopen_count,
            r.csat,
        ).where(r.account_id == account_id, r.contact_key.is_not(None)),
    )
    if rooms.empty:
        return pd.DataFrame(
            columns=[
                "contact_key",
                "c_rooms",
                "c_open_rooms",
                "c_avg_first_response_s",
                "c_avg_sentiment",
                "c_avg_urgency",
                "c_purchase_intent_rooms",
                "c_reopens",
                "c_avg_csat",
                "c_last_room_at",
            ]
        )
    rooms["sent"] = map_unique(rooms["ai_sentiment"], sentiment_value)
    rooms["urg"] = map_unique(rooms["ai_urgency"], urgency_value)
    rooms["is_open"] = rooms["closed_at"].isna().astype(int)
    rooms["purchase"] = flag_eq(rooms["ai_intent"], "purchase")
    g = rooms.groupby("contact_key")
    return pd.DataFrame(
        {
            "c_rooms": g.size(),
            "c_open_rooms": g["is_open"].sum(),
            "c_avg_first_response_s": g["first_response_s"].mean(),
            "c_avg_sentiment": g["sent"].mean(),
            "c_avg_urgency": g["urg"].mean(),
            "c_purchase_intent_rooms": g["purchase"].sum(),
            "c_reopens": g["reopen_count"].sum(),
            "c_avg_csat": g["csat"].mean(),
            "c_last_room_at": g["created_at"].max(),
        }
    ).reset_index()


def build_contact_profiles(session: Session, account_id: str) -> pd.DataFrame:
    """One row per contact_key, outer-joined across sources (single-source accounts simply have NaN blocks)."""
    conn = session.connection()
    f = funnel_profile(conn, account_id)
    c = conversation_profile(conn, account_id)
    f["in_funnels"] = True
    c["in_conversations"] = True
    if f.empty and c.empty:
        return pd.DataFrame(columns=PROFILE_COLUMNS)
    if f.empty:
        df = c
    elif c.empty:
        df = f
    else:
        df = f.merge(c, on="contact_key", how="outer")
    for col in PROFILE_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan if col != "utm_source" else None
    for flag in ("in_funnels", "in_conversations"):
        df[flag] = df[flag].fillna(False).astype(bool)
    for col in (
        "f_events",
        "f_page_views",
        "f_steps_reached",
        "f_funnels",
        "f_optins",
        "f_orders",
        "f_revenue",
        "n_leads",
        "c_rooms",
        "c_open_rooms",
        "c_purchase_intent_rooms",
        "c_reopens",
    ):
        df[col] = df[col].fillna(0)
    return df[PROFILE_COLUMNS].reset_index(drop=True)


def columnar_funnel_profile(lf: Any, cap: int) -> pd.DataFrame:
    """Funnel profile from the Parquet store, deterministically down-sampled (by contact hash) to at most `cap` rows
    so memory stays bounded however many contacts exist. Columns match `funnel_profile` (plus `contact_h`)."""
    import polars as pl

    n = int(lf.select(pl.len()).collect().item())
    if n == 0:
        return pd.DataFrame(columns=["contact_key", "contact_h"])
    if cap and n > cap:
        lf = lf.filter(pl.col("contact_h") % ((n + cap - 1) // cap) == 0)
    df = lf.collect().to_pandas()
    df["contact_key"] = df["contact_h"].map(lambda h: f"{int(h):016x}")
    df = df.rename(columns={})
    df["n_leads"] = 0
    df["utm_source"] = None
    return df


def build_contact_profiles_columnar(session: Session, account_id: str, lf: Any, cap: int) -> pd.DataFrame:
    """Same output as `build_contact_profiles`, with the funnel side read out-of-core from Parquet."""
    f = columnar_funnel_profile(lf, cap)
    c = conversation_profile(session.connection(), account_id)
    f["in_funnels"] = True
    c["in_conversations"] = True
    if not c.empty:
        c["contact_key"] = c["contact_key"].map(lambda k: f"{int(str(k)[:16], 16):016x}")
    if f.empty and c.empty:
        return pd.DataFrame(columns=PROFILE_COLUMNS)
    df = c if f.empty else (f if c.empty else f.merge(c, on="contact_key", how="outer"))
    for col in PROFILE_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan if col != "utm_source" else None
    for flag in ("in_funnels", "in_conversations"):
        df[flag] = df[flag].fillna(False).astype(bool)
    for col in (
        "f_events",
        "f_page_views",
        "f_steps_reached",
        "f_funnels",
        "f_optins",
        "f_orders",
        "f_revenue",
        "n_leads",
        "c_rooms",
        "c_open_rooms",
        "c_purchase_intent_rooms",
        "c_reopens",
    ):
        df[col] = df[col].fillna(0)
    return df[PROFILE_COLUMNS].reset_index(drop=True)


def join_report(session: Session, account_id: str) -> dict[str, Any]:
    """Coverage of the cross-app identity join, with explicit pepper-mismatch diagnostics."""
    conn = session.connection()
    sources = (
        session.execute(select(m.Source).where(m.Source.account_id == account_id, m.Source.status == "active")).scalars().all()
    )
    kinds = {s.kind for s in sources}
    fkeys = set(
        _read(
            conn, select(m.Event.contact_key).distinct().where(m.Event.account_id == account_id, m.Event.contact_key.is_not(None))
        ).iloc[:, 0]
    )
    fkeys |= set(_read(conn, select(m.Lead.contact_key).distinct().where(m.Lead.account_id == account_id)).iloc[:, 0])
    ckeys = set(
        _read(
            conn, select(m.Room.contact_key).distinct().where(m.Room.account_id == account_id, m.Room.contact_key.is_not(None))
        ).iloc[:, 0]
    )
    overlap = len(fkeys & ckeys)
    warnings: list[str] = []
    fps = {s.kind: s.pepper_fp for s in sources}
    if len({s.pepper_fp for s in sources}) > 1 and {"funnels", "conversations"} <= kinds:
        warnings.append(
            "pepper_fingerprint_mismatch: the funnels and conversations sources are configured with different identity peppers; contacts cannot be joined"
        )
    smaller = min(len(fkeys), len(ckeys))
    coverage = overlap / smaller if smaller else None
    if {"funnels", "conversations"} <= kinds and len(fkeys) >= 50 and len(ckeys) >= 50 and (coverage or 0) < 0.01:
        warnings.append(
            "pepper_mismatch_suspected: almost no contact_key appears in both apps; verify both sources use the same identity pepper"
        )
    lead_dupes = _read(
        conn,
        select(m.Lead.contact_key, func.count().label("n"))
        .where(m.Lead.account_id == account_id)
        .group_by(m.Lead.contact_key)
        .having(func.count() > 1),
    )
    return {
        "sources": sorted(kinds),
        "single_source": len(kinds) < 2,
        "funnel_contacts": len(fkeys),
        "conversation_contacts": len(ckeys),
        "joined_contacts": overlap,
        "coverage_of_smaller_side": None if coverage is None else round(coverage, 4),
        "coverage_funnels_side": round(overlap / len(fkeys), 4) if fkeys else None,
        "coverage_conversations_side": round(overlap / len(ckeys), 4) if ckeys else None,
        "duplicate_lead_keys": len(lead_dupes),
        "pepper_fingerprints": fps,
        "warnings": warnings,
    }
