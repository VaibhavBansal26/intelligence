from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.connectors.client import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorSchemaError,
    ConnectorUnavailable,
    SourceClient,
    StreamStats,
    unwrap_list,
)

BASE = "https://funnels.example.com"
EXP = BASE + "/api/v1/analytics/export/events"


def nd(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def make(http: httpx.AsyncClient, sleeps: list[float] | None = None, **kw) -> SourceClient:  # type: ignore[no-untyped-def]
    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    return SourceClient(base_url=BASE, api_key="k", http=http, sleep=sleep, rand=lambda: 1.0, **kw)


async def collect(client: SourceClient, **kw):  # type: ignore[no-untyped-def]
    return [r async for r in client.stream_ndjson("/export/events", {"from": "2026-01-01"}, **kw)]


@respx.mock
async def test_ndjson_pagination_follows_x_next_cursor_and_reports_pages() -> None:
    route = respx.get(EXP).mock(
        side_effect=[
            httpx.Response(200, text=nd([{"id": "1"}, {"id": "2"}]), headers={"X-Next-Cursor": "c1"}),
            httpx.Response(200, text=nd([{"id": "3"}])),
        ]
    )
    seen: list[str | None] = []

    async def on_page(c: str | None) -> None:
        seen.append(c)

    async with httpx.AsyncClient() as http:
        rows = await collect(make(http), on_page=on_page)
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert seen == ["c1", None]
    assert route.calls[1].request.url.params["cursor"] == "c1"
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
async def test_resume_from_cursor() -> None:
    route = respx.get(EXP).mock(return_value=httpx.Response(200, text=nd([{"id": "9"}])))
    async with httpx.AsyncClient() as http:
        rows = await collect(make(http), cursor="saved")
    assert rows == [{"id": "9"}] and route.calls[0].request.url.params["cursor"] == "saved"


@respx.mock
async def test_429_honours_retry_after_then_succeeds() -> None:
    respx.get(EXP).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, text=nd([{"id": "1"}]))]
    )
    sleeps: list[float] = []
    stats = StreamStats()
    async with httpx.AsyncClient() as http:
        rows = await collect(make(http, sleeps), stats=stats)
    assert rows == [{"id": "1"}] and sleeps == [3.0] and stats.retries == 1


@respx.mock
async def test_429_with_huge_retry_after_surfaces_for_rescheduling() -> None:
    respx.get(EXP).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorRateLimited) as e:
            await collect(make(http, []))
    assert e.value.retry_after_s == 3600


@respx.mock
async def test_5xx_retried_with_jittered_backoff_then_gives_up() -> None:
    route = respx.get(EXP).mock(return_value=httpx.Response(503))
    sleeps: list[float] = []
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorUnavailable):
            await collect(make(http, sleeps, max_attempts=4, backoff_base_s=1.0, backoff_max_s=100))
    assert route.call_count == 4 and sleeps == [1.0, 2.0, 4.0]  # rand()==1 -> full exponential ceiling


@respx.mock
async def test_timeout_and_transport_errors_are_retried() -> None:
    respx.get(EXP).mock(
        side_effect=[httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.Response(200, text=nd([{"id": "1"}]))]
    )
    async with httpx.AsyncClient() as http:
        assert await collect(make(http, [])) == [{"id": "1"}]


@respx.mock
@pytest.mark.parametrize("code", [401, 403])
async def test_auth_errors_are_not_retried(code: int) -> None:
    route = respx.get(EXP).mock(return_value=httpx.Response(code, json={"error": {"code": "X", "message": "no"}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorAuthError):
            await collect(make(http, []))
    assert route.call_count == 1


@respx.mock
async def test_malformed_middle_lines_are_skipped_and_counted() -> None:
    body = nd([{"id": "1"}]) + "{broken\n" + nd([{"id": "2"}]) + "[1,2]\n" + nd([{"id": "3"}]) * 1
    respx.get(EXP).mock(return_value=httpx.Response(200, text=body))
    stats = StreamStats()
    async with httpx.AsyncClient() as http:
        rows = await collect(make(http, max_bad_line_ratio=0.9), stats=stats)
    assert [r["id"] for r in rows] == ["1", "2", "3"] and stats.bad_lines == 2


@respx.mock
async def test_mostly_garbage_is_a_schema_error() -> None:
    respx.get(EXP).mock(return_value=httpx.Response(200, text="nope\n" * 20 + nd([{"id": "1"}])))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorSchemaError):
            await collect(make(http, max_bad_line_ratio=0.05))


@respx.mock
async def test_truncated_stream_retries_page_without_duplicates() -> None:
    good = nd([{"id": "1"}, {"id": "2"}])
    respx.get(EXP).mock(
        side_effect=[
            httpx.Response(200, text=good + '{"id": "3", "ts": "2026'),
            httpx.Response(200, text=good + nd([{"id": "3"}])),
        ]
    )
    sleeps: list[float] = []
    async with httpx.AsyncClient() as http:
        rows = await collect(make(http, sleeps))
    assert [r["id"] for r in rows] == ["1", "2", "3"] and len(sleeps) == 1


@respx.mock
async def test_stalled_cursor_is_detected() -> None:
    respx.get(EXP).mock(return_value=httpx.Response(200, text=nd([{"id": "1"}]), headers={"X-Next-Cursor": "same"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorSchemaError):
            await collect(make(http, []))


@respx.mock
async def test_unknown_fields_survive_and_envelope_variants_parse() -> None:
    respx.get(BASE + "/api/v1/analytics/funnels").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"id": "f1", "brand_new_field": 1}], "next_cursor": "n"}),
            httpx.Response(200, json=[{"id": "f2"}]),
        ]
    )
    async with httpx.AsyncClient() as http:
        out = await make(http).list_all("/funnels")
    assert [o["id"] for o in out] == ["f1", "f2"]


