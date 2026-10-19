from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.main import create_app
from app.synthetic.world import World
from tests.conftest import ADMIN_KEY, FUNNELS_URL, Env, build_env, make_settings


def mint(env: Env, role: str, scopes: list[str] | None = None) -> dict[str, str]:
    r = env.client.post("/v1/api-keys", json={"name": role, "role": role, "scopes": scopes or []}, headers=env.h)
    assert r.status_code == 201, r.text
    assert "api_key" in r.json()
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def second_account(env: Env, name: str = "Other") -> dict[str, str]:
    r = env.client.post("/admin/accounts", json={"name": name}, headers=env.admin)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


# --- authentication ----------------------------------------------------------------------------------------
def test_every_v1_route_requires_authentication(env: Env) -> None:
    spec = env.client.get("/openapi.json").json()
    public = {"/v1/health", "/v1/ready"}
    checked = 0
    for path, ops in spec["paths"].items():
        if not path.startswith("/v1") or path in public:
            continue
        for method in ops:
            url = (
                path.replace("{source_id}", "x")
                .replace("{job_id}", "x")
                .replace("{key_id}", "x")
                .replace("{account_id}", "x")
                .replace("{version_id}", "x")
                .replace("{model_type}", "segments")
            )
            r = env.client.request(method.upper(), url, json={} if method != "get" else None)
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.json()["error"]["code"] == "UNAUTHORIZED"
            checked += 1
    assert checked >= 30


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer isk_bad_key"},
        {"Authorization": "Bearer not.a.jwt"},
    ],
)
def test_bad_credentials_rejected(env: Env, header: dict[str, str]) -> None:
    assert env.client.get("/v1/account", headers=header).status_code == 401


def test_key_lifecycle_revoke_expiry_and_hashed_storage(fresh_env: Env) -> None:
    e = fresh_env
    r = e.client.post("/v1/api-keys", json={"name": "k", "role": "viewer"}, headers=e.h).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    assert e.client.get("/v1/account", headers=h).status_code == 200
    from sqlalchemy import select

    from app.db import models as m
    from app.db.session import session_scope

    with session_scope(e.app.state.container.sessions) as s:
        row = s.execute(select(m.ApiKey).where(m.ApiKey.id == r["id"])).scalar_one()
        assert r["api_key"] not in (row.key_hash, row.prefix) and len(row.key_hash) == 64
    assert e.client.delete(f"/v1/api-keys/{r['id']}", headers=e.h).status_code == 204
    assert e.client.get("/v1/account", headers=h).status_code == 401
    expired = e.client.post("/v1/api-keys", json={"name": "old", "role": "viewer", "expires_at": 1000}, headers=e.h).json()
    assert e.client.get("/v1/account", headers={"Authorization": f"Bearer {expired['api_key']}"}).status_code == 401
    assert "api_key" not in e.client.get("/v1/api-keys", headers=e.h).text


