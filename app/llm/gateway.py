"""Cost and safety controls around an LLMClient: input budget, prompt cache, daily caps, PII redaction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.clock import Clock
from app.config import Settings
from app.db import models as m
from app.db.session import SessionFactory, session_scope
from app.llm.client import LLMBudgetExceeded, LLMCapExceeded, LLMClient, LLMResponse, LLMUnavailable
from app.llm.guard import redact_pii


class LLMGateway:
    def __init__(self, client: LLMClient | None, settings: Settings, sessions: SessionFactory, clock: Clock) -> None:
        self._client = client
        self._settings = settings
        self._sessions = sessions
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._client.model if self._client else "template"

    def _redact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._settings.llm_redact_pii:
            return messages
        return json.loads(redact_pii(json.dumps(messages)))

    def complete(
        self,
        account_id: str,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if self._client is None:
            raise LLMUnavailable("no ANTHROPIC_API_KEY configured")
        msgs = self._redact(messages)
        payload = json.dumps({"s": system, "m": msgs, "t": tools}, sort_keys=True)
        if len(payload) > self._settings.llm_max_input_chars:
            raise LLMBudgetExceeded(f"prompt is {len(payload)} chars (> {self._settings.llm_max_input_chars})")
        key = hashlib.sha256(f"{self._client.model}|{payload}".encode()).hexdigest()
        now = int(self._clock())
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        with session_scope(self._sessions) as s:
            cached = s.get(m.LlmCache, key)
            usage = s.get(m.LlmUsage, (account_id, day))
            if (
                cached is not None
                and cached.account_id == account_id
                and now - cached.created_at <= self._settings.llm_cache_ttl_s
            ):
                if usage is None:
                    usage = m.LlmUsage(account_id=account_id, day=day, calls=0, input_tokens=0, output_tokens=0, cache_hits=0)
                    s.add(usage)
                usage.cache_hits += 1
                return LLMResponse.from_dict(cached.response)
            if usage and (
                usage.calls >= self._settings.llm_daily_call_cap
                or usage.input_tokens + usage.output_tokens >= self._settings.llm_daily_token_cap
            ):
                raise LLMCapExceeded("daily LLM cap reached for this account")
        resp = self._client.complete(
            system=system,
            messages=msgs,
            tools=tools,
            max_tokens=max_tokens or self._settings.llm_max_output_tokens,
            timeout_s=self._settings.llm_timeout_s,
        )
        with session_scope(self._sessions) as s:
            usage = s.get(m.LlmUsage, (account_id, day))
            if usage is None:
                usage = m.LlmUsage(account_id=account_id, day=day, calls=0, input_tokens=0, output_tokens=0, cache_hits=0)
                s.add(usage)
            usage.calls += 1
            usage.input_tokens += resp.input_tokens
            usage.output_tokens += resp.output_tokens
            if resp.text.strip() or resp.tool_calls:
                row = s.execute(select(m.LlmCache).where(m.LlmCache.key == key)).scalar_one_or_none()
                if row is None:
                    s.add(m.LlmCache(key=key, account_id=account_id, response=resp.to_dict(), created_at=now))
                else:
                    row.response, row.created_at = resp.to_dict(), now
        return resp