def test_unwrap_rejects_garbage() -> None:
    with pytest.raises(ConnectorSchemaError):
        unwrap_list({"weird": 1})


@respx.mock
async def test_get_records_accepts_ndjson_and_json() -> None:
    url = BASE + "/api/v1/analytics/export/funnel-structure"
    respx.get(url).mock(return_value=httpx.Response(200, text=nd([{"funnel_id": "a"}, {"funnel_id": "b"}])))
    async with httpx.AsyncClient() as http:
        assert len(await make(http).get_records("/export/funnel-structure")) == 2
    respx.get(url).mock(return_value=httpx.Response(200, json={"data": [{"funnel_id": "a"}]}))
    async with httpx.AsyncClient() as http:
        assert len(await make(http).get_records("/export/funnel-structure")) == 1


# --- rate-limit pacing and export page size (found by running against the real apps) -----------------------------
def test_parse_rate_limit_reads_ietf_and_legacy_headers() -> None:
    from app.connectors.client import parse_rate_limit

    ietf = httpx.Headers({"RateLimit": '"analytics.export";r=8;t=12', "RateLimit-Policy": '"analytics.export";q=10;w=60'})
    assert parse_rate_limit(ietf) == (8, 6.0)
    assert parse_rate_limit(httpx.Headers({"X-RateLimit-Remaining": "3"})) == (3, None)
    assert parse_rate_limit(httpx.Headers({})) == (None, None)


@respx.mock
async def test_zero_remaining_paces_the_next_export_request_instead_of_provoking_a_429() -> None:
    hdr = {"RateLimit": '"analytics.export";r=0;t=55', "RateLimit-Policy": '"analytics.export";q=10;w=60'}
    route = respx.get(EXP).mock(
        side_effect=[
            httpx.Response(200, text=nd([{"id": "1"}]), headers={**hdr, "X-Next-Cursor": "c1"}),
            httpx.Response(200, text=nd([{"id": "2"}])),
        ]
    )
    sleeps: list[float] = []
    async with httpx.AsyncClient() as http:
        rows = await collect(make(http, sleeps))
    assert [r["id"] for r in rows] == ["1", "2"] and route.call_count == 2
    assert sleeps == [6.25]  # one refill interval of the policy (60 s / 10), not the 55 s until a full reset


@respx.mock
async def test_reads_and_exports_are_paced_independently() -> None:
    respx.get(BASE + "/api/v1/analytics/funnels").mock(
        return_value=httpx.Response(
            200,
            json={"data": []},
            headers={"RateLimit": '"analytics.read";r=0;t=1', "RateLimit-Policy": '"analytics.read";q=60;w=60'},
        )
    )
    respx.get(EXP).mock(return_value=httpx.Response(200, text=nd([{"id": "1"}])))
    sleeps: list[float] = []
    async with httpx.AsyncClient() as http:
        c = make(http, sleeps)
        await c.get_json("/funnels")
        await collect(c)  # export bucket untouched by the read bucket
    assert sleeps == []


@respx.mock
async def test_429_wait_clears_pacing_so_the_retry_is_not_delayed_twice() -> None:
    respx.get(EXP).mock(
        side_effect=[
            httpx.Response(
                429,
                headers={
                    "Retry-After": "7",
                    "RateLimit": '"analytics.export";r=0;t=7',
                    "RateLimit-Policy": '"analytics.export";q=10;w=60',
                },
            ),
            httpx.Response(200, text=nd([{"id": "1"}])),
        ]
    )
    sleeps: list[float] = []
    async with httpx.AsyncClient() as http:
        await collect(make(http, sleeps))
    assert sleeps == [7.0]


@respx.mock
async def test_exports_ask_for_large_pages_and_fall_back_to_the_contract_limit_on_400() -> None:
    route = respx.get(EXP).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"code": "VALIDATION_ERROR", "message": "limit must be <= 1000"}}),
            httpx.Response(200, text=nd([{"id": "1"}]), headers={"X-Next-Cursor": "c1"}),
            httpx.Response(200, text=nd([{"id": "2"}])),
        ]
    )
    async with httpx.AsyncClient() as http:
        rows = await collect(make(http, [], export_page_limit=5000))
    assert [r["id"] for r in rows] == ["1", "2"]
    assert [c.request.url.params["limit"] for c in route.calls] == ["5000", "1000", "1000"]


@respx.mock
async def test_400_at_the_contract_limit_is_a_hard_error() -> None:
    from app.connectors.client import ConnectorBadRequest

    respx.get(EXP).mock(return_value=httpx.Response(400, json={"error": {"code": "VALIDATION_ERROR", "message": "bad from"}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorBadRequest) as e:
            await collect(make(http, [], export_page_limit=1000))
    assert e.value.code == "VALIDATION_ERROR"
