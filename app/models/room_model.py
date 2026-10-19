"""SLA breach model for conversations: P(first response slower than the SLA | features known at creation)."""

from __future__ import annotations

from app.config import Settings
from app.features.rooms import CATEGORICAL_FEATURES, NUMERIC_FEATURES, RoomFrame
from app.models.gbm import TrainOutcome, TrainSpec, train_binary
from app.models.scorers import sla_heuristic

MODEL_TYPE = "sla_breach"
HEURISTIC_BASELINE = {
    "hour": 12.0, "dow": 2.0, "priority": 0.0, "sentiment": 0.0, "urgency": 0.3, "prior_rooms": 0.0,
    "prior_avg_first_response_s": None, "funnel_revenue_before": 0.0, "funnel_orders_before": 0.0,
    "inbox_id": None, "channel": "dm", "intent": None,
}


def room_spec(settings: Settings, sla_s: float) -> TrainSpec:
    return TrainSpec(
        name=MODEL_TYPE, numeric=NUMERIC_FEATURES, categorical=CATEGORICAL_FEATURES, time_col="created_at", label_col="breach",
        embargo_s=sla_s, heuristic=sla_heuristic, heuristic_baseline=HEURISTIC_BASELINE, min_rows=settings.min_rooms_for_model,
        min_class=settings.min_class_count, seed=settings.seed, backend="hgb", time_budget_s=settings.train_time_budget_s,
        target_description=f"first response slower than {int(sla_s)}s (rooms still unanswered past the SLA count as breaches)",
        notes=["AI sentiment/urgency/intent labels are assumed to be available shortly after room creation.",
               "Rooms still inside the SLA window with no response are censored and excluded from training."],
    )


def train_room_model(frame: RoomFrame, settings: Settings) -> TrainOutcome:
    df = frame.df
    labelled = df[df["label_known"]].copy() if not df.empty else df
    return train_binary(labelled, room_spec(settings, frame.sla_s))
