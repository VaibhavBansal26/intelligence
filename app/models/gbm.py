"""Shared training pipeline for binary models: time-aware split, embargo, calibration, metrics, importances."""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from app.features.stats import lift_by_decile, reference_bins
from app.models.scorers import Calibrator, FeatureEncoder, FunctionScorer, GbmScorer, Scorer


@dataclass
class TrainSpec:
    name: str
    numeric: list[str]
    categorical: list[str]
    time_col: str
    label_col: str
    embargo_s: float
    heuristic: Any  # callable(df, base_rate) -> probs
    heuristic_baseline: dict[str, Any]
    min_rows: int
    min_class: int
    seed: int
    max_rows: int = 0
    backend: str = "hgb"
    time_budget_s: float = 120.0
    target_description: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class TrainOutcome:
    scorer: Scorer
    method: str
    metrics: dict[str, Any]
    card: dict[str, Any]
    reference: dict[str, Any]
    trained_rows: int
    cold_start_reason: str | None = None


def _estimator(backend: str, seed: int, categorical_mask: list[bool]) -> Any:
    if backend == "lightgbm":
        try:
            import lightgbm as lgb

            return lgb.LGBMClassifier(
                n_estimators=200,
                learning_rate=0.05,
                num_leaves=15,
                min_child_samples=20,
                subsample=0.8,
                random_state=seed,
                verbose=-1,
                deterministic=True,
                force_row_wise=True,
            )
        except ImportError:
            pass
    return HistGradientBoostingClassifier(
        max_iter=140,
        learning_rate=0.06,
        max_leaf_nodes=15,
        min_samples_leaf=20,
        l2_regularization=1.0,
        early_stopping=False,
        categorical_features=categorical_mask,
        random_state=seed,
    )


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            ece += m.mean() * abs(float(y[m].mean()) - float(p[m].mean()))
    return float(ece)


def heuristic_outcome(df: pd.DataFrame, spec: TrainSpec, reason: str) -> TrainOutcome:
    labelled = df[df["_trainable"]] if "_trainable" in df else df
    base = float(labelled[spec.label_col].mean()) if len(labelled) >= 20 else 0.1
    scorer = FunctionScorer(spec.heuristic, base, spec.numeric + spec.categorical, spec.heuristic_baseline, spec.name)
    metrics = {
        "method": "heuristic",
        "base_rate": round(base, 4),
        "labelled_rows": len(labelled),
        "cold_start_reason": reason,
    }
    card = _card(spec, "heuristic", metrics, df, [], reason)
    return TrainOutcome(scorer, "heuristic", metrics, card, {}, len(labelled), reason)


