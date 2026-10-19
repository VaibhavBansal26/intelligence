"""LLM client protocol, error taxonomy and the Anthropic implementation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    code = "llm_error"


class LLMUnavailable(LLMError):
    code = "unavailable"


class LLMTimeout(LLMError):
    code = "timeout"


class LLMRefusal(LLMError):
    code = "refusal"


class LLMEmpty(LLMError):
    code = "empty"


class LLMBudgetExceeded(LLMError):
    code = "input_budget"


class LLMCapExceeded(LLMError):
    code = "daily_cap"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMResponse:
        return cls(
            text=d.get("text", ""),
            tool_calls=[ToolCall(**t) for t in d.get("tool_calls", [])],
            content_blocks=d.get("content_blocks", []),
            stop_reason=d.get("stop_reason", "end_turn"),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
        )


class LLMClient(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        timeout_s: float,
    ) -> LLMResponse: ...


class AnthropicClient:
    """Thin wrapper over the official SDK; maps SDK failures onto the LLMError taxonomy."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=1)
        self.model = model

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        timeout_s: float,
    ) -> LLMResponse:
        a = self._anthropic
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "timeout": timeout_s,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            resp = self._client.messages.create(**kwargs)
        except a.APITimeoutError as exc:
            raise LLMTimeout(str(exc)) from exc
        except a.APIError as exc:
            raise LLMError(f"{type(exc).__name__}") from exc
        if resp.stop_reason == "refusal":
            raise LLMRefusal("model refused")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        blocks: list[dict[str, Any]] = []
        for b in resp.content:
            if b.type == "text":
                text_parts.append(b.text)
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, dict(b.input)))
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        return LLMResponse(
            "".join(text_parts), calls, blocks, str(resp.stop_reason), resp.usage.input_tokens, resp.usage.output_tokens
        )
