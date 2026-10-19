"""HTTP client for the Analytics API contract: retries with jitter, 429 handling, NDJSON paging with resume."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/analytics"
RETRY_STATUS = {500, 502, 503, 504}


class ConnectorError(Exception):
    retryable = False


class ConnectorAuthError(ConnectorError):
    """401/403: the stored API key is invalid, revoked or lacks scope."""


class ConnectorNotFound(ConnectorError):
    pass


class ConnectorSchemaError(ConnectorError):
    """The remote payload does not look like the contract."""


class BudgetExceeded(ConnectorError):
    """A per-account resource budget (landed rows) was reached; not retryable."""


class ConnectorUnavailable(ConnectorError):
    retryable = True


class ConnectorRateLimited(ConnectorError):
    retryable = True

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class StreamStats:
    pages: int = 0
    records: int = 0
    bad_lines: int = 0
    retries: int = 0


PageCallback = Callable[[str | None], Awaitable[None]]


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError):
            return None


def unwrap_list(payload: Any) -> tuple[list[Any], str | None]:
    """Accept `[...]` or `{data:[...], next_cursor}`; ignore everything else tolerantly."""
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("data", "items", "funnels", "steps", "points"):
            if isinstance(payload.get(key), list):
                nxt = payload.get("next_cursor")
                return payload[key], str(nxt) if nxt else None
    raise ConnectorSchemaError("expected a list or {data:[...]} payload")


class SourceClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http: httpx.AsyncClient,
        timeout_s: float = 30.0,
        max_attempts: int = 5,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 30.0,
        max_retry_after_s: float = 60.0,
        max_bad_line_ratio: float = 0.05,
        page_limit: int = 1000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + API_PREFIX
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "gzip", "User-Agent": "intelligence/0.1"}
        self._timeout = httpx.Timeout(timeout_s)
        self._max_attempts = max_attempts
        self._base_s = backoff_base_s
        self._max_s = backoff_max_s
        self._max_retry_after = max_retry_after_s
        self._max_bad_ratio = max_bad_line_ratio
        self._page_limit = min(page_limit, 1000)
        self._sleep = sleep
        self._rand = rand
        import time

        self._now = now or time.time

    # -- low level ---------------------------------------------------------------------------------
    def _classify(self, resp: httpx.Response) -> None:
        code = resp.status_code
        if code < 400:
            return
        if code in (401, 403):
            raise ConnectorAuthError(f"source rejected credentials (HTTP {code})")
        if code == 404:
            raise ConnectorNotFound("source resource not found")
        if code == 429:
            ra = _retry_after(resp.headers.get("Retry-After"), self._now()) or 1.0
            raise ConnectorRateLimited("source rate limited us", ra)
        if code in RETRY_STATUS:
            raise ConnectorUnavailable(f"source error HTTP {code}")
        raise ConnectorError(f"unexpected HTTP {code} from source")

    async def _backoff(self, attempt: int, exc: ConnectorError | None) -> None:
        if isinstance(exc, ConnectorRateLimited):
            if exc.retry_after_s > self._max_retry_after:
                raise exc
            delay = exc.retry_after_s
        else:
            delay = self._rand() * min(self._max_s, self._base_s * (2**attempt))
        await self._sleep(delay)

    async def _with_retries(self, op: Callable[[], Awaitable[Any]], stats: StreamStats | None = None) -> Any:
        last: ConnectorError | None = None
        for attempt in range(self._max_attempts):
            try:
                return await op()
            except ConnectorError as exc:
                if not exc.retryable:
                    raise
                last = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = ConnectorUnavailable(f"transport error: {type(exc).__name__}")
            if attempt == self._max_attempts - 1:
                break
            if stats:
                stats.retries += 1
            await self._backoff(attempt, last)
        assert last is not None
        raise last

    # -- JSON ----------------------------------------------------------------------------------------
    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async def op() -> Any:
            resp = await self._http.get(self._base + path, params=params, headers=self._headers, timeout=self._timeout)
            self._classify(resp)
            try:
                return resp.json()
            except ValueError as exc:
                raise ConnectorSchemaError("response is not valid JSON") from exc

        return await self._with_retries(op)

    async def list_all(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 10_000) -> list[Any]:
        out: list[Any] = []
        cursor: str | None = None
        for _ in range(max_pages):
            q = {**(params or {}), "limit": self._page_limit}
            if cursor:
                q["cursor"] = cursor
            items, nxt = unwrap_list(await self.get_json(path, q))
            out.extend(items)
            if not nxt or nxt == cursor:
                return out
            cursor = nxt
        raise ConnectorSchemaError("pagination did not terminate")

    async def get_records(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetch a small collection that may be served as a JSON list, `{data:[]}` envelope or NDJSON."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(1000):
            q = dict(params or {})
            if cursor:
                q["cursor"] = cursor

            async def op(q: dict[str, Any] = q) -> tuple[list[Any], str | None]:
                resp = await self._http.get(self._base + path, params=q, headers=self._headers, timeout=self._timeout)
                self._classify(resp)
                text = resp.text
                try:
                    return unwrap_list(json.loads(text))
                except ValueError:
                    items = []
                    for line in text.splitlines():
                        if line.strip():
                            try:
                                items.append(json.loads(line))
                            except ValueError:
                                continue
                    return items, resp.headers.get("X-Next-Cursor") or None

            items, nxt = await self._with_retries(op)
            out.extend(i for i in items if isinstance(i, dict))
            if not nxt or nxt == cursor:
                return out
            cursor = nxt
        raise ConnectorSchemaError("pagination did not terminate")

    async def schema(self) -> dict[str, Any] | None:
        try:
            payload = await self.get_json("/schema")
        except ConnectorNotFound:
            return None
        return payload if isinstance(payload, dict) else None

    # -- NDJSON ---------------------------------------------------------------------------------------
    async def stream_ndjson(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cursor: str | None = None,
        on_page: PageCallback | None = None,
        stats: StreamStats | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield records page by page. A page is only released after it was read completely, so a
        truncated stream is retried from the same cursor without emitting duplicates. `on_page`
        receives the cursor of the NEXT page (None when finished) after each page has been consumed."""
        stats = stats if stats is not None else StreamStats()
        seen: set[str] = set()
        while True:
            page_cursor = cursor
            records, next_cursor = await self._with_retries(lambda c=page_cursor: self._read_page(path, params, c, stats), stats)  # type: ignore[misc]
            stats.pages += 1
            for rec in records:
                stats.records += 1
                yield rec
            if on_page:
                await on_page(next_cursor)
            if not next_cursor:
                return
            if next_cursor == page_cursor or next_cursor in seen:
                raise ConnectorSchemaError("cursor did not advance")
            seen.add(next_cursor)
            cursor = next_cursor

    async def _read_page(
        self, path: str, params: dict[str, Any] | None, cursor: str | None, stats: StreamStats
    ) -> tuple[list[dict[str, Any]], str | None]:
        q: dict[str, Any] = {**(params or {}), "limit": self._page_limit}
        if cursor:
            q["cursor"] = cursor
        records: list[dict[str, Any]] = []
        bad = 0
        total = 0
        last_line_bad = False
        async with self._http.stream(
            "GET", self._base + path, params=q, headers={**self._headers, "Accept": "application/x-ndjson"}, timeout=self._timeout
        ) as resp:
            self._classify(resp)
            next_cursor = resp.headers.get("X-Next-Cursor") or None
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                total += 1
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError("not an object")
                    records.append(obj)
                    last_line_bad = False
                except ValueError:
                    bad += 1
                    last_line_bad = True
        if last_line_bad:
            # most likely a stream cut mid-record; re-request the whole page (rows are upserted idempotently)
            raise ConnectorUnavailable("truncated NDJSON stream")
        if total and bad / total > self._max_bad_ratio and bad > 3:
            raise ConnectorSchemaError(f"too many malformed NDJSON lines ({bad}/{total})")
        stats.bad_lines += bad
        return records, next_cursor
