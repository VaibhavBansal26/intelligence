"""FAKE implementations of the Analytics API contract (Funnels and Conversations) backed by a synthetic World.

Used by the end-to-end tests and by the demo (`python -m app.synthetic.fake_sources`). Supports fault
injection (429, 503, truncated stream, malformed line) via `app.state.faults`.
"""

from __future__ import annotations

import base64
import bisect
import json
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.synthetic.world import FUNNEL_DEFS, World, iso

PREFIX = "/api/v1/analytics"
SCHEMA_VERSION = 1


def _err(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def _parse(v: str | None) -> str | None:
    if not v:
        return None
    t = v.strip()
    if len(t) == 10:
        t += "T00:00:00Z"
    return iso(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())


def _epoch_series(s: pd.Series) -> list[int]:
    return [int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()) for v in s]


def _epoch(v: str | None, default: int) -> int:
    p = _parse(v)
    return default if p is None else int(datetime.fromisoformat(p.replace("Z", "+00:00")).timestamp())


class _Export:
    def __init__(self, rows: list[dict[str, Any]], ts_field: str) -> None:
        self.rows = sorted(rows, key=lambda r: (r[ts_field], r["id"]))
        self.keys = [(r[ts_field], r["id"]) for r in self.rows]
        self.ts_field = ts_field

    def page(self, frm: str | None, to: str | None, cursor: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        lo = 0
        if frm:
            lo = bisect.bisect_left(self.keys, (frm, ""))
        hi = len(self.keys)
        if to:
            hi = bisect.bisect_right(self.keys, (to, "￿"))
        if cursor:
            last = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"=="))
            lo = max(lo, bisect.bisect_right(self.keys, (last[0], last[1])))
        end = min(lo + limit, hi)
        out = self.rows[lo:end]
        nxt = None
        if end < hi and out:
            nxt = base64.urlsafe_b64encode(json.dumps([out[-1][self.ts_field], out[-1]["id"]]).encode()).decode().rstrip("=")
        return out, nxt


def _ndjson(rows: list[dict[str, Any]], nxt: str | None) -> Response:
    body = "".join(json.dumps(r) + "\n" for r in rows)
    headers = {"X-Next-Cursor": nxt} if nxt else {}
    return Response(body, media_type="application/x-ndjson", headers=headers)


def _apply_fault(app: FastAPI, path: str) -> str | None:
    for key, queue in app.state.faults.items():
        if key in path and queue:
            return str(queue.pop(0))
    return None


