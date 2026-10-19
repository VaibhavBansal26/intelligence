"""Open-conversation prioritisation: blends the SLA-breach model with explicit, inspectable business rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.models.scorers import Scorer, explain_by_occlusion


@dataclass(frozen=True)
class PriorityWeights:
    breach: float = 0.35
    urgency: float = 0.20
    sentiment: float = 0.15
    waiting: float = 0.15
    vip: float = 0.10
    reopen: float = 0.05
    breached_floor: float = 60.0
    vip_negative_bump: float = 10.0


def rank_open_rooms(
    open_rooms: pd.DataFrame, scorer: Scorer, *, as_of: int, sla_s: float, vip_threshold: float, weights: PriorityWeights | None = None, limit: int = 50, explain: bool = True,
) -> list[dict[str, Any]]:
    w = weights or PriorityWeights()
    if open_rooms.empty:
        return []
    df = open_rooms.copy()
    p = scorer.predict(df)
    wait_s = (as_of - df["created_at"]).clip(lower=0).to_numpy(dtype=float)
    no_reply = df["first_response_s"].isna().to_numpy()
    wait_ratio = np.minimum(np.where(no_reply, wait_s, 0.5 * wait_s) / sla_s, 3.0) / 3.0
    urg = df["urgency"].fillna(0.3).to_numpy(dtype=float)
    neg = np.clip(-df["sentiment"].fillna(0.0).to_numpy(dtype=float), 0, 1)
    vip = ((df["funnel_revenue_before"].fillna(0) >= vip_threshold) | (df["funnel_orders_before"].fillna(0) >= 3)).to_numpy()
    reopen = np.minimum(df["reopen_count"].fillna(0).to_numpy(dtype=float) / 3.0, 1.0)
    raw = w.breach * p + w.urgency * urg + w.sentiment * neg + w.waiting * wait_ratio + w.vip * vip + w.reopen * reopen
    score = 100.0 * raw
    breached = no_reply & (wait_s > sla_s)
    score = np.where(breached, np.maximum(score, w.breached_floor), score)
    score = np.where(vip & (neg > 0.3), score + w.vip_negative_bump, score)
    score = np.clip(score, 0, 100)
    churn = np.clip(0.45 * neg + 0.2 * reopen + 0.2 * wait_ratio + 0.15 * vip, 0, 1)
    order = np.argsort(-score, kind="stable")[:limit]
    top = df.iloc[order]
    factors = explain_by_occlusion(scorer, top, top_k=3) if explain else [[] for _ in order]
    items: list[dict[str, Any]] = []
    for rank, (pos, row) in enumerate(zip(order, top.itertuples(index=False), strict=True), 1):
        reasons: list[str] = []
        if breached[pos]:
            reasons.append(f"waiting {wait_s[pos] / 3600:.1f}h with no first response (SLA {sla_s / 3600:.1f}h)")
        elif no_reply[pos]:
            reasons.append(f"no first response yet, waiting {wait_s[pos] / 3600:.1f}h")
        if neg[pos] > 0.3:
            reasons.append(f"negative sentiment ({-neg[pos]:.2f})")
        if urg[pos] >= 0.7:
            reasons.append(f"high urgency ({urg[pos]:.2f})")
        if vip[pos]:
            reasons.append(f"VIP contact: {float(row.funnel_revenue_before):.0f} lifetime revenue from funnels")
        if reopen[pos] > 0:
            reasons.append(f"reopened {int(row.reopen_count)} times")
        reasons.append(f"model P(SLA breach) = {p[pos]:.2f} ({scorer.method})")
        items.append({
            "rank": rank, "room_id": row.room_id, "source_id": row.source_id, "contact_key": row.contact_key, "inbox_id": row.inbox_id,
            "priority_score": round(float(score[pos]), 1), "sla_breach_probability": round(float(p[pos]), 4), "churn_risk": round(float(churn[pos]), 3),
            "waiting_hours": round(float(wait_s[pos] / 3600), 2), "vip": bool(vip[pos]), "sla_breached": bool(breached[pos]),
            "components": {"breach_model": round(float(w.breach * p[pos] * 100), 1), "urgency": round(float(w.urgency * urg[pos] * 100), 1), "sentiment": round(float(w.sentiment * neg[pos] * 100), 1), "waiting": round(float(w.waiting * wait_ratio[pos] * 100), 1), "vip": round(float(w.vip * vip[pos] * 100), 1), "reopen": round(float(w.reopen * reopen[pos] * 100), 1)},
            "reasons": reasons, "model_factors": factors[rank - 1],
        })
    return items