def test_jwt_exchange_and_validation(fresh_env: Env) -> None:
    e = fresh_env
    viewer = mint(e, "viewer")
    tok = e.client.post("/v1/auth/token", headers=viewer).json()
    jh = {"Authorization": f"Bearer {tok['access_token']}"}
    assert e.client.get("/v1/segments", headers=jh).status_code == 200
    assert e.client.post("/v1/sync", json={}, headers=jh).status_code == 403  # role carried in the token
    assert e.client.post("/v1/auth/token", headers=jh).status_code == 422
    secret = e.app.state.container.settings.jwt_secret
    expired = jwt.encode(
        {"account_id": e.account_id, "role": "admin", "exp": int(e.world.end_ts) - 100, "iss": "intelligence"},
        secret,
        algorithm="HS256",
    )
    forged = jwt.encode(
        {"account_id": e.account_id, "role": "admin", "exp": int(e.world.end_ts) + 100, "iss": "intelligence"},
        "wrong-secret-wrong-secret-wrong!",
        algorithm="HS256",
    )
    escalated = jwt.encode(
        {
            "account_id": e.account_id,
            "role": "admin",
            "perms": ["data:read"],
            "exp": int(e.world.end_ts) + 100,
            "iss": "intelligence",
        },
        secret,
        algorithm="HS256",
    )
    for bad in (expired, forged):
        assert e.client.get("/v1/account", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert e.client.post("/v1/sync", json={}, headers={"Authorization": f"Bearer {escalated}"}).status_code == 403


# --- RBAC matrix --------------------------------------------------------------------------------------------
MATRIX: list[tuple[str, str, dict[str, Any] | None, set[str]]] = [
    ("GET", "/v1/segments", None, {"viewer", "analyst", "admin"}),
    ("GET", "/v1/rankings/funnels", None, {"viewer", "analyst", "admin"}),
    ("GET", "/v1/leads/scores?limit=1", None, {"viewer", "analyst", "admin"}),
    ("POST", "/v1/ask", {"question": "which funnel first?"}, {"viewer", "analyst", "admin"}),
    ("GET", "/v1/models", None, {"viewer", "analyst", "admin"}),
    ("POST", "/v1/sync", {}, {"analyst", "admin"}),
    ("POST", "/v1/models/train", {"model_types": ["segments"]}, {"analyst", "admin"}),
    ("POST", "/v1/models/types/segments/rollback", None, {"admin"}),
    ("POST", "/v1/models/nope/promote", None, {"admin"}),
    (
        "POST",
        "/v1/sources",
        {"kind": "funnels", "name": "dup", "base_url": FUNNELS_URL, "api_key": "k" * 10, "identity_pepper": "p" * 10},
        {"admin"},
    ),
    ("PATCH", "/v1/account", {"name": "Renamed"}, {"admin"}),
    ("GET", "/v1/api-keys", None, {"admin"}),
    ("POST", "/v1/api-keys", {"name": "n", "role": "viewer"}, {"admin"}),
    ("DELETE", "/v1/sources/nope", None, {"admin"}),
]


@pytest.fixture(scope="module")
def rbac_env(small_world: World, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    yield from build_env(tmp_path_factory.mktemp("rbac"), small_world, train=False)


@pytest.mark.parametrize("role", ["viewer", "analyst", "admin"])
@pytest.mark.parametrize(("method", "path", "body", "allowed"), MATRIX)
def test_rbac_matrix(rbac_env: Env, role: str, method: str, path: str, body: Any, allowed: set[str]) -> None:
    h = mint(rbac_env, role)
    r = rbac_env.client.request(method, path, json=body, headers=h)
    if role in allowed:
        assert r.status_code != 403 and r.status_code != 401, (role, path, r.text)
    else:
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_scoped_key_cannot_exceed_its_scopes(fresh_env: Env) -> None:
    h = mint(fresh_env, "admin", scopes=["data:read"])
    assert fresh_env.client.get("/v1/segments", headers=h).status_code == 200
    assert fresh_env.client.get("/v1/api-keys", headers=h).status_code == 403
    assert (
        fresh_env.client.post(
            "/v1/api-keys", json={"name": "x", "role": "viewer", "scopes": ["root"]}, headers=fresh_env.h
        ).status_code
        == 422
    )


# --- tenant isolation ----------------------------------------------------------------------------------------------
def test_cross_account_access_is_404_everywhere(fresh_env: Env) -> None:
    e = fresh_env
    other = second_account(e)
    src = e.client.get("/v1/sources", headers=e.h).json()["items"][0]["id"]
    key_id = e.client.get("/v1/api-keys", headers=e.h).json()[0]["id"]
    job_id = e.client.get("/v1/sync/status", headers=e.h).json()["jobs"][0]["id"]
    e.client.post("/v1/models/train?wait=true", json={"model_types": ["segments"]}, headers=e.h)
    model_id = e.client.get("/v1/models", headers=e.h).json()[0]["id"]
    attempts = [
        ("GET", f"/v1/sources/{src}", None),
        ("PATCH", f"/v1/sources/{src}", {"name": "pwn"}),
        ("DELETE", f"/v1/sources/{src}", None),
        ("POST", "/v1/sync", {"source_id": src}),
        ("GET", f"/v1/jobs/{job_id}", None),
        ("DELETE", f"/v1/api-keys/{key_id}", None),
        ("POST", f"/v1/models/{model_id}/promote", None),
        ("GET", f"/v1/accounts/{e.account_id}", None),
    ]
    for method, path, body in attempts:
        r = e.client.request(method, path, json=body, headers=other)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "NOT_FOUND"
    # nothing leaked through listings, rankings or model registry
    assert e.client.get("/v1/sources", headers=other).json()["items"] == []
    assert e.client.get("/v1/models", headers=other).json() == []
    assert e.client.get("/v1/rankings/funnels", headers=other).json()["steps"] == []
    assert e.client.get("/v1/leads/scores", headers=other).json()["items"] == []
    assert e.client.get("/v1/sync/status", headers=other).json() == {"sources": [], "jobs": []}
    assert e.client.get("/v1/segments", headers=other).json()["segments"] == []
    assert (
        e.client.post("/v1/leads/score", json={"contact_key": e.world.leads[0]["contact_key"]}, headers=other).status_code == 404
    )
    # the original account is untouched
    assert e.client.get(f"/v1/sources/{src}", headers=e.h).json()["name"] != "pwn"
    ask = e.client.post("/v1/ask", json={"question": "which funnel first?"}, headers=other).json()
    assert "Webinar" not in ask["answer"]


# --- errors, validation, headers ---------------------------------------------------------------------------------------
def test_error_model_and_validation(env: Env) -> None:
    r = env.client.get("/v1/nothing", headers=env.h)
    assert r.status_code == 404 and set(r.json()) == {"error"} and r.json()["error"]["code"] == "NOT_FOUND"
    r = env.client.post("/v1/ask", json={"question": "x"}, headers=env.h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR" and "question" in r.json()["error"]["message"]
    assert env.client.get("/v1/leads/scores?limit=9999", headers=env.h).status_code == 422
    assert (
        env.client.post("/v1/leads/score", json={"email": "a@b.co", "contact_key": "x"}, headers=env.h).json()["error"]["code"]
        == "INVALID_INPUT"
    )
    assert env.client.post("/v1/leads/score", json={"unknown": 1}, headers=env.h).status_code == 422
    assert env.client.get("/v1/segments", headers=env.h, params={}).status_code == 200
    assert env.client.delete("/v1/segments", headers=env.h).status_code == 405
    big = env.client.post("/v1/ask", content=b"x" * 2_000_000, headers={**env.h, "Content-Type": "application/json"})
    assert big.status_code == 413 and big.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_request_id_and_security_headers(env: Env) -> None:
    r = env.client.get("/v1/health", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    assert env.client.get("/v1/health", headers={"X-Request-ID": "bad id\r\n<script>"}).headers["x-request-id"] != "bad id"
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors" in r.headers["content-security-policy"]
    assert r.json()["status"] == "ok"


def test_ready_and_metrics(env: Env) -> None:
    r = env.client.get("/v1/ready").json()
    assert r["status"] == "ready" and r["checks"]["database"] == "ok"
    text = env.client.get("/metrics").text
    assert "intelligence_http_requests_total" in text and "isk_" not in text


def test_metrics_token_enforced(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, metrics_token="m-secret"), use_env_llm=False)
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer m-secret"}).status_code == 200


def test_rate_limit_returns_429_with_retry_after(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, rate_limit_per_min=60, rate_limit_burst=3), use_env_llm=False)
    with TestClient(app) as c:
        codes = [c.get("/v1/account", headers={"Authorization": "Bearer isk_x_y"}).status_code for _ in range(5)]
        assert codes[:3] == [401, 401, 401] and 429 in codes
        r = c.get("/v1/account", headers={"Authorization": "Bearer isk_x_y"})
        assert r.status_code == 429 and "retry-after" in r.headers and r.json()["error"]["code"] == "RATE_LIMITED"


# --- idempotency, ETag, pagination ---------------------------------------------------------------------------------------
def test_idempotent_posts(fresh_env: Env) -> None:
    e = fresh_env
    body = {"kind": "funnels", "name": "second", "base_url": FUNNELS_URL, "api_key": "k" * 10, "identity_pepper": "p" * 10}
    h = {**e.h, "Idempotency-Key": "abc-123"}
    r1 = e.client.post("/v1/sources", json=body, headers=h)
    r2 = e.client.post("/v1/sources", json=body, headers=h)
    assert (
        r1.status_code == r2.status_code == 201
        and r1.json()["id"] == r2.json()["id"]
        and r2.headers["idempotent-replay"] == "true"
    )
    assert len(e.client.get("/v1/sources", headers=e.h).json()["items"]) == 3
    assert e.client.post("/v1/sources", json={**body, "name": "different"}, headers=h).status_code == 409
    dup = e.client.post("/v1/sources", json=body, headers=e.h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "SOURCE_EXISTS"
    s1 = e.client.post("/v1/sync", json={}, headers={**e.h, "Idempotency-Key": "sync-1"}).json()
    s2 = e.client.post("/v1/sync", json={}, headers={**e.h, "Idempotency-Key": "sync-1"}).json()
    assert s1["id"] == s2["id"]


def test_etag_invalidates_after_data_changes(fresh_env: Env) -> None:
    e = fresh_env
    r1 = e.client.get("/v1/rankings/funnels", headers=e.h)
    assert e.client.get("/v1/rankings/funnels", headers={**e.h, "If-None-Match": r1.headers["etag"]}).status_code == 304
    e.client.post("/v1/sync?wait=true", json={}, headers=e.h)
    assert e.client.get("/v1/rankings/funnels", headers={**e.h, "If-None-Match": r1.headers["etag"]}).status_code == 200


def test_pagination(env: Env) -> None:
    p1 = env.client.get("/v1/leads/scores?limit=5&offset=0", headers=env.h).json()
    p2 = env.client.get("/v1/leads/scores?limit=5&offset=5", headers=env.h).json()
    assert p1["total"] == p2["total"] > 10 and len(p1["items"]) == 5
    assert p1["items"][-1]["rank"] == 5 and p2["items"][0]["rank"] == 6
    assert p1["items"][-1]["score"] >= p2["items"][0]["score"]
    s = env.client.get("/v1/sources?limit=1", headers=env.h).json()
    assert s["total"] == 2 and len(s["items"]) == 1


# --- sources / SSRF -----------------------------------------------------------------------------------------------------------
def test_source_url_ssrf_enforced_via_api(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, allow_private_sources=False), resolver=lambda h, p: ["10.0.0.7"], use_env_llm=False)
    with TestClient(app) as c:
        key = c.post("/admin/accounts", json={"name": "a"}, headers={"X-Admin-Key": ADMIN_KEY}).json()["api_key"]
        h = {"Authorization": f"Bearer {key}"}
        for url in ("http://169.254.169.254", "https://internal.example.com", "http://localhost:9"):
            r = c.post(
                "/v1/sources",
                json={"kind": "funnels", "name": "x", "base_url": url, "api_key": "k" * 10, "identity_pepper": "p" * 10},
                headers=h,
            )
            assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_SOURCE_URL", url
        assert c.get("/v1/sources", headers=h).json()["total"] == 0


def test_source_crud_never_returns_secrets(fresh_env: Env) -> None:
    e = fresh_env
    src = e.client.get("/v1/sources", headers=e.h).json()["items"][0]
    assert set(src) == {"id", "kind", "name", "base_url", "status", "pepper_fingerprint", "last_schema_version", "created_at"}
    r = e.client.patch(f"/v1/sources/{src['id']}", json={"status": "paused"}, headers=e.h)
    assert r.json()["status"] == "paused"
    assert e.client.patch(f"/v1/sources/{src['id']}", json={"status": "weird"}, headers=e.h).status_code == 422
    assert e.client.delete(f"/v1/sources/{src['id']}", headers=e.h).status_code == 204
    assert e.client.get(f"/v1/sources/{src['id']}", headers=e.h).status_code == 404
    assert e.client.get("/v1/sources", headers=e.h).json()["total"] == 1


# --- admin API ---------------------------------------------------------------------------------------------------------------------
def test_admin_api(fresh_env: Env) -> None:
    e = fresh_env
    assert e.client.get("/admin/accounts").status_code == 401
    assert e.client.get("/admin/accounts", headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert (
        e.client.get("/admin/accounts", headers={"Authorization": f"Bearer {e.key}"}).status_code == 401
    )  # tenant keys are not admin keys
    accts = e.client.get("/admin/accounts", headers=e.admin).json()
    assert accts["total"] == 1
    usage = e.client.get(f"/admin/accounts/{e.account_id}/usage", headers=e.admin).json()
    assert usage["events"] == len(e.world.events) and usage["sources"] == 2
    r = e.client.post(f"/admin/accounts/{e.account_id}/retrain?wait=true", headers=e.admin).json()
    assert r["status"] == "succeeded"
    models = e.client.get("/admin/models", headers=e.admin).json()
    assert {m["model_type"] for m in models} == {"lead_conversion", "sla_breach", "segments"}
    assert e.client.get("/admin/jobs?status=succeeded", headers=e.admin).json()
    assert e.client.get("/admin/accounts/nope/usage", headers=e.admin).status_code == 404
    e.client.patch(f"/admin/accounts/{e.account_id}", json={"disabled": True}, headers=e.admin)
    assert e.client.get("/v1/account", headers=e.h).status_code == 401


def test_admin_disabled_without_configured_key(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, admin_api_key=""), use_env_llm=False)
    with TestClient(app) as c:
        assert c.get("/admin/accounts", headers={"X-Admin-Key": ""}).status_code == 401


# --- OpenAPI contract --------------------------------------------------------------------------------------------------------------------
CONTRACT = [
    ("/v1/account", "AccountOut"),
    ("/v1/sources", "SourceList"),
    ("/v1/sync/status", "SyncStatus"),
    ("/v1/rankings/funnels", "RankingResponse"),
    ("/v1/rankings/steps", "RankingResponse"),
    ("/v1/leads/scores?limit=3", "LeadScoresResponse"),
    ("/v1/rankings/conversations/open", "OpenConversationsResponse"),
    ("/v1/segments", "SegmentsResponse"),
    ("/v1/anomalies", "AnomaliesResponse"),
    ("/v1/insights", "InsightsResponse"),
    ("/v1/health", "Health"),
    ("/v1/ready", "Ready"),
]


@pytest.mark.parametrize(("path", "schema"), CONTRACT)
def test_responses_conform_to_openapi_schema(env: Env, path: str, schema: str) -> None:
    spec = env.client.get("/openapi.json").json()
    r = env.client.get(path, headers=env.h)
    assert r.status_code == 200
    Draft202012Validator({"$ref": f"#/components/schemas/{schema}", "components": spec["components"]}).validate(r.json())


def test_models_and_ask_and_errors_conform(env: Env) -> None:
    spec = env.client.get("/openapi.json").json()
    comps = spec["components"]
    for m in env.client.get("/v1/models", headers=env.h).json():
        Draft202012Validator({"$ref": "#/components/schemas/ModelOut", "components": comps}).validate(m)
    Draft202012Validator({"$ref": "#/components/schemas/AskResponse", "components": comps}).validate(
        env.client.post("/v1/ask", json={"question": "fix first?"}, headers=env.h).json()
    )
    Draft202012Validator({"$ref": "#/components/schemas/ErrorResponse", "components": comps}).validate(
        env.client.get("/v1/jobs/none", headers=env.h).json()
    )


def test_openapi_documents_every_route_with_tags_and_security_errors(env: Env) -> None:
    spec = env.client.get("/openapi.json").json()
    assert spec["info"]["title"] == "Intelligence" and len(spec["paths"]) >= 34
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert op.get("tags"), (method, path)
            if path.startswith("/v1") and path not in ("/v1/health", "/v1/ready"):
                assert "401" in op["responses"] or "403" in op["responses"], (method, path)
