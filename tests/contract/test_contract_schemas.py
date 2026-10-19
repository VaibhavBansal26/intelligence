"""The synthetic fixtures and the fake contract apps must satisfy the JSON Schemas in contracts/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from app.connectors.records import EventRec, LeadRec, MessageMetaRec, RoomRec, StructureFunnelRec
from app.synthetic.world import World
from tests.conftest import Env

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((CONTRACTS / f"{name}.schema.json").read_text()), format_checker=FormatChecker())


def test_all_schemas_are_valid_json_schema() -> None:
    files = sorted(CONTRACTS.glob("*.schema.json"))
    assert len(files) >= 10
    for f in files:
        Draft202012Validator.check_schema(json.loads(f.read_text()))


@pytest.mark.parametrize(
    ("name", "attr"), [("event", "events"), ("lead", "leads"), ("room", "rooms"), ("message_meta", "messages_meta")]
)
def test_world_fixtures_match_schema(world: World, name: str, attr: str) -> None:
    v = validator(name)
    rows = getattr(world, attr)
    assert rows
    for r in rows[:: max(1, len(rows) // 3000)]:
        errs = list(v.iter_errors(r))
        assert not errs, (r, errs[0].message)


def test_fake_apps_serve_schema_valid_payloads(env: Env) -> None:
    for app, key, paths in (
        (
            env.funnels_app,
            "fa_test_key",
            [
                ("/export/events?limit=50", "event"),
                ("/export/leads?limit=50", "lead"),
                ("/funnels", "funnel"),
                ("/funnels/fn_webinar/steps", "funnel_step"),
                ("/export/funnel-structure", "funnel_structure"),
            ],
        ),
        (env.conv_app, "ca_test_key", [("/export/rooms?limit=50", "room"), ("/export/messages-meta?limit=50", "message_meta")]),
    ):
        c = TestClient(app)
        h = {"Authorization": f"Bearer {key}"}
        for path, name in paths:
            r = c.get("/api/v1/analytics" + path, headers=h)
            assert r.status_code == 200, path
            if "ndjson" in r.headers["content-type"]:
                rows: list[Any] = [json.loads(x) for x in r.text.splitlines()]
            else:
                body = r.json()
                rows = body["data"] if isinstance(body, dict) else body
            assert rows
            v = validator(name)
            for row in rows:
                assert not list(v.iter_errors(row)), (path, row)
        assert not list(validator("schema_info").iter_errors(c.get("/api/v1/analytics/schema", headers=h).json()))
        assert not list(validator("error").iter_errors(c.get("/api/v1/analytics/schema").json()))
    c = TestClient(env.funnels_app)
    ts = c.get(
        "/api/v1/analytics/funnels/fn_webinar/timeseries?metric=visits", headers={"Authorization": "Bearer fa_test_key"}
    ).json()
    assert not list(validator("timeseries").iter_errors(ts))


def test_consumer_models_accept_schema_valid_records_with_unknown_fields(world: World) -> None:
    for model, row in (
        (EventRec, world.events[0]),
        (LeadRec, world.leads[0]),
        (RoomRec, world.rooms[0]),
        (MessageMetaRec, world.messages_meta[0]),
    ):
        model.model_validate({**row, "brand_new_field": {"nested": [1, 2]}})
    StructureFunnelRec.model_validate({"funnel_id": "f", "steps": [{"step_id": "s", "future": 1}], "extra": True})


def test_ai_fields_tolerate_numeric_encodings(world: World) -> None:
    from app.features.norm import sentiment_value, urgency_value

    assert (
        sentiment_value("negative") < 0 < sentiment_value("positive")
        and sentiment_value(-0.4) == -0.4
        and sentiment_value(5) == 1.0
    )
    assert (
        urgency_value("high") == 0.8
        and urgency_value(0.3) == 0.3
        and urgency_value(4) == 0.8
        and urgency_value(None) != urgency_value(None)
    )
    RoomRec.model_validate({**world.rooms[0], "ai_sentiment": -0.3, "ai_urgency": 3})
