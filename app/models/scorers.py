"""Scorer objects (picklable) shared by the lead and SLA models, with occlusion-based explanations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


class FeatureEncoder:
    """Numeric passthrough + top-K frequency ordinal encoding of categoricals (unknown -> 'other', missing -> NaN)."""

    def __init__(self, numeric: list[str], categorical: list[str], max_categories: int = 20) -> None:
        self.numeric = numeric
        self.categorical = categorical
        self.max_categories = max_categories
        self.maps: dict[str, dict[str, int]] = {}

    @property
    def columns(self) -> list[str]:
        return self.numeric + self.categorical

    @property
    def categorical_mask(self) -> list[bool]:
        return [False] * len(self.numeric) + [True] * len(self.categorical)

    def fit(self, df: pd.DataFrame) -> FeatureEncoder:
        for c in self.categorical:
            top = df[c].dropna().astype(str).value_counts().head(self.max_categories - 1).index
            self.maps[c] = {str(k): i for i, k in enumerate(sorted(top))}
        return self

    def transform(self, df: pd.DataFrame) -> NDArray[np.float64]:
        cols: list[NDArray[np.float64]] = []
        for c in self.numeric:
            cols.append(pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float) if c in df else np.full(len(df), np.nan))
        for c in self.categorical:
            other = len(self.maps[c])
            raw = df[c] if c in df else pd.Series([None] * len(df), index=df.index)
            mapping = self.maps[c]
            codes = raw.astype("string").map(lambda v, mp=mapping, oth=other: np.nan if v is pd.NA or v is None else float(mp[str(v)] if str(v) in mp else oth))
            cols.append(codes.to_numpy(dtype=float))
        return np.column_stack(cols) if cols else np.empty((len(df), 0))


class Scorer(Protocol):
    method: str
    features: list[str]
    baseline: dict[str, Any]

    def predict(self, df: pd.DataFrame) -> NDArray[np.float64]: ...


def explain_by_occlusion(scorer: Scorer, df: pd.DataFrame, top_k: int = 3, min_abs: float = 0.005) -> list[list[dict[str, Any]]]:
    """Per-row top factors: change in predicted probability when one feature is reset to its reference value."""
    if df.empty:
        return []
    base = scorer.predict(df)
    deltas = np.zeros((len(df), len(scorer.features)))
    for j, f in enumerate(scorer.features):
        alt = df.copy()
        alt[f] = scorer.baseline.get(f)
        deltas[:, j] = base - scorer.predict(alt)
    out: list[list[dict[str, Any]]] = []
    for i in range(len(df)):
        order = np.argsort(-np.abs(deltas[i]))[:top_k]
        row: list[dict[str, Any]] = []
        for j in order:
            if abs(deltas[i, j]) < min_abs:
                continue
            f = scorer.features[j]
            v = df.iloc[i][f]
            row.append({
                "feature": f,
                "value": None if pd.isna(v) else (v if isinstance(v, str) else float(v)),
                "effect": round(float(deltas[i, j]), 4),
                "direction": "increases" if deltas[i, j] > 0 else "decreases",
            })
        out.append(row)
    return out


class Calibrator:
    """Isotonic (enough data) or Platt (little data) calibration of raw probabilities; identity if unusable."""

    def __init__(self) -> None:
        self.kind = "none"
        self._iso: IsotonicRegression | None = None
        self._lr: LogisticRegression | None = None

    def fit(self, raw: NDArray[np.float64], y: NDArray[np.int_]) -> Calibrator:
        pos = int(y.sum())
        neg = int(len(y) - pos)
        if pos >= 20 and neg >= 20 and len(y) >= 400:
            self._iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, y)
            self.kind = "isotonic"
        elif pos >= 10 and neg >= 10:
            self._lr = LogisticRegression(C=1e6, max_iter=500).fit(_logit(raw).reshape(-1, 1), y)
            self.kind = "platt"
        return self

    def transform(self, raw: NDArray[np.float64]) -> NDArray[np.float64]:
        if self._iso is not None:
            return np.clip(self._iso.predict(raw), 1e-4, 1 - 1e-4)
        if self._lr is not None:
            return np.asarray(self._lr.predict_proba(_logit(raw).reshape(-1, 1))[:, 1], dtype=float)
        return raw


def _logit(p: NDArray[np.float64]) -> NDArray[np.float64]:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.asarray(np.log(p / (1 - p)), dtype=float)


class GbmScorer:
    method = "gbm"

    def __init__(self, estimator: Any, encoder: FeatureEncoder, calibrator: Calibrator, baseline: dict[str, Any]) -> None:
        self.estimator = estimator
        self.encoder = encoder
        self.calibrator = calibrator
        self.baseline = baseline
        self.features = encoder.columns

    def predict(self, df: pd.DataFrame) -> NDArray[np.float64]:
        x = self.encoder.transform(df)
        raw = np.asarray(self.estimator.predict_proba(x)[:, 1], dtype=float)
        return self.calibrator.transform(raw)


class FunctionScorer:
    """Heuristic scorer wrapping a documented formula; used as the cold-start path."""

    method = "heuristic"

    def __init__(self, fn: Callable[[pd.DataFrame, float], NDArray[np.float64]], base_rate: float, features: list[str], baseline: dict[str, Any], name: str) -> None:
        self._fn = fn
        self.base_rate = base_rate
        self.features = features
        self.baseline = baseline
        self.name = name

    def predict(self, df: pd.DataFrame) -> NDArray[np.float64]:
        return self._fn(df, self.base_rate)


def sigmoid(x: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.asarray(1.0 / (1.0 + np.exp(-x)), dtype=float)


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> NDArray[np.float64]:
    if col not in df:
        return np.full(len(df), default)
    return pd.to_numeric(df[col], errors="coerce").fillna(default).to_numpy(dtype=float)


def lead_heuristic(df: pd.DataFrame, base_rate: float) -> NDArray[np.float64]:
    """logit(p) = logit(base) + 0.25*(extra visits, capped 4) + 0.9*purchase-intent chat + 0.3*prior sentiment
    + 0.3*(email/webinar traffic) - 0.2*(mobile) + 0.15*(prior orders, capped 3). Coefficients are priors, not learned."""
    base = float(np.clip(base_rate, 0.01, 0.99))
    z = np.log(base / (1 - base))
    z = z + 0.25 * np.clip(_num(df, "n_page_views_pre", 1) - 1, 0, 4)
    z = z + 0.9 * (_num(df, "prior_intent_purchase") > 0)
    z = z + 0.3 * _num(df, "prior_sentiment_mean")
    src = df["utm_source"].astype("string").str.lower() if "utm_source" in df else pd.Series([""] * len(df))
    z = z + 0.3 * src.isin(["email", "webinar"]).to_numpy(dtype=float)
    dev = df["device"].astype("string").str.lower() if "device" in df else pd.Series([""] * len(df))
    z = z - 0.2 * (dev == "mobile").to_numpy(dtype=float)
    z = z + 0.15 * np.clip(_num(df, "prior_orders"), 0, 3)
    return sigmoid(np.asarray(z, dtype=float))


def sla_heuristic(df: pd.DataFrame, base_rate: float) -> NDArray[np.float64]:
    """logit(p) = logit(base) + 0.8*night(22-06h) + 0.5*weekend + 0.5*(contact historically slow) + 0.3*(email channel)."""
    base = float(np.clip(base_rate, 0.02, 0.98))
    z = np.full(len(df), np.log(base / (1 - base)))
    hour = _num(df, "hour", 12)
    z = z + 0.8 * ((hour >= 22) | (hour < 6))
    z = z + 0.5 * np.isin(_num(df, "dow", 0), [5, 6])
    z = z + 0.5 * (_num(df, "prior_avg_first_response_s", 0) > 3600)
    ch = df["channel"].astype("string") if "channel" in df else pd.Series([""] * len(df))
    z = z + 0.3 * (ch == "email").to_numpy(dtype=float)
    return sigmoid(np.asarray(z, dtype=float))
