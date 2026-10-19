"""End-to-end: fake contract apps -> sync -> train -> rank -> narrate, asserting recovery of planted truth."""

from __future__ import annotations

import pandas as pd

from tests.conftest import Env


def test_sync_landed_everything(env: Env) -> None:
    st = env.client.get("/v1/sync/status", headers=env.h).json()
    resources = {(s["resource"]) for s in st["sources"]}
    assert {"events", "leads", "rooms", "messages_meta", "rollups", "funnels", "funnel_structure"} <= resources
    assert all(s["last_status"] == "ok" for s in st["sources"])
    assert st["jobs"][0]["status"] in ("succeeded",)


def test_planted_weak_step_ranked_first(env: Env) -> None:
    r = env.client.get("/v1/rankings/steps", headers=env.h).json()
    top = r["steps"][0]
    assert (top["funnel_id"], top["step_id"]) == env.world.truth["weak_step"]
    assert top["uplift_p10"] <= top["expected_uplift"] <= top["uplift_p90"]
    f = env.client.get("/v1/rankings/funnels", headers=env.h).json()
    assert f["funnels"][0]["funnel_id"] == "fn_webinar"
    assert any(h["code"] == "long_copy_no_video" for h in top["content_hints"])


def test_tiny_funnel_never_outranks_big_one(env: Env) -> None:
    f = env.client.get("/v1/rankings/funnels", headers=env.h).json()["funnels"]
    ids = [x["funnel_id"] for x in f]
    assert ids.index("fn_pilot") > ids.index("fn_webinar")
    pilot = next(x for x in f if x["funnel_id"] == "fn_pilot")
    assert pilot["confidence"] in ("low", "medium")


def test_lead_model_recovers_planted_signal(env: Env) -> None:
    models = {m["model_type"]: m for m in env.client.get("/v1/models", headers=env.h).json()}
    lead = models["lead_conversion"]
    assert lead["status"] == "active" and lead["method"] == "gbm"
    assert lead["metrics"]["auc"] > 0.68
    assert lead["metrics"]["brier"] < lead["metrics"]["brier_baseline"]
    assert lead["metrics"]["lift_by_decile"][0]["lift"] > 1.4
    top = [x["feature"] for x in lead["metrics"]["permutation_importance"][:4]]
    assert len({"utm_source", "prior_intent_purchase", "hours_since_first_seen", "n_page_views_pre"} & set(top)) >= 3
    imp = {x["feature"]: x["importance"] for x in lead["metrics"]["permutation_importance"]}
    assert imp["utm_source"] > imp["optin_dow"] and imp["prior_intent_purchase"] > imp["optin_hour"]
    assert lead["card"]["split"].startswith("time-ordered")


def test_lead_scores_ranked_and_filtered(env: Env) -> None:
    r = env.client.get("/v1/leads/scores?limit=20&min_score=0.4", headers=env.h).json()
    scores = [i["score"] for i in r["items"]]
    assert scores == sorted(scores, reverse=True) and all(s >= 0.4 for s in scores)
    assert r["method"] == "gbm" and r["items"][0]["top_factors"]
    one = env.client.post("/v1/leads/score", json={"contact_key": r["items"][0]["contact_key"]}, headers=env.h).json()
    assert abs(one["score"] - r["items"][0]["score"]) < 1e-6


def test_score_by_email_uses_shared_pepper(env: Env) -> None:
    from app.security.crypto import contact_key

    keys = {ld["contact_key"] for ld in env.world.leads}
    email = next(
        f"user{i}@example.test" for i in range(1, 20000) if contact_key(env.world.config.pepper, f"user{i}@example.test") in keys
    )
    r = env.client.post("/v1/leads/score", json={"email": email}, headers=env.h)
    assert r.status_code == 200, r.text
    assert r.json()["contact_key"] == contact_key(env.world.config.pepper, email)
    assert email not in r.text
    assert env.client.post("/v1/leads/score", json={"email": "nobody@example.test"}, headers=env.h).status_code == 404


