"""Population-stability-index drift checks of recent data against the training reference."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.features.stats import psi_against_reference, psi_from_proportions
from app.models.scorers import GbmScorer


def drift_report(scorer: Any, reference: dict[str, Any], recent: pd.DataFrame, *, warn: float, alert: float) -> dict[str, Any]:
    if not isinstance(scorer, GbmScorer) or not reference:
        return {"status": "not_applicable", "reason": "heuristic or reference-less model", "features": []}
    if len(recent) < 30:
        return {"status": "insufficient_data", "reason": f"only {len(recent)} recent rows", "features": []}
    enc = scorer.encoder
    x = enc.transform(recent)
    feats: list[dict[str, Any]] = []
    for j, c in enumerate(enc.numeric):
        ref = reference.get("numeric", {}).get(c)
        if ref:
            feats.append({"feature": c, "psi": round(psi_against_reference(ref, x[:, j]), 4)})
    for c in enc.categorical:
        ref_p = reference.get("categorical", {}).get(c)
        if not ref_p:
            continue
        cur = recent[c].astype("string").fillna("__missing__").value_counts(normalize=True)
        cats = sorted(set(ref_p) | set(map(str, cur.index)))
        feats.append({"feature": c, "psi": round(psi_from_proportions([ref_p.get(k, 0.0) for k in cats], [float(cur.get(k, 0.0)) for k in cats]), 4)})
    worst = max((f["psi"] for f in feats), default=0.0)
    for f in feats:
        f["level"] = "alert" if f["psi"] >= alert else "warn" if f["psi"] >= warn else "ok"
    status = "alert" if worst >= alert else "warn" if worst >= warn else "ok"
    feats.sort(key=lambda f: -f["psi"])
    return {"status": status, "max_psi": round(float(worst), 4), "rows": int(len(recent)), "features": feats, "thresholds": {"warn": warn, "alert": alert}}


__all__ = ["drift_report", "np"]
