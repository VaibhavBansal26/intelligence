"""Test doubles: a scriptable fake LLM client."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.llm.client import LLMResponse


class FakeLLM:
    """`script` is a list of responses/exceptions consumed in order, or a callable(system, messages, tools)."""

    model = "fake-model"

    def __init__(self, script: list[Any] | Callable[..., Any] | None = None) -> None:
        self.script = script if script is not None else []
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        timeout_s: float,
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        item = self.script(system, messages, tools) if callable(self.script) else self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return LLMResponse(text=item, input_tokens=100, output_tokens=50)
        return item
