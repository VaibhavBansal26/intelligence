"""Segment discovery: KMeans on standardised contact-profile features with rule-based names and descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

LOG_COLS = ["f_revenue", "f_orders", "f_page_views", "f_steps_reached", "c_rooms", "c_open_rooms", "c_purchase_intent_rooms", "c_reopens"]
EXTRA_COLS = ["c_avg_sentiment", "recency_days", "in_funnels", "in_conversations"]
FEATURES = LOG_COLS + EXTRA_COLS


def segment_features(profiles: pd.DataFrame, as_of: int) -> pd.DataFrame:
    x = pd.DataFrame(index=profiles.index)
    for c in LOG_COLS:
        x[c] = np.log1p(profiles[c].fillna(0).astype(float))
    x["c_avg_sentiment"] = profiles["c_avg_sentiment"].fillna(0.0).astype(float)
    last = profiles[["f_last_seen", "c_last_room_at"]].max(axis=1)
    x["recency_days"] = np.log1p(((as_of - last.fillna(as_of - 365 * 86400)) / 86400).clip(lower=0))
    x["in_funnels"] = profiles["in_funnels"].astype(float)
    x["in_conversations"] = profiles["in_conversations"].astype(float)
    return x[FEATURES]


@dataclass
class SegmentModel:
    scaler: StandardScaler | None
    kmeans: KMeans | None
    names: dict[int, str]
    method: str  # kmeans | rules

    def assign(self, profiles: pd.DataFrame, as_of: int) -> np.ndarray:
        if self.method == "rules" or self.kmeans is None or self.scaler is None:
            return rule_assign(profiles)
        return np.asarray(self.kmeans.predict(self.scaler.transform(segment_features(profiles, as_of))))


RULE_NAMES = {0: "Customers (have ordered)", 1: "Leads (opted in, not ordered)", 2: "Support-only contacts"}


def rule_assign(profiles: pd.DataFrame) -> np.ndarray:
    orders = profiles["f_orders"].fillna(0)
    optins = profiles["f_optins"].fillna(0) + profiles["n_leads"].fillna(0)
    return np.where(orders > 0, 0, np.where(optins > 0, 1, 2))


def fit_segments(profiles: pd.DataFrame, as_of: int, seed: int, min_contacts: int, k_range: tuple[int, int] = (3, 6)) -> tuple[SegmentModel, dict[str, Any]]:
    n = len(profiles)
    if n < min_contacts:
        return SegmentModel(None, None, dict(RULE_NAMES), "rules"), {"method": "rules", "contacts": n, "cold_start_reason": f"only {n} contacts (< {min_contacts}); using rule-based segments"}
    x = segment_features(profiles, as_of)
    scaler = StandardScaler().fit(x)
    xs = scaler.transform(x)
    rng = np.random.default_rng(seed)
    sample = rng.choice(n, size=min(n, 3000), replace=False)
    best: tuple[float, int, KMeans] | None = None
    scores: dict[int, float] = {}
    for k in range(k_range[0], min(k_range[1], max(2, n // 20)) + 1):
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(xs)
        labels = km.labels_
        if len(set(labels[sample])) < 2:
            continue
        sil = float(silhouette_score(xs[sample], labels[sample]))
        scores[k] = round(sil, 4)
        if best is None or sil > best[0]:
            best = (sil, k, km)
    if best is None:
        return SegmentModel(None, None, dict(RULE_NAMES), "rules"), {"method": "rules", "contacts": n, "cold_start_reason": "clustering degenerate; using rule-based segments"}
    sil, k, km = best
    labels = km.labels_
    names = name_clusters(profiles, labels, k)
    return SegmentModel(scaler, km, names, "kmeans"), {"method": "kmeans", "k": k, "silhouette": round(sil, 4), "silhouette_by_k": scores, "contacts": n}


def name_clusters(profiles: pd.DataFrame, labels: np.ndarray, k: int) -> dict[int, str]:
    overall_rev = float(profiles["f_revenue"].fillna(0).mean()) or 1e-9
    stats: dict[int, dict[str, float]] = {}
    for c in range(k):
        g = profiles[labels == c]
        stats[c] = {
            "rev": float(g["f_revenue"].fillna(0).mean()), "orders": float(g["f_orders"].fillna(0).mean()), "rooms": float(g["c_rooms"].fillna(0).mean()),
            "open": float(g["c_open_rooms"].fillna(0).mean()), "buy_intent": float(g["c_purchase_intent_rooms"].fillna(0).mean()),
            "steps": float(g["f_steps_reached"].fillna(0).mean()), "funnel_share": float(g["in_funnels"].mean()), "sent": float(g["c_avg_sentiment"].fillna(0).mean()),
        }
    names: dict[int, str] = {}
    top_rev = max(stats, key=lambda c: stats[c]["rev"])
    for c, s in stats.items():
        if s["rev"] > 2 * overall_rev and s["orders"] >= 1 and c == top_rev:
            nm = "High-value buyers"
        elif s["orders"] >= 0.5:
            nm = "Repeat or recent buyers"
        elif s["buy_intent"] >= 0.3 and s["rev"] < overall_rev:
            nm = "Hot prospects (asked about buying)"
        elif s["rooms"] >= 1.5 and s["funnel_share"] < 0.5:
            nm = "Support-heavy contacts"
        elif s["sent"] <= -0.3 and s["rooms"] >= 0.5:
            nm = "Unhappy contacts"
        elif s["steps"] >= 2:
            nm = "Engaged leads, not yet buying"
        else:
            nm = "Low-engagement contacts"
        names[c] = nm
    seen: dict[str, int] = {}
    for c in sorted(names):  # make names unique
        base = names[c]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            names[c] = f"{base} ({seen[base]})"
    return names


def summarise(profiles: pd.DataFrame, labels: np.ndarray, names: dict[int, str]) -> list[dict[str, Any]]:
    total = len(profiles)
    total_rev = float(profiles["f_revenue"].fillna(0).sum()) or 1e-9
    rows: list[dict[str, Any]] = []
    for c in sorted(set(labels.tolist())):
        g = profiles[labels == c]
        rev = float(g["f_revenue"].fillna(0).sum())
        avg = {
            "orders": float(g["f_orders"].fillna(0).mean()), "rooms": float(g["c_rooms"].fillna(0).mean()),
            "funnel_steps": float(g["f_steps_reached"].fillna(0).mean()),
        }
        name = names.get(int(c), f"Segment {c}")
        rows.append({
            "segment_id": int(c), "name": name, "size": int(len(g)), "share": round(len(g) / total, 4) if total else 0.0,
            "revenue": round(rev, 2), "revenue_share": round(rev / total_rev, 4), "avg_revenue_per_contact": round(rev / max(len(g), 1), 2),
            "avg_orders": round(avg["orders"], 2), "avg_rooms": round(avg["rooms"], 2), "avg_funnel_steps": round(avg["funnel_steps"], 2),
            "description": f"{name}: {len(g)} contacts ({100 * len(g) / max(total, 1):.0f}% of contacts), {avg['orders']:.2f} orders and {avg['rooms']:.2f} conversations on average, {100 * rev / total_rev:.0f}% of revenue.",
        })
    rows.sort(key=lambda r: (-r["revenue"], -r["size"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows
