"""Tolerant normalisation of AI-labelled conversation fields (the contract leaves their encoding open)."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd

_SENT_WORDS = {
    "very_negative": -1.0,
    "angry": -1.0,
    "negative": -0.8,
    "frustrated": -0.7,
    "mixed": 0.0,
    "neutral": 0.0,
    "positive": 0.8,
    "happy": 0.9,
    "very_positive": 1.0,
}
_URG_WORDS = {
    "none": 0.0,
    "low": 0.2,
    "normal": 0.35,
    "medium": 0.5,
    "moderate": 0.5,
    "high": 0.8,
    "urgent": 0.95,
    "critical": 1.0,
}


def _to_float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def sentiment_value(value: object) -> float:
    """Return a sentiment in [-1, 1]; NaN when unknown."""
    if value is None:
        return float("nan")
    f = _to_float(value)
    if f is not None:
        if -1.0 <= f <= 1.0:
            return f
        if 1.0 < f <= 5.0:
            return (f - 3.0) / 2.0
        return float(np.clip(f, -1.0, 1.0))
    return _SENT_WORDS.get(str(value).strip().lower().replace(" ", "_"), float("nan"))


def urgency_value(value: object) -> float:
    """Return urgency in [0, 1]; NaN when unknown."""
    if value is None:
        return float("nan")
    f = _to_float(value)
    if f is not None:
        if 0.0 <= f <= 1.0:
            return f
        if f <= 5.0:
            return min(1.0, f / 5.0)
        return float(np.clip(f / 10.0, 0.0, 1.0))
    return _URG_WORDS.get(str(value).strip().lower(), float("nan"))


def map_unique(series: pd.Series, fn: Callable[[object], float]) -> pd.Series:
    """Apply a scalar function once per distinct value (fast on categorical-like columns)."""
    mapping = {u: fn(u) for u in series.dropna().unique()}
    return series.map(mapping).astype("float64")


def flag_eq(series: pd.Series, value: str) -> pd.Series:
    """1 where the (nullable, case-insensitive) string column equals `value`, else 0.

    The comparison on a pandas string column yields <NA> for null rows; real exports have null `ai_intent` for every
    room the app's triage has not labelled, so a plain `.astype(int)` crashes on them."""
    return series.astype("string").str.lower().eq(value).fillna(False).astype(int)
