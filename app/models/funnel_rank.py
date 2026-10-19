"""Funnel and step ranking by expected revenue uplift if the weakest steps reached the benchmark median.

Each step has a pass rate (share of its unique visitors that reach the next step; for the final step, that
place an order). Raw rates of low-traffic steps are noise, so rates are shrunk with an empirical-Bayes Beta
prior fitted per step role (entry / middle / final), optionally centred on a content-aware ridge prediction.
Uncertainty comes from Beta-posterior bootstrap draws.
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from app.features.stats import BetaPrior, fit_beta_prior, wilson_interval

MIN_PASSERS_FOR_RPP = 5
MIN_GROUP_FOR_BENCHMARK = 3
MIN_GROUP_FOR_CONTENT_PRIOR = 6
CONTENT_FEATURES = ["log_words", "has_form", "has_video", "has_countdown", "has_bump", "has_upsell"]


def _role(i: int, last: int) -> str:
    if i == 0:
        return "entry"
    return "final" if i == last else "middle"


def _rng_for(seed: int, *parts: str) -> np.random.Generator:
    h = hashlib.sha256("|".join(parts).encode()).digest()
    return np.random.default_rng([seed, int.from_bytes(h[:4], "big")])


def step_frame(steps: pd.DataFrame, funnels: pd.DataFrame) -> pd.DataFrame:
    """Turn funnel_steps rows into trials/successes per step (`steps` has source_id, funnel_id, ord, ...)."""
    rows: list[dict[str, Any]] = []
    fgross = {(r.source_id, r.funnel_id): float(r.gross) for r in funnels.itertuples()} if not funnels.empty else {}
    for (src, fid), g in steps.sort_values("ord").groupby(["source_id", "funnel_id"]):
        g = g.reset_index(drop=True)
        last = len(g) - 1
        for i, r in g.iterrows():
            i = int(i)
            trials = float(r["unique_visits"])
            if trials <= 0:
                continue
            passers = float(g.loc[i + 1, "unique_visits"]) if i < last else float(r["orders"])
            passers = min(passers, trials)
            downstream = float(g.loc[i + 1 :, "gross"].sum()) if i < last else float(r["gross"])
            if downstream <= 0:
                downstream = fgross.get((src, fid), 0.0)
            rows.append({
                "source_id": src, "funnel_id": fid, "step_id": r["step_id"], "step_name": r["name"], "ord": int(r["ord"]), "role": _role(i, last),
                "n": trials, "passers": passers, "downstream_gross": downstream, "step_gross": float(r["gross"]),
            })
    return pd.DataFrame(rows)


def _content_matrix(df: pd.DataFrame, structure: pd.DataFrame) -> pd.DataFrame | None:
    if structure.empty:
        return None
    st = structure.copy()
    feats = pd.DataFrame({
        "source_id": st["source_id"], "funnel_id": st["funnel_id"], "step_id": st["step_id"],
        "log_words": np.log1p(st["word_count"].astype(float)), "has_form": (st["n_form"] > 0).astype(float),
        "has_video": (st["n_video"] > 0).astype(float), "has_countdown": (st["n_countdown"] > 0).astype(float),
        "has_bump": st["has_order_bump"].astype(float), "has_upsell": st["has_upsell"].astype(float),
    })
    return df.merge(feats, on=["source_id", "funnel_id", "step_id"], how="left")


def content_hints(row: pd.Series) -> list[dict[str, Any]]:
    """Rule-based, content-aware hints (no free text from the source is echoed except via the step name)."""
    hints: list[dict[str, Any]] = []
    words = float(row.get("word_count", np.nan)) if "word_count" in row else float("nan")
    if not np.isnan(words) and words > 1500 and not row.get("n_video", 0):
        hints.append({"code": "long_copy_no_video", "word_count": int(words)})
    if row.get("step_type") == "sales" and not row.get("n_video", 0):
        hints.append({"code": "sales_without_video"})
    if row.get("step_type") == "checkout" and not row.get("has_order_bump", False):
        hints.append({"code": "checkout_without_order_bump"})
    if row.get("step_type") in ("sales", "checkout") and not row.get("n_countdown", 0):
        hints.append({"code": "no_urgency_element"})
    if row.get("n_form", 0) and row["n_form"] >= 2:
        hints.append({"code": "multiple_forms", "forms": int(row["n_form"])})
    return hints


def rank_steps(
    steps: pd.DataFrame,
    funnels: pd.DataFrame,
    structure: pd.DataFrame | None = None,
    *,
    seed: int = 1234,
    n_boot: int = 1000,
    benchmark_override: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return {"steps": [...ranked...], "funnels": [...ranked...], "notes": [...], "method": ...}."""
    structure = structure if structure is not None else pd.DataFrame()
    sf: pd.DataFrame = step_frame(steps, funnels) if not steps.empty else pd.DataFrame()
    if sf.empty:
        return {"steps": [], "funnels": [], "method": "none", "notes": ["no funnel step data synced yet"], "priors": {}}
    notes: list[str] = []
    if not structure.empty:
        merged = _content_matrix(sf, structure)
        sf = merged if merged is not None else sf
    has_content = "log_words" in sf.columns
    sf["rate"] = sf["passers"] / sf["n"]
    sf["prior_alpha"] = np.nan
    sf["prior_beta"] = np.nan
    priors: dict[str, dict[str, float]] = {}
    for role, g in sf.groupby("role"):
        prior = fit_beta_prior(g["passers"], g["n"])
        centre = np.full(len(g), prior.mean)
        used_content = False
        if has_content and len(g) >= MIN_GROUP_FOR_CONTENT_PRIOR and g[CONTENT_FEATURES].notna().all(axis=1).sum() >= MIN_GROUP_FOR_CONTENT_PRIOR:
            ok = g[CONTENT_FEATURES].notna().all(axis=1)
            x = g.loc[ok, CONTENT_FEATURES].to_numpy(dtype=float)
            r = np.clip(g.loc[ok, "rate"].to_numpy(), 0.01, 0.99)
            w = np.minimum(g.loc[ok, "n"].to_numpy(), 500.0)
            model = Ridge(alpha=10.0).fit(x, np.log(r / (1 - r)), sample_weight=w)
            pred = 1.0 / (1.0 + np.exp(-model.predict(x)))
            # shrink the content-based centre towards the plain group mean: content is a weak, noisy signal
            centre_ok = 0.5 * pred + 0.5 * prior.mean
            centre[ok.to_numpy()] = centre_ok
            used_content = True
        strength = prior.strength
        sf.loc[g.index, "prior_alpha"] = centre * strength
        sf.loc[g.index, "prior_beta"] = (1 - centre) * strength
        priors[str(role)] = {"mean": round(prior.mean, 4), "strength": round(strength, 2), "content_aware": used_content, "steps": int(len(g))}
    sf["shrunk_rate"] = (sf["prior_alpha"] + sf["passers"]) / (sf["prior_alpha"] + sf["prior_beta"] + sf["n"])
    # benchmark = median of shrunk rates among peers with the same role (or an explicit override)
    bench: dict[str, float | None] = {}
    for role, g in sf.groupby("role"):
        if benchmark_override and role in benchmark_override:
            bench[str(role)] = float(benchmark_override[role])
        elif len(g) >= MIN_GROUP_FOR_BENCHMARK:
            bench[str(role)] = float(g["shrunk_rate"].median())
        else:
            bench[str(role)] = None
            notes.append(f"fewer than {MIN_GROUP_FOR_BENCHMARK} '{role}' steps: no benchmark, no uplift claimed for them")
    out_rows: list[dict[str, Any]] = []
    structure_idx = structure.set_index(["source_id", "funnel_id", "step_id"]) if not structure.empty else None
    for r in sf.itertuples(index=False):
        b = bench.get(r.role)
        lo, hi = wilson_interval(r.passers, r.n)
        rpp = r.downstream_gross / max(r.passers, MIN_PASSERS_FOR_RPP)
        if b is None:
            uplift = {"mean": 0.0, "p10": 0.0, "p50": 0.0, "p90": 0.0}
            extra = 0.0
        else:
            rng = _rng_for(seed, r.source_id, r.funnel_id, r.step_id)
            draws = rng.beta(r.prior_alpha + r.passers, r.prior_beta + r.n - r.passers, size=n_boot)
            vals = r.n * np.clip(b - draws, 0, None) * rpp
            uplift = {"mean": float(vals.mean()), "p10": float(np.percentile(vals, 10)), "p50": float(np.percentile(vals, 50)), "p90": float(np.percentile(vals, 90))}
            extra = float(r.n * max(0.0, b - r.shrunk_rate))
        row: dict[str, Any] = {
            "source_id": r.source_id, "funnel_id": r.funnel_id, "step_id": r.step_id, "step_name": r.step_name, "order": int(r.ord), "role": r.role,
            "visitors": int(r.n), "passed": int(r.passers), "raw_rate": round(float(r.rate), 4), "shrunk_rate": round(float(r.shrunk_rate), 4),
            "rate_ci95": [round(lo, 4), round(hi, 4)], "benchmark_rate": None if b is None else round(b, 4),
            "extra_passers": round(extra, 1), "revenue_per_passer": round(float(rpp), 2),
            "expected_uplift": round(uplift["mean"], 2), "uplift_p10": round(uplift["p10"], 2), "uplift_p50": round(uplift["p50"], 2), "uplift_p90": round(uplift["p90"], 2),
            "confidence": "high" if r.n >= 500 else "medium" if r.n >= 100 else "low",
        }
        if structure_idx is not None and (r.source_id, r.funnel_id, r.step_id) in structure_idx.index:
            srow = structure_idx.loc[(r.source_id, r.funnel_id, r.step_id)]
            if isinstance(srow, pd.DataFrame):
                srow = srow.iloc[0]
            row["content"] = {"type": srow["step_type"], "word_count": int(srow["word_count"]), "forms": int(srow["n_form"]), "videos": int(srow["n_video"]), "countdowns": int(srow["n_countdown"]), "order_bump": bool(srow["has_order_bump"]), "upsell": bool(srow["has_upsell"])}
            row["content_hints"] = content_hints(pd.Series({"word_count": srow["word_count"], "n_video": srow["n_video"], "step_type": srow["step_type"], "has_order_bump": srow["has_order_bump"], "n_countdown": srow["n_countdown"], "n_form": srow["n_form"]}))
        out_rows.append(row)
    ranked = sorted(out_rows, key=lambda x: (-x["expected_uplift"], -x["visitors"], x["step_id"]))
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    funnels_out = _rank_funnels(ranked, funnels)
    return {"steps": ranked, "funnels": funnels_out, "method": "empirical_bayes_beta_bootstrap", "notes": notes, "priors": priors, "benchmarks": {k: v for k, v in bench.items()}}


