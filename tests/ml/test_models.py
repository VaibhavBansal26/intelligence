from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.db.session import session_scope
from app.errors import Conflict, NotFound
from app.features.leads import build_lead_frame
from app.features.rooms import build_room_frame
from app.models.drift import drift_report
from app.models.gbm import TrainSpec, train_binary
from app.models.lead_model import MODEL_TYPE, lead_spec
from app.models.registry import ModelRegistry
from app.models.scorers import lead_heuristic
from tests.conftest import Env


def toy_frame(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n)
    y = (rng.random(n) < 1 / (1 + np.exp(-(1.5 * x - 0.5)))).astype(int)
    return pd.DataFrame(
        {"t": np.arange(n) * 3600.0, "x": x, "noise": rng.normal(size=n), "cat": rng.choice(["a", "b"], n), "y": y}
    )


def spec(**kw) -> TrainSpec:  # type: ignore[no-untyped-def]
    base = {
        "name": "toy",
        "numeric": ["x", "noise"],
        "categorical": ["cat"],
        "time_col": "t",
        "label_col": "y",
        "embargo_s": 0.0,
        "heuristic": lambda df, b: np.full(len(df), b),
        "heuristic_baseline": {},
        "min_rows": 200,
        "min_class": 20,
        "seed": 3,
    }
    base.update(kw)
    return TrainSpec(**base)


def test_gbm_learns_signal_with_time_ordered_split() -> None:
    out = train_binary(toy_frame(3000), spec())
    assert out.method == "gbm" and out.metrics["auc"] > 0.75
    imp = {r["feature"]: r["importance"] for r in out.metrics["permutation_importance"]}
    assert imp["x"] > 5 * max(imp["noise"], 1e-4)
    assert out.metrics["brier"] < out.metrics["brier_baseline"]
    assert out.card["split"].startswith("time-ordered") and out.card["seed"] == 3


def test_training_is_reproducible_for_a_seed() -> None:
    a = train_binary(toy_frame(1500), spec())
    b = train_binary(toy_frame(1500), spec())
    assert a.metrics["auc"] == b.metrics["auc"] and a.metrics["brier"] == b.metrics["brier"]


def test_cold_start_falls_back_to_documented_heuristic_and_says_so() -> None:
    out = train_binary(toy_frame(50), spec())
    assert out.method == "heuristic" and "only 50 labelled rows" in out.cold_start_reason
    out2 = train_binary(toy_frame(400).assign(y=0), spec())
    assert out2.method == "heuristic" and "fewer than" in out2.cold_start_reason
    assert out.card["method"] == "heuristic" and out.card["cold_start_reason"]


def test_validation_window_is_strictly_after_training_window() -> None:
    df = toy_frame(2000)
    out = train_binary(df, spec(embargo_s=24 * 3600.0))
    assert out.metrics["embargo_dropped_rows"] >= 20
    assert (
        out.metrics["train_rows"]
        + out.metrics["calibration_rows"]
        + out.metrics["embargo_dropped_rows"]
        + out.metrics["validation_rows"]
        == 2000
    )


def test_calibration_improves_or_keeps_ece_small() -> None:
    out = train_binary(toy_frame(4000, seed=4), spec())
    assert out.metrics["calibration"] in ("isotonic", "platt") and out.metrics["ece"] < 0.08


def test_lead_heuristic_orders_sensibly() -> None:
    df = pd.DataFrame(
        {
            "n_page_views_pre": [1, 5],
            "prior_intent_purchase": [0, 1],
            "prior_sentiment_mean": [0.0, 0.8],
            "utm_source": ["facebook", "email"],
            "device": ["mobile", "desktop"],
            "prior_orders": [0, 2],
        }
    )
    p = lead_heuristic(df, 0.2)
    assert p[1] > p[0] and 0 < p[0] < p[1] < 1


def test_lead_features_are_point_in_time_and_immature_leads_excluded(env: Env) -> None:
    c = env.app.state.container
    with session_scope(c.sessions) as s:
        frame = build_lead_frame(s, env.account_id, horizon_days=14)
        df = frame.df
        assert (~df["mature"]).sum() > 0 and df["mature"].sum() > 1000
        assert (df.loc[df["mature"], "created_at"] + 14 * 86400 <= frame.as_of).all()
        sample = df[(df["prior_rooms"] > 0)].iloc[0]
        rooms = s.execute(select(m.Room).where(m.Room.contact_key == sample["contact_key"])).scalars().all()
        before = [r for r in rooms if r.created_at < sample["created_at"]]
        assert sample["prior_rooms"] == len(before)
        # a lead's feature row never changes when later data exists: recompute with an earlier as_of
        early = build_lead_frame(s, env.account_id, horizon_days=14, as_of=int(sample["created_at"]) + 3600).df
        row = early[early["lead_id"] == sample["lead_id"]].iloc[0]
        assert row["prior_rooms"] == sample["prior_rooms"] and row["n_page_views_pre"] == sample["n_page_views_pre"]
        assert row["label"] == sample["label"] and not row["mature"]


