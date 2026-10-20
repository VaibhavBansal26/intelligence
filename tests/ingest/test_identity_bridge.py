"""Identity bridge: the real apps each generate their own random identity pepper (found by the integration run), so
the same e-mail has a different contact_key in each app. A customer-supplied contact list links them."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import models as m
from app.db.session import session_scope
from app.security.crypto import contact_key
from app.synthetic.world import WorldConfig, generate_world
from tests.conftest import Env, build_env

CONV_PEPPER = "a-different-pepper"


@pytest.fixture
def mismatched(tmp_path: Path) -> Iterator[Env]:
    world = generate_world(
        WorldConfig(seed=5, n_days=45, visitors_per_day=60, support_only_contacts=150, conversations_pepper=CONV_PEPPER)
    )
    gen = build_env(tmp_path, world, train=False)
    env = next(gen)
    conv = next(s for s in env.client.get("/v1/sources", headers=env.h).json()["items"] if s["kind"] == "conversations")
    env.client.patch(f"/v1/sources/{conv['id']}", json={"identity_pepper": CONV_PEPPER}, headers=env.h)
    yield env
    gen.close()


def emails(n: int = 8000) -> list[str]:
    return [f"user{i}@example.test" for i in range(1, n)]


def test_without_a_bridge_nothing_joins_and_the_report_points_to_the_bridge(mismatched: Env) -> None:
    j = mismatched.client.get("/v1/join", headers=mismatched.h).json()
    assert j["joined_contacts"] == 0 and j["bridge_links"] == 0
    assert any("POST /v1/identity/bridge" in w for w in j["warnings"])


def test_bridge_joins_the_overlap_and_new_rows_are_rekeyed_on_arrival(mismatched: Env) -> None:
    env = mismatched
    r = env.client.post(
        "/v1/identity/bridge", json={"emails": [*emails(), " User1@Example.test ", "not-an-email"]}, headers=env.h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rooms_rekeyed"] > 0 and body["emails_usable"] == len(set(emails()))
    j = env.client.get("/v1/join", headers=env.h).json()
    w = env.world
    f_keys = {e["contact_key"] for e in w.events if e["contact_key"]} | {ld["contact_key"] for ld in w.leads}
    c_keys = {r["contact_key"] for r in w.rooms if r["contact_key"]}
    expected = sum(1 for e in emails() if contact_key(w.config.pepper, e) in f_keys and contact_key(CONV_PEPPER, e) in c_keys)
    assert expected > 20
    assert j["joined_contacts"] == expected
    assert any(w.startswith("identity_bridge_active") for w in j["warnings"]) and not any(
        "pepper_mismatch_suspected" in w for w in j["warnings"]
    )
    # a full re-sync re-reads every room with its ORIGINAL key: ingest must map it again
    assert env.client.post("/v1/sync?wait=true", json={"full": True}, headers=env.h).json()["status"] == "succeeded"
    j2 = env.client.get("/v1/join", headers=env.h).json()
    assert j2["joined_contacts"] == j["joined_contacts"]
    status = env.client.get("/v1/identity/bridge", headers=env.h).json()
    assert status["total_links"] == sum(body["links_by_source"].values()) > 0
    c = env.app.state.container
    with session_scope(c.sessions) as s:
        raw = int(s.execute(select(func.count()).select_from(m.Room).where(m.Room.contact_key_raw.is_not(None))).scalar() or 0)
    assert raw > 0  # the original key is kept next to the canonical one


def test_changing_a_source_pepper_drops_its_links(mismatched: Env) -> None:
    env = mismatched
    env.client.post("/v1/identity/bridge", json={"emails": emails()}, headers=env.h)
    conv = next(s for s in env.client.get("/v1/sources", headers=env.h).json()["items"] if s["kind"] == "conversations")
    env.client.patch(f"/v1/sources/{conv['id']}", json={"identity_pepper": "rotated-pepper-value"}, headers=env.h)
    assert env.client.get("/v1/identity/bridge", headers=env.h).json()["total_links"] == 0
    j = env.client.get("/v1/join", headers=env.h).json()
    assert j["joined_contacts"] == 0  # rooms went back to their original keys


def test_bridge_needs_two_sources_and_a_usable_list(fresh_env: Env) -> None:
    r = fresh_env.client.post("/v1/identity/bridge", json={"emails": ["no-at-sign"]}, headers=fresh_env.h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "EMPTY_BRIDGE"
    for s in fresh_env.client.get("/v1/sources", headers=fresh_env.h).json()["items"][:1]:
        fresh_env.client.delete(f"/v1/sources/{s['id']}", headers=fresh_env.h)
    r = fresh_env.client.post("/v1/identity/bridge", json={"emails": ["a@example.test"]}, headers=fresh_env.h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "BRIDGE_NEEDS_TWO_SOURCES"


def test_bridge_is_account_scoped_and_needs_the_sources_permission(mismatched: Env) -> None:
    env = mismatched
    viewer = env.client.post("/v1/api-keys", json={"name": "v", "role": "viewer"}, headers=env.h).json()["api_key"]
    r = env.client.post("/v1/identity/bridge", json={"emails": emails(50)}, headers={"Authorization": f"Bearer {viewer}"})
    assert r.status_code == 403
    other = env.client.post("/admin/accounts", json={"name": "Other"}, headers=env.admin).json()["api_key"]
    j = env.client.get("/v1/identity/bridge", headers={"Authorization": f"Bearer {other}"}).json()
    assert j["total_links"] == 0


def test_calls_accumulate_so_a_long_list_can_be_sent_in_chunks_and_replace_starts_over(mismatched: Env) -> None:
    env = mismatched
    everyone = emails()
    one_shot = env.client.post("/v1/identity/bridge", json={"emails": everyone, "replace": True}, headers=env.h).json()
    joined_full = env.client.get("/v1/join", headers=env.h).json()["joined_contacts"]
    env.client.post("/v1/identity/bridge", json={"emails": [everyone[0]], "replace": True}, headers=env.h)
    assert env.client.get("/v1/identity/bridge", headers=env.h).json()["total_links"] == 1
    half = len(everyone) // 2
    env.client.post("/v1/identity/bridge", json={"emails": everyone[:half], "replace": True}, headers=env.h)
    env.client.post("/v1/identity/bridge", json={"emails": everyone[half:]}, headers=env.h)  # second chunk adds
    assert env.client.get("/v1/identity/bridge", headers=env.h).json()["total_links"] == sum(one_shot["links_by_source"].values())
    assert env.client.get("/v1/join", headers=env.h).json()["joined_contacts"] == joined_full
