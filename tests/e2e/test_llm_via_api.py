"""LLM paths through the real HTTP API with a fake client (never the network)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.llm.client import LLMResponse, LLMTimeout, ToolCall
from app.synthetic.world import World
from tests.conftest import Env, build_env
from tests.fakes import FakeLLM


@pytest.fixture(scope="module")
def llm_env(small_world: World, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Env, FakeLLM]]:
    fake = FakeLLM()
    yield from ((e, fake) for e in build_env(tmp_path_factory.mktemp("llm"), small_world, llm=fake, llm_cache_ttl_s=-1))


def test_insights_use_llm_when_numbers_are_grounded(llm_env: tuple[Env, FakeLLM]) -> None:
    env, fake = llm_env
    base = env.client.get("/v1/insights?narrate=false", headers=env.h).json()
    top = base["findings"][0]
    fake.script = [
        json.dumps(
            {
                "summary": "One step dominates the upside",
                "actions": [
                    {"finding_id": top["id"], "text": f"Rework this step: it is worth about {top['metrics']['expected_uplift']}."}
                ],
            }
        )
    ]
    r = env.client.get("/v1/insights", headers=env.h).json()
    assert r["narrative"]["source"] == "llm" and r["narrative"]["model"] == "fake-model"
    assert fake.calls and "Evidence JSON" in fake.calls[0]["messages"][0]["content"]


def test_insights_fall_back_when_llm_invents_numbers_or_fails(llm_env: tuple[Env, FakeLLM]) -> None:
    env, fake = llm_env
    top = env.client.get("/v1/insights?narrate=false", headers=env.h).json()["findings"][0]["id"]
    fake.script = [json.dumps({"summary": "Revenue will triple (300%)", "actions": [{"finding_id": top, "text": "Do it now."}]})]
    r = env.client.get("/v1/insights?narrate=true&x=1", headers=env.h).json()
    assert r["narrative"]["source"] == "template" and r["narrative"]["fallback_reason"] == "number_guard"
    fake.script = [LLMTimeout("slow")]
    r = env.client.get("/v1/insights?x=2", headers=env.h).json()
    assert r["narrative"]["fallback_reason"] == "timeout" and r["narrative"]["actions"]


def test_rerank_via_api_keeps_ml_order_unless_valid(llm_env: tuple[Env, FakeLLM]) -> None:
    env, fake = llm_env
    ml = env.client.get("/v1/rankings/steps", headers=env.h).json()["steps"]
    ids = [f"{s['funnel_id']}:{s['step_id']}" for s in ml[:5]]
    fake.script = [json.dumps({"order": ids[::-1]})]
    r = env.client.get("/v1/rankings/steps?rerank=true", headers=env.h).json()
    assert r["rerank"]["reranked"] is True
    assert [f"{s['funnel_id']}:{s['step_id']}" for s in r["steps"][:5]] == ids[::-1] and r["steps"][0]["ml_rank"] == 5
    fake.script = [json.dumps({"order": ["bogus", *ids[1:]]})]
    r = env.client.get("/v1/rankings/steps?rerank=true&y=1", headers=env.h).json()
    assert (
        r["rerank"] == {"reranked": False, "reason": "not_a_permutation"}
        and [f"{s['funnel_id']}:{s['step_id']}" for s in r["steps"][:5]] == ids
    )


def test_ask_uses_tools_via_api(llm_env: tuple[Env, FakeLLM]) -> None:
    env, fake = llm_env

    def script(system: str, messages: list[dict[str, Any]], tools: Any) -> LLMResponse:
        if len(messages) == 1:
            blk = {"type": "tool_use", "id": "t1", "name": "get_open_conversations", "input": {"limit": 1}}
            return LLMResponse(tool_calls=[ToolCall("t1", "get_open_conversations", {"limit": 1})], content_blocks=[blk])
        res = json.loads(messages[-1]["content"][0]["content"])
        return LLMResponse(text=f"{res['total_open']} conversations are open.")

    fake.script = script
    r = env.client.post("/v1/ask", json={"question": "how many conversations are open?"}, headers=env.h).json()
    assert r["source"] == "llm" and r["tools_used"] == ["get_open_conversations"]