def test_conversation_ranking_surfaces_planted_rooms(env: Env) -> None:
    r = env.client.get("/v1/rankings/conversations/open?limit=8", headers=env.h).json()
    top_ids = [i["room_id"].split(":")[1] for i in r["items"][:4]]
    assert set(top_ids) == set(env.world.truth["planted_open_rooms"])
    assert all(i["reasons"] and i["components"] for i in r["items"])
    calm = [i for i in r["items"] if i["room_id"].endswith("room_calm")]
    assert not calm or calm[0]["rank"] > 4
    assert r["items"][0]["vip"] is True


def test_sla_model_learned_night_and_inbox(env: Env) -> None:
    sla = next(m for m in env.client.get("/v1/models", headers=env.h).json() if m["model_type"] == "sla_breach")
    assert sla["metrics"]["auc"] > 0.64
    top = [x["feature"] for x in sla["metrics"]["permutation_importance"][:3]]
    assert "hour" in top and "inbox_id" in top


def test_planted_anomalies_detected_with_few_false_alerts(env: Env) -> None:
    r = env.client.get("/v1/anomalies?since_days=200", headers=env.h).json()
    from datetime import UTC, datetime

    start = env.world.start_ts

    def day(a: dict) -> int:
        return (a["start_t"] - start) // 86400

    t_day, b_day = env.world.truth["traffic_anomaly"]["day"], env.world.truth["backlog_anomaly_day"]
    traffic = [
        a
        for a in r["alerts"]
        if a["scope"] == "fn_webinar"
        and a["metric"] in ("unique_visits", "visits")
        and a["direction"] == "drop"
        and abs(day(a) - t_day) <= 2
    ]
    backlog = [
        a
        for a in r["alerts"]
        if a["metric"] in ("backlog", "resolution_p50") and a["direction"] == "spike" and abs(day(a) - b_day) <= 4
    ]
    assert traffic and backlog
    planted = {id(a) for a in traffic + backlog}
    related = [a for a in r["alerts"] if abs(day(a) - t_day) <= 3 or abs(day(a) - b_day) <= 5]
    false_alerts = [a for a in r["alerts"] if a not in related]
    assert len(false_alerts) <= 6, [(a["metric"], a["scope"], a["start"]) for a in false_alerts]
    assert planted and datetime.now(UTC)


def test_segments_cover_contacts_and_revenue(env: Env) -> None:
    r = env.client.get("/v1/segments", headers=env.h).json()
    assert r["method"] == "kmeans" and 3 <= len(r["segments"]) <= 6
    assert abs(sum(s["revenue_share"] for s in r["segments"]) - 1.0) < 0.01
    assert r["segments"][0]["revenue_share"] > 0.5
    assert len({s["name"] for s in r["segments"]}) == len(r["segments"])


def test_join_report(env: Env) -> None:
    j = env.client.get("/v1/join", headers=env.h).json()
    assert j["single_source"] is False and j["joined_contacts"] > 500 and j["warnings"] == []


def test_insights_template_and_numbers_traceable(env: Env) -> None:
    r = env.client.get("/v1/insights", headers=env.h).json()
    assert r["narrative"]["source"] == "template" and r["narrative"]["fallback_reason"] == "llm_not_configured"
    assert r["findings"][0]["id"] == "step:fn_webinar:st_web_sales"
    from app.llm.guard import NumberGuard

    guard = NumberGuard(r["findings"], allow_ranks_up_to=len(r["findings"]))
    for a in r["narrative"]["actions"]:
        assert guard.ok(a["text"]), a


def test_ask_offline_answers_fix_first(env: Env) -> None:
    r = env.client.post("/v1/ask", json={"question": "Which funnel should I fix first and why?"}, headers=env.h).json()
    assert r["source"] == "template" and "Webinar Funnel" in r["answer"] and "Sales Page" in r["answer"]
    assert r["tools_used"] == ["get_funnel_rankings"]


def test_etag_conditional_get(env: Env) -> None:
    r1 = env.client.get("/v1/segments", headers=env.h)
    r2 = env.client.get("/v1/segments", headers={**env.h, "If-None-Match": r1.headers["ETag"]})
    assert r2.status_code == 304


def test_pandas_available() -> None:
    assert pd.__version__
