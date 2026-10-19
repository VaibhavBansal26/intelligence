"""Lead conversion model: P(order within the label horizon | features known at opt-in time)."""

from __future__ import annotations

import pandas as pd

from app.config import Settings
from app.features.leads import CATEGORICAL_FEATURES, NUMERIC_FEATURES, LeadFrame
from app.models.gbm import TrainOutcome, TrainSpec, train_binary
from app.models.scorers import lead_heuristic

MODEL_TYPE = "lead_conversion"
HEURISTIC_BASELINE = {
    "n_page_views_pre": 1.0,
    "n_distinct_steps_pre": 1.0,
    "hours_since_first_seen": 0.0,
    "optin_hour": 12.0,
    "optin_dow": 2.0,
    "prior_orders": 0.0,
    "prior_rooms": 0.0,
    "prior_intent_purchase": 0.0,
    "prior_sentiment_mean": 0.0,
    "prior_urgency_max": 0.0,
    "prior_first_response_mean_s": None,
    "prior_csat_mean": None,
    "utm_source": "organic",
    "funnel_id": None,
    "device": "desktop",
}


def lead_spec(settings: Settings, horizon_days: int) -> TrainSpec:
    return TrainSpec(
        name=MODEL_TYPE,
        numeric=NUMERIC_FEATURES,
        categorical=CATEGORICAL_FEATURES,
        time_col="created_at",
        label_col="label",
        embargo_s=horizon_days * 86400,
        heuristic=lead_heuristic,
        heuristic_baseline=HEURISTIC_BASELINE,
        min_rows=settings.min_leads_for_model,
        min_class=settings.min_class_count,
        seed=settings.seed,
        max_rows=settings.ml_max_training_rows,
        backend="hgb",
        time_budget_s=settings.train_time_budget_s,
        target_description=f"order within {horizon_days} days of opt-in, using only information available at opt-in time",
        notes=[
            "Leads whose label window is still open at the data end are excluded from training (right-censored).",
            "Conversation features use only rooms created BEFORE the opt-in moment.",
        ],
    )


def train_lead_model(frame: LeadFrame, settings: Settings) -> TrainOutcome:
    df = frame.df
    mature = df[df["mature"]].copy() if not df.empty else df
    spec = lead_spec(settings, frame.horizon_days)
    out = train_binary(mature, spec)
    out.card["conversation_features_available"] = frame.has_conversations
    out.card["as_of"] = frame.as_of
    return out


def open_leads(frame: LeadFrame) -> pd.DataFrame:
    """Leads whose outcome is not yet known: unconverted and inside the label window (worth ranking)."""
    df = frame.df
    if df.empty:
        return df
    return df[(~df["mature"]) & (~df["converted_to_order"].astype(bool))].copy()
