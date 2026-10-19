from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import models as m
from app.main import create_app
from app.synthetic.fake_sources import create_fake_conversations_app, create_fake_funnels_app
from app.synthetic.world import World, WorldConfig, generate_world

ADMIN_KEY = "test-admin-key-0123456789"
FUNNELS_URL = "http://funnels.fake.test"
CONV_URL = "http://conversations.fake.test"


def make_settings(tmp: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "database_url": f"sqlite:///{tmp}/test.db",
        "model_dir": str(tmp / "models"),
        "admin_api_key": ADMIN_KEY,
        "allow_private_sources": True,
        "rate_limit_burst": 100_000,
        "rate_limit_per_min": 100_000,
        "connector_backoff_base_s": 0.0,
        "log_level": "WARNING",
        "jwt_secret": "test-jwt-secret-0123456789abcdef0123",
    }
    base.update(over)
    return Settings(**base)


def fake_http_factory(funnels_app: FastAPI | None, conv_app: FastAPI | None) -> Callable[[m.Source], httpx.AsyncClient]:
    def make(src: m.Source) -> httpx.AsyncClient:
        app = funnels_app if src.kind == "funnels" else conv_app
        assert app is not None
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=src.base_url)

    return make


@dataclass
class Env:
    app: FastAPI
    client: TestClient
    world: World
    account_id: str
    admin: dict[str, str]
    key: str  # admin-role key of the account
    funnels_app: FastAPI
    conv_app: FastAPI

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}


def build_env(tmp: Path, world: World, *, train: bool = True, llm: Any = None, **settings_over: Any) -> Iterator[Env]:
    settings = make_settings(tmp, **settings_over)
    f_app = create_fake_funnels_app(world)
    c_app = create_fake_conversations_app(world)
    app = create_app(
        settings,
        http_factory=fake_http_factory(f_app, c_app),
        clock=lambda: float(world.end_ts),
        llm_client=llm,
        use_env_llm=False,
    )
    with TestClient(app) as client:
        admin = {"X-Admin-Key": ADMIN_KEY}
        r = client.post("/admin/accounts", json={"name": "Acme"}, headers=admin)
        assert r.status_code == 201, r.text
        key, acct = r.json()["api_key"], r.json()["account"]["id"]
        h = {"Authorization": f"Bearer {key}"}
        for kind, url, api_key in (("funnels", FUNNELS_URL, "fa_test_key"), ("conversations", CONV_URL, "ca_test_key")):
            r = client.post(
                "/v1/sources",
                json={"kind": kind, "name": kind, "base_url": url, "api_key": api_key, "identity_pepper": world.config.pepper},
                headers=h,
            )
            assert r.status_code == 201, r.text
        r = client.post("/v1/sync?wait=true", json={}, headers=h)
        assert r.status_code == 202 and r.json()["status"] == "succeeded", r.text
        if train:
            r = client.post("/v1/models/train?wait=true", json={}, headers=h)
            assert r.json()["status"] == "succeeded", r.text
        yield Env(app, client, world, acct, admin, key, f_app, c_app)


@pytest.fixture(scope="session")
def world() -> World:
    return generate_world(WorldConfig())


@pytest.fixture(scope="session")
def env(world: World, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    """Fully synced and trained environment, shared read-only across tests."""
    yield from build_env(tmp_path_factory.mktemp("env"), world)


@pytest.fixture(scope="session")
def small_world() -> World:
    return generate_world(WorldConfig(seed=11, n_days=45, visitors_per_day=60, support_only_contacts=150))


@pytest.fixture
def fresh_env(small_world: World, tmp_path: Path) -> Iterator[Env]:
    """Synced (untrained) environment that tests may mutate."""
    yield from build_env(tmp_path, small_world, train=False)