def _rank_funnels(ranked_steps: list[dict[str, Any]], funnels: pd.DataFrame) -> list[dict[str, Any]]:
    by_funnel: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in ranked_steps:
        by_funnel.setdefault((s["source_id"], s["funnel_id"]), []).append(s)
    meta = {(r.source_id, r.funnel_id): r for r in funnels.itertuples()} if not funnels.empty else {}
    rows: list[dict[str, Any]] = []
    for key, steps in by_funnel.items():
        weakest = max(steps, key=lambda s: s["expected_uplift"])  # steps are already sorted by uplift globally
        f = meta.get(key)
        rows.append({
            "source_id": key[0], "funnel_id": key[1], "funnel_name": getattr(f, "name", key[1]), "gross": round(float(getattr(f, "gross", 0.0)), 2),
            "visits": int(getattr(f, "unique_visits", 0) or 0), "orders": int(getattr(f, "orders", 0) or 0),
            "weakest_step_id": weakest["step_id"], "weakest_step_name": weakest["step_name"], "weakest_step_rank": weakest["rank"],
            "expected_uplift": weakest["expected_uplift"], "uplift_p10": weakest["uplift_p10"], "uplift_p90": weakest["uplift_p90"],
            "confidence": weakest["confidence"],
        })
    rows.sort(key=lambda r: (-r["expected_uplift"], -r["gross"], r["funnel_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


__all__ = ["rank_steps", "step_frame", "BetaPrior"]
