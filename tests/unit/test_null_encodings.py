"""Nulls and odd encodings of the AI-labelled fields, as the real Conversations app exports them."""

from __future__ import annotations

import math

import pandas as pd

from app.api.v1 import _finite
from app.features.norm import flag_eq, sentiment_value, urgency_value


def test_flag_eq_treats_null_as_false_instead_of_crashing() -> None:
    s = pd.Series(["purchase", None, "Purchase", "support", pd.NA], dtype="object")
    assert flag_eq(s, "purchase").tolist() == [1, 0, 1, 0, 0]
    assert flag_eq(pd.Series([None, None]), "purchase").tolist() == [0, 0]  # an all-null column (triage never ran)


def test_real_encodings_of_ai_fields() -> None:
    # Conversations exports sentiment as a word and urgency as a 0..1 number (JSON number, stored as text here)
    assert [sentiment_value(v) for v in ("negative", "neutral", "positive")] == [-0.8, 0.0, 0.8]
    assert [urgency_value(v) for v in ("0.15", 0.4, "0.9", 1)] == [0.15, 0.4, 0.9, 1.0]
    assert math.isnan(sentiment_value(None)) and math.isnan(urgency_value(None))


def test_json_encoder_replaces_non_finite_floats() -> None:
    assert _finite({"a": float("nan"), "b": [1.0, float("inf")], "c": {"d": -float("inf")}, "e": "x"}) == {
        "a": None,
        "b": [1.0, None],
        "c": {"d": None},
        "e": "x",
    }