def _mangle(resp: Response, fault: str) -> Response:
    body = bytes(resp.body).decode()
    lines = body.splitlines()
    if fault == "truncate" and lines:
        body = "\n".join(lines[:-1]) + "\n" + lines[-1][: max(1, len(lines[-1]) // 2)]
    elif fault == "bad_line" and len(lines) > 2:
        lines.insert(1, "{not json")
        body = "\n".join(lines) + "\n"
    return Response(
        body, media_type="application/x-ndjson", headers={k: v for k, v in resp.headers.items() if k.lower() == "x-next-cursor"}
    )


def _make_app(title: str, api_key: str, pepper: str, routes: Callable[[FastAPI], None]) -> FastAPI:
    app = FastAPI(title=title)
    app.state.faults = defaultdict(list)
    app.state.api_key = api_key
    app.state.pepper = pepper
    app.state.request_log = []

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[..., Any]) -> Response:
        app.state.request_log.append(request.url.path)
        if request.headers.get("authorization") != f"Bearer {api_key}":
            return _err(401, "UNAUTHORIZED", "invalid api key")
        fault = _apply_fault(app, request.url.path)
        if fault == "429":
            return _err(429, "RATE_LIMITED", "slow down", {"Retry-After": "1"})
        if fault == "503":
            return _err(503, "UNAVAILABLE", "try later")
        resp: Response = await call_next(request)
        if fault in ("truncate", "bad_line") and resp.status_code == 200:
            body = b"".join([chunk async for chunk in resp.body_iterator])  # type: ignore[attr-defined]
            return _mangle(Response(body, headers=dict(resp.headers)), fault)
        return resp

    @app.get(f"{PREFIX}/schema")
    def schema() -> dict[str, Any]:
        return {"schema_version": app.state.schema_version, "schemas": {}}

    @app.get(f"{PREFIX}/identity-pepper")
    def identity_pepper() -> dict[str, str]:
        return {"pepper": pepper}

    app.state.schema_version = SCHEMA_VERSION
    routes(app)
    return app


def _bucket(ts: float, interval: str) -> int:
    dt = datetime.fromtimestamp(ts, UTC)
    if interval == "hour":
        dt = dt.replace(minute=0, second=0, microsecond=0)
    elif interval == "week":
        dt = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == "month":
        dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


def _step(interval: str) -> int:
    return {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}.get(interval, 86400)


def _points(series: dict[int, float], frm: int, to: int, interval: str) -> list[dict[str, Any]]:
    out = []
    t = _bucket(frm, interval)
    while t <= to:
        out.append({"t": iso(t), "value": float(series.get(t, 0.0))})
        t += _step(interval)
    return out


def create_fake_funnels_app(world: World, api_key: str = "fa_test_key", pepper: str | None = None) -> FastAPI:
    ev = _Export(world.events, "ts")
    leads = _Export(world.leads, "created_at")
    ev_df = pd.DataFrame(world.events)

    def routes(app: FastAPI) -> None:
        @app.get(f"{PREFIX}/funnels")
        def funnels(limit: int = 1000, cursor: str | None = None) -> dict[str, Any]:
            return {"data": world.funnels_table(), "next_cursor": None}

        @app.get(f"{PREFIX}/funnels/{{funnel_id}}/steps")
        def steps(funnel_id: str) -> Response:
            rows = world.steps_table(funnel_id)
            if not rows:
                return _err(404, "NOT_FOUND", "funnel not found")
            return JSONResponse({"data": rows, "next_cursor": None})

        @app.get(f"{PREFIX}/funnels/{{funnel_id}}/timeseries")
        def timeseries(request: Request, funnel_id: str, metric: str = "visits", interval: str = "day") -> Response:
            frm, to = request.query_params.get("from"), request.query_params.get("to")
            if ev_df.empty or funnel_id not in {f["id"] for f in FUNNEL_DEFS}:
                return _err(404, "NOT_FOUND", "funnel not found")
            e = ev_df[ev_df.funnel_id == funnel_id].copy()
            first = next(f for f in FUNNEL_DEFS if f["id"] == funnel_id)["steps"][0][0]
            if metric == "visits":
                e = e[(e.type == "page_view") & (e.step_id == first)]
            elif metric == "unique_visits":
                e = e[(e.type == "page_view") & (e.step_id == first) & e.unique_visit]
            elif metric == "optins":
                e = e[e.type == "optin"]
            elif metric in ("orders", "gross"):
                e = e[e.type == "order"]
            else:
                return _err(400, "BAD_REQUEST", "unknown metric")
            tsv = _epoch_series(e.ts) if len(e) else []
            buckets = [_bucket(int(t), interval) for t in tsv]
            series: dict[int, float] = defaultdict(float)
            vals = e.amount.fillna(0).tolist() if metric == "gross" else [1.0] * len(e)
            for b, v in zip(buckets, vals, strict=True):
                series[b] += v
            lo = _epoch(frm, world.start_ts)
            hi = _epoch(to, world.end_ts)
            return JSONResponse({"interval": interval, "points": _points(series, lo, hi, interval)})

        @app.get(f"{PREFIX}/export/events")
        def export_events(request: Request, cursor: str | None = None, limit: int = 1000) -> Response:
            q = request.query_params
            rows, nxt = ev.page(_parse(q.get("from")), _parse(q.get("to")), cursor, min(limit, 5000))
            return _ndjson(rows, nxt)

        @app.get(f"{PREFIX}/export/leads")
        def export_leads(request: Request, cursor: str | None = None, limit: int = 1000) -> Response:
            q = request.query_params
            rows, nxt = leads.page(_parse(q.get("from")), _parse(q.get("to")), cursor, min(limit, 5000))
            return _ndjson(rows, nxt)

        @app.get(f"{PREFIX}/export/funnel-structure")
        def structure() -> Response:
            grouped: dict[str, dict[str, Any]] = {}
            for s in world.structure:
                g = grouped.setdefault(s["funnel_id"], {"funnel_id": s["funnel_id"], "name": s["funnel_name"], "steps": []})
                g["steps"].append({k: v for k, v in s.items() if k not in ("funnel_id", "funnel_name")})
            return Response("".join(json.dumps(v) + "\n" for v in grouped.values()), media_type="application/x-ndjson")

    return _make_app("fake-funnels", api_key, pepper or world.config.pepper, routes)


def create_fake_conversations_app(world: World, api_key: str = "ca_test_key", pepper: str | None = None) -> FastAPI:
    rooms = _Export(world.rooms, "created_at")
    msgs = _Export(world.messages_meta, "ts")
    room_df = pd.DataFrame(world.rooms)

    def routes(app: FastAPI) -> None:
        @app.get(f"{PREFIX}/overview")
        def overview() -> dict[str, Any]:
            return {"rooms_opened": len(world.rooms), "rooms_closed": int(room_df.closed_at.notna().sum()) if len(room_df) else 0}

        @app.get(f"{PREFIX}/timeseries")
        def timeseries(request: Request, metric: str = "rooms_opened", interval: str = "day") -> Response:
            frm, to = request.query_params.get("from"), request.query_params.get("to")
            if room_df.empty:
                return JSONResponse({"interval": interval, "points": []})
            lo = _epoch(frm, world.start_ts)
            hi = _epoch(to, world.end_ts)
            created = _epoch_series(room_df.created_at)
            closed: list[float] = [
                float("inf") if pd.isna(c) else float(c)
                for c in pd.to_datetime(room_df.closed_at, utc=True).map(lambda x: x.timestamp() if pd.notna(x) else None)
            ]
            series: dict[int, float] = defaultdict(float)
            if metric in ("rooms_opened", "messages_in", "messages_out"):
                col = {"rooms_opened": None, "messages_in": "messages_in", "messages_out": "messages_out"}[metric]
                for i, c in enumerate(created):
                    series[_bucket(int(c), interval)] += 1.0 if col is None else float(room_df[col].iat[i])
            elif metric == "backlog":
                t = _bucket(lo, interval)
                while t <= hi:
                    end = t + _step(interval)
                    series[t] = float(sum(1 for i, c in enumerate(created) if c < end and closed[i] >= end))
                    t = end
            elif metric in ("first_response_p50", "resolution_p50"):
                col = "first_response_s" if metric.startswith("first") else "resolution_s"
                groups: dict[int, list[float]] = defaultdict(list)
                for i, c in enumerate(created):
                    v = room_df[col].iat[i]
                    if pd.notna(v):
                        groups[_bucket(int(c), interval)].append(float(v))
                series = defaultdict(float, {k: float(pd.Series(v).median()) for k, v in groups.items()})
            else:
                return _err(400, "BAD_REQUEST", "unknown metric")
            return JSONResponse({"interval": interval, "points": _points(series, lo, hi, interval)})

        @app.get(f"{PREFIX}/export/rooms")
        def export_rooms(request: Request, cursor: str | None = None, limit: int = 1000) -> Response:
            q = request.query_params
            rows, nxt = rooms.page(_parse(q.get("from")), _parse(q.get("to")), cursor, min(limit, 5000))
            return _ndjson(rows, nxt)

        @app.get(f"{PREFIX}/export/messages-meta")
        def export_msgs(request: Request, cursor: str | None = None, limit: int = 1000) -> Response:
            q = request.query_params
            rows, nxt = msgs.page(_parse(q.get("from")), _parse(q.get("to")), cursor, min(limit, 5000))
            return _ndjson(rows, nxt)

    return _make_app("fake-conversations", api_key, pepper or world.config.conversations_pepper or world.config.pepper, routes)


if __name__ == "__main__":  # pragma: no cover - demo helper
    import argparse
    import threading

    import uvicorn

    from app.synthetic.world import generate_world

    parser = argparse.ArgumentParser(description="Run fake Funnels (port) and Conversations (port+1) contract servers")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--days", type=int, default=120)
    args = parser.parse_args()
    w = generate_world(__import__("app.synthetic.world", fromlist=["WorldConfig"]).WorldConfig(n_days=args.days))
    f_app, c_app = create_fake_funnels_app(w), create_fake_conversations_app(w)
    t = threading.Thread(target=lambda: uvicorn.run(c_app, port=args.port + 1, log_level="warning"), daemon=True)
    t.start()
    print(
        f"funnels on :{args.port} key=fa_test_key | conversations on :{args.port + 1} key=ca_test_key | pepper={w.config.pepper}"
    )
    uvicorn.run(f_app, port=args.port, log_level="warning")
