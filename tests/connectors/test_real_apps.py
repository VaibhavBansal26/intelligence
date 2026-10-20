"""Regression tests on responses RECORDED from the real Funnels and Conversations apps (tests/fixtures/real/).

The fixtures are produced by `scripts/integration/run.sh --record` (see docs/INTEGRATION.md). They contain no API keys,
no peppers and no e-mail addresses: contact/visitor/agent keys are HMACs of throw-away test e-mails under peppers that
are not stored. Each test pins something the synthetic fake apps did not exercise and the real ones did."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.connectors.client import ConnectorRateLimited, SourceClient, parse_rate_limit
from app.connectors.records import EventRec, LeadRec, MessageMetaRec, RoomRec, StructureFunnelRec, parse_ts
from app.db import models as m
from app.db.session import session_scope
from app.main import create_app
from tests.conftest import ADMIN_KEY, make_settings

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "real"
FUNNELS = "http://funnels.recorded.test"
CONV = "http://conversations.recorded.test"


def nd(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


def doc(name: str) -> Any:
    return json.loads((FIX / name).read_text())


# --- record models -----------------------------------------------------------------------------------------------
def test_recorded_funnels_exports_parse_and_keep_utc_instants() -> None:
    events, leads = nd("funnels_events.ndjson"), nd("funnels_leads.ndjson")
    assert {e["type"] for e in events} == {"page_view", "optin", "order"}
    for row in events:
        rec = EventRec.model_validate(row)
        assert rec.ts == int(datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).astimezone(UTC).timestamp())
    assert any(e["ts"].endswith("Z") and "." in e["ts"] for e in events)  # milliseconds, as the real app sends them
    assert any(e["contact_key"] for e in events) and any(e["contact_key"] is None for e in events)
    assert any(e["device"] == "unknown" for e in events) and any(e["device"] == "desktop" for e in events)
    parsed = [LeadRec.model_validate(r) for r in leads]
    assert any(r.converted_to_order and r.first_order_at for r in parsed) and any(not r.converted_to_order for r in parsed)
    assert all(r.first_order_at is None or r.first_order_at >= r.created_at - 1 for r in parsed)


def test_recorded_rooms_include_unlabelled_rooms_and_numeric_urgency() -> None:
    rooms = nd("conversations_rooms.ndjson")
    assert any(r["ai_intent"] is None for r in rooms), "fixture must contain rooms the app's triage never labelled"
    assert any(isinstance(r["ai_urgency"], float) for r in rooms) and {r["ai_sentiment"] for r in rooms} >= {
        "negative",
        "neutral",
        "positive",
    }
    parsed = [RoomRec.model_validate(r) for r in rooms]
    assert all(p.contact_key is None or len(p.contact_key) == 64 for p in parsed)
    assert {p.ai_urgency for p in parsed if p.ai_urgency} <= {
        "0.15",
        "0.4",
        "0.6",
        "0.8",
        "0.9",
    }  # kept as text, parsed by norm.py
    assert any(sum(p.channel_mix.values()) > 0 for p in parsed)
    metas = [MessageMetaRec.model_validate(r) for r in nd("conversations_messages_meta.ndjson")]
    assert {x.direction for x in metas} == {"in", "out"}


def test_recorded_funnel_structure_has_the_real_shape_and_is_adapted_like_sync_does() -> None:
    body = doc("funnel_structure.json")
    assert set(body) == {"data", "next_cursor"} and "funnel_id" not in body["data"][0] and "id" in body["data"][0]
    assert "type" not in body["data"][0]["steps"][0]
    raw = dict(body["data"][0])
    raw.setdefault("funnel_id", raw.get("id"))
    raw["steps"] = [{**x, "step_id": x.get("step_id", x.get("id"))} for x in raw["steps"]]
    rec = StructureFunnelRec.model_validate(raw)
    assert rec.funnel_id == body["data"][0]["id"] and rec.steps[0].blocks and rec.steps[0].word_count > 0


def test_recorded_last_step_has_null_exit_rate_and_timeseries_is_enveloped() -> None:
    steps = doc("funnel_steps.json")["data"]
    assert steps[-1]["exit_rate"] is None and steps[0]["avg_time_on_page_s"] is None
    ts = doc("funnel_timeseries.json")
    assert {"interval", "metric", "tz", "points"} <= set(ts)
    assert ts["tz"] == "America/New_York" and ts["points"][0]["t"].endswith(
        "T04:00:00.000Z"
    )  # recorded with tz=America/New_York: UTC instants of local midnights
    assert parse_ts(ts["points"][1]["t"]) - parse_ts(ts["points"][0]["t"]) == 86400


# --- the real 429 ------------------------------------------------------------------------------------------------
@respx.mock
async def test_recorded_429_is_slept_out_once_and_then_the_page_is_served() -> None:
    rec = doc("funnels_429.json")
    url = FUNNELS + "/api/v1/analytics/export/events"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(429, headers=rec["headers"], json=rec["body"]),
            httpx.Response(200, text='{"id":"1"}\n', headers=rec["ok_headers"]),
        ]
    )
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async with httpx.AsyncClient() as http:
        client = SourceClient(base_url=FUNNELS, api_key="k", http=http, sleep=sleep, rand=lambda: 1.0)
        rows = [r async for r in client.stream_ndjson("/export/events", {})]
    assert rows == [{"id": "1"}] and sleeps == [float(rec["headers"]["retry-after"])]
    remaining, interval = parse_rate_limit(httpx.Headers(rec["ok_headers"]))
    assert remaining == 6 and interval == 6.0


@respx.mock
async def test_recorded_429_with_penalty_longer_than_the_cap_is_surfaced() -> None:
    rec = doc("funnels_429.json")
    respx.get(FUNNELS + "/api/v1/analytics/export/events").mock(
        return_value=httpx.Response(429, headers={**rec["headers"], "retry-after": "300"}, json=rec["body"])
    )
    async with httpx.AsyncClient() as http:
        client = SourceClient(
            base_url=FUNNELS, api_key="k", http=http, sleep=lambda s: _noop(), rand=lambda: 1.0, max_retry_after_s=120
        )
        with pytest.raises(ConnectorRateLimited) as e:
            [r async for r in client.stream_ndjson("/export/events", {})]
    assert e.value.retry_after_s == 300 and e.value.group == "export"


async def _noop() -> None:
    return None


# --- the whole service on recorded responses ------------------------------------------------------------------------
def recorded_apps(page_size: int) -> Callable[[httpx.Request], httpx.Response]:
    """The two apps as recorded: same paths, same envelopes, NDJSON exports paged with X-Next-Cursor."""
    exports = {
        "/export/events": nd("funnels_events.ndjson"),
        "/export/leads": nd("funnels_leads.ndjson"),
        "/export/rooms": nd("conversations_rooms.ndjson"),
        "/export/messages-meta": nd("conversations_messages_meta.ndjson"),
    }
    steps, structure = doc("funnel_steps.json"), doc("funnel_structure.json")

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/analytics")
        host = request.url.host
        if path == "/schema":
            return httpx.Response(200, json={"schema_version": 1, "schemas": {}})
        if path in exports:
            rows = exports[path]
            if (host.startswith("funnels")) != (path in ("/export/events", "/export/leads")):
                return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
            start = int(request.url.params.get("cursor", "0"))
            lim = min(int(request.url.params.get("limit", "1000")), page_size)
            page = rows[start : start + lim]
            headers = {"content-type": "application/x-ndjson; charset=utf-8", "x-row-count": str(len(page))}
            if start + lim < len(rows):
                headers["x-next-cursor"] = str(start + lim)
            return httpx.Response(200, text="".join(json.dumps(r) + "\n" for r in page), headers=headers)
        if path == "/export/funnel-structure":
            return httpx.Response(200, json=structure)
        if path == "/funnels":
            return httpx.Response(200, json=doc("funnels_list.json"))
        if path.startswith("/funnels/") and path.endswith("/steps"):
            return httpx.Response(200, json=steps)
        if path.endswith("/timeseries"):
            return httpx.Response(
                200, json=doc("funnel_timeseries.json" if host.startswith("funnels") else "conversations_timeseries.json")
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": path}})

    return handle


@pytest.fixture
def recorded_env(tmp_path: Path) -> Iterator[TestClient]:
    last = max(parse_ts(r["ts"]) for r in nd("funnels_events.ndjson"))
    handler = recorded_apps(page_size=150)

    def factory(src: m.Source) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=src.base_url)

    app = create_app(
        make_settings(tmp_path, connector_export_page_limit=150),
        http_factory=factory,
        clock=lambda: float(last + 3600),
        use_env_llm=False,
    )
    with TestClient(app) as client:
        yield client


def test_recorded_responses_sync_train_and_every_endpoint_answers(recorded_env: TestClient) -> None:
    client = recorded_env
    h = {
        "Authorization": "Bearer "
        + client.post("/admin/accounts", json={"name": "Recorded"}, headers={"X-Admin-Key": ADMIN_KEY}).json()["api_key"]
    }
    for kind, url in (("funnels", FUNNELS), ("conversations", CONV)):
        r = client.post(
            "/v1/sources",
            json={"kind": kind, "name": kind, "base_url": url, "api_key": "k" * 12, "identity_pepper": "p" * 12},
            headers=h,
        )
        assert r.status_code == 201, r.text
    job = client.post("/v1/sync?wait=true", json={}, headers=h).json()
    assert job["status"] == "succeeded" and not job["result"]["errors"], job
    by = {(s["kind"], x["resource"]): x for s in job["result"]["sources"] for x in s["resources"]}
    assert by[("funnels", "events")]["rows"] == len(nd("funnels_events.ndjson")) and by[("funnels", "events")]["pages"] == 3
    assert by[("conversations", "rooms")]["rows"] == len(nd("conversations_rooms.ndjson"))
    assert by[("funnels", "funnel_structure")]["rows"] == sum(len(f["steps"]) for f in doc("funnel_structure.json")["data"])
    c = client.app.state.container  # type: ignore[attr-defined]
    with session_scope(c.sessions) as s:
        unlabelled = int(s.execute(select(func.count()).select_from(m.Room).where(m.Room.ai_intent.is_(None))).scalar() or 0)
    assert unlabelled > 0
    assert client.post("/v1/models/train?wait=true", json={}, headers=h).json()["status"] == "succeeded"
    for path in (
        "/v1/rankings/funnels",
        "/v1/rankings/steps",
        "/v1/leads/scores",
        "/v1/rankings/conversations/open?limit=200",
        "/v1/segments",
        "/v1/anomalies",
        "/v1/insights",
        "/v1/join",
    ):
        r = client.get(path, headers=h)
        assert r.status_code == 200, (path, r.text)
        json.loads(r.text)  # strict JSON: no NaN/Infinity tokens
    assert "NaN" not in client.get("/v1/rankings/conversations/open?limit=200", headers=h).text
    assert client.post("/v1/ask", json={"question": "Which funnel should I fix first?"}, headers=h).status_code == 200