def train_binary(df: pd.DataFrame, spec: TrainSpec) -> TrainOutcome:
    """`df` must contain only rows whose label is final (mature); see the feature builders for leakage guards."""
    t0 = time.monotonic()
    data = df.sort_values(spec.time_col, kind="stable").reset_index(drop=True)
    if spec.max_rows and len(data) > spec.max_rows:
        data = data.tail(spec.max_rows).reset_index(drop=True)  # per-account budget: most recent rows only
    n = len(data)
    if n < spec.min_rows:
        return heuristic_outcome(data, spec, f"only {n} labelled rows (< {spec.min_rows}); using documented heuristic")
    cut = float(data[spec.time_col].quantile(0.7))
    train_all = data[data[spec.time_col] < cut - spec.embargo_s]
    val = data[data[spec.time_col] >= cut]
    y_tr_all = train_all[spec.label_col].to_numpy(dtype=int)
    y_val = val[spec.label_col].to_numpy(dtype=int)
    if min(y_tr_all.sum(), len(y_tr_all) - y_tr_all.sum(), y_val.sum(), len(y_val) - y_val.sum()) < spec.min_class:
        return heuristic_outcome(
            data, spec, f"fewer than {spec.min_class} examples of a class in train/validation; using documented heuristic"
        )
    cal_cut = float(train_all[spec.time_col].quantile(0.8))
    fit_df = train_all[train_all[spec.time_col] < cal_cut]
    cal_df = train_all[train_all[spec.time_col] >= cal_cut]
    usable = [c for c in spec.numeric if fit_df[c].notna().any()]  # e.g. conversation features on single-source accounts
    dropped = [c for c in spec.numeric if c not in usable]
    enc = FeatureEncoder(usable, spec.categorical).fit(fit_df)
    est = _estimator(spec.backend, spec.seed, enc.categorical_mask)
    x_fit = enc.transform(fit_df)
    y_fit = fit_df[spec.label_col].to_numpy(dtype=int)
    if y_fit.min() == y_fit.max():
        return heuristic_outcome(data, spec, "training slice has a single class; using documented heuristic")
    est.fit(x_fit, y_fit)
    cal = Calibrator()
    if len(cal_df):
        raw_cal = np.asarray(est.predict_proba(enc.transform(cal_df))[:, 1], dtype=float)
        cal.fit(raw_cal, cal_df[spec.label_col].to_numpy(dtype=int))
    baseline = _baseline(fit_df, enc)
    scorer = GbmScorer(est, enc, cal, baseline)
    x_val = enc.transform(val)
    p_val = scorer.predict(val)
    metrics = _metrics(y_val, p_val)
    metrics.update(
        {
            "method": "gbm",
            "dropped_all_missing_features": dropped,
            "backend": type(est).__name__,
            "calibration": cal.kind,
            "train_rows": len(fit_df),
            "calibration_rows": len(cal_df),
            "validation_rows": len(val),
            "embargo_dropped_rows": len(data[(data[spec.time_col] >= cut - spec.embargo_s) & (data[spec.time_col] < cut)]),
            "train_positive_rate": round(float(y_fit.mean()), 4),
            "validation_positive_rate": round(float(y_val.mean()), 4),
        }
    )
    imp = permutation_importance(est, x_val, y_val, scoring="roc_auc", n_repeats=5, random_state=spec.seed, n_jobs=1)
    importances: list[dict[str, Any]] = sorted(
        (
            {"feature": f, "importance": round(float(m), 5), "std": round(float(s), 5)}
            for f, m, s in zip(enc.columns, imp.importances_mean, imp.importances_std, strict=True)
        ),
        key=lambda r: -float(r["importance"]),
    )
    metrics["permutation_importance"] = importances
    metrics["train_seconds"] = round(time.monotonic() - t0, 2)
    if metrics["train_seconds"] > spec.time_budget_s:
        metrics["time_budget_exceeded"] = True
    x_all = enc.transform(fit_df)
    reference: dict[str, Any] = {"numeric": {}, "categorical": {}}
    for j, c in enumerate(enc.numeric):
        ref = reference_bins(x_all[:, j])
        if ref:
            reference["numeric"][c] = ref
    for c in enc.categorical:
        vc = fit_df[c].astype("string").fillna("__missing__").value_counts(normalize=True)
        reference["categorical"][c] = {str(k): float(v) for k, v in vc.items()}
    card = _card(spec, "gbm", metrics, data, importances, None)
    return TrainOutcome(scorer, "gbm", metrics, card, reference, int(len(fit_df) + len(cal_df)))


def _metrics(y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    base = float(y.mean())
    return {
        "auc": round(float(roc_auc_score(y, p)), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "brier_baseline": round(float(brier_score_loss(y, np.full(len(y), base))), 4),
        "ece": round(expected_calibration_error(y, p), 4),
        "base_rate": round(base, 4),
        "lift_by_decile": [{k: round(v, 4) for k, v in row.items()} for row in lift_by_decile(y, p)],
    }


def _baseline(df: pd.DataFrame, enc: FeatureEncoder) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in enc.numeric:
        med = pd.to_numeric(df[c], errors="coerce").median()
        out[c] = None if pd.isna(med) else float(med)
    for c in enc.categorical:
        mode = df[c].dropna().astype(str).mode()
        out[c] = str(mode.iloc[0]) if len(mode) else None
    return out


def _card(
    spec: TrainSpec, method: str, metrics: dict[str, Any], df: pd.DataFrame, importances: list[dict[str, Any]], reason: str | None
) -> dict[str, Any]:
    return {
        "model": spec.name,
        "method": method,
        "target": spec.target_description,
        "features": {"numeric": spec.numeric, "categorical": spec.categorical},
        "split": "time-ordered: first 70% train (last 20% of it used only to calibrate), last 30% validation; embargo = label horizon",
        "training_window": {
            "from": int(df[spec.time_col].min()) if len(df) else None,
            "to": int(df[spec.time_col].max()) if len(df) else None,
        },
        "rows": len(df),
        "seed": spec.seed,
        "libraries": {
            "python": platform.python_version(),
            "scikit-learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "metrics_summary": {k: metrics.get(k) for k in ("auc", "pr_auc", "brier", "brier_baseline", "ece", "base_rate")},
        "top_features": importances[:5],
        "cold_start_reason": reason,
        "limitations": [
            *spec.notes,
            "Metrics are measured on a single time-ordered validation window and do not guarantee future performance.",
            "Per-prediction explanations are occlusion effects (feature reset to a reference value), not Shapley values.",
        ],
    }