def test_room_features_exclude_outcome_columns_and_censor_unanswered(env: Env) -> None:
    from app.features.rooms import FEATURES

    for banned in (
        "messages_in",
        "messages_out",
        "resolution_s",
        "first_response_s",
        "reopen_count",
        "csat",
        "status",
        "closed_at",
        "assignee_key",
    ):
        assert banned not in FEATURES
    c = env.app.state.container
    with session_scope(c.sessions) as s:
        f = build_room_frame(s, env.account_id, sla_s=3600)
    df = f.df
    censored = df[~df["label_known"]]
    assert len(censored) > 0 and censored["first_response_s"].isna().all() and (f.as_of - censored["created_at"] <= 3600).all()
    stale = df[df["first_response_s"].isna() & df["label_known"]]
    assert (stale["breach"] == 1).all()


def test_drift_detects_shifted_population_and_ignores_resample(env: Env) -> None:
    c = env.app.state.container
    row = c.registry.active(env.account_id, MODEL_TYPE)
    art = c.registry.load(row)
    with session_scope(c.sessions) as s:
        df = build_lead_frame(s, env.account_id, horizon_days=14).df
    same = drift_report(art["scorer"], art["reference"], df.sample(1500, random_state=1), warn=0.1, alert=0.25)
    assert same["status"] in ("ok", "warn")
    shifted = df.copy()
    shifted["utm_source"] = "facebook"
    shifted["n_page_views_pre"] = shifted["n_page_views_pre"] + 6
    bad = drift_report(art["scorer"], art["reference"], shifted, warn=0.1, alert=0.25)
    assert bad["status"] == "alert" and bad["features"][0]["psi"] > 0.25
    assert drift_report(art["scorer"], art["reference"], df.head(5), warn=0.1, alert=0.25)["status"] == "insufficient_data"
    api = env.client.get("/v1/models/types/lead_conversion/drift?recent_days=30", headers=env.h)
    assert api.status_code == 200 and api.json()["status"] in ("ok", "warn", "alert", "insufficient_data")


def test_registry_versions_promote_rollback_and_integrity(fresh_env: Env) -> None:
    h, cl = fresh_env.h, fresh_env.client
    for _ in range(2):
        assert (
            cl.post("/v1/models/train?wait=true", json={"model_types": ["segments"]}, headers=h).json()["status"] == "succeeded"
        )
    versions = list(cl.get("/v1/models?model_type=segments", headers=h).json())
    assert (
        [v["version"] for v in versions] == [2, 1] and versions[0]["status"] == "active" and versions[1]["status"] == "archived"
    )
    rb = cl.post("/v1/models/types/segments/rollback", headers=h)
    assert rb.status_code == 200 and rb.json()["version"] == 1 and rb.json()["status"] == "active"
    again = cl.post("/v1/models/types/segments/rollback", headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "NO_PREVIOUS_MODEL"
    v2 = next(v for v in cl.get("/v1/models", headers=h).json() if v["version"] == 2)
    assert cl.post(f"/v1/models/{v2['id']}/promote", headers=h).json()["status"] == "active"
    # tamper with the artifact: loading must be refused
    c = fresh_env.app.state.container
    row = c.registry.active(fresh_env.account_id, "segments")
    Path(row.artifact_path).write_bytes(b"tampered")
    with pytest.raises(Conflict):
        c.registry.load(row)
    assert cl.get("/v1/segments", headers=h).status_code == 409


def test_registry_unknown_ids_are_404(fresh_env: Env) -> None:
    c = fresh_env.app.state.container
    with pytest.raises(NotFound):
        c.registry.get(fresh_env.account_id, "nope")
    assert isinstance(c.registry, ModelRegistry)


def test_auto_promotion_policy_keeps_better_model(fresh_env: Env) -> None:
    c = fresh_env.app.state.container
    svc = c.analytics
    first = svc.train(fresh_env.account_id, ["sla_breach"])[0]
    assert first["status"] == "active"
    second = svc.train(fresh_env.account_id, ["sla_breach"])[0]
    assert (second["version"] == 2 and second["status"] == "active" and "within 0.02" in second["promotion"]) or second[
        "method"
    ] == "heuristic"
    s = Settings(env="test")
    assert lead_spec(s, 14).embargo_s == 14 * 86400


def test_heuristics_are_finite_with_missing_inputs() -> None:
    from app.models.scorers import sla_heuristic

    df = pd.DataFrame([{"utm_source": None, "device": None}, {"utm_source": "email", "n_page_views_pre": np.nan}])
    assert np.isfinite(lead_heuristic(df, 0.2)).all()
    assert np.isfinite(sla_heuristic(pd.DataFrame([{"channel": None, "hour": np.nan}, {}]), 0.3)).all()
