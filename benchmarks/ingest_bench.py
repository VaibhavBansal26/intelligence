"""Streams N synthetic events (NDJSON, paged) through the real ingest path and reports time and peak RSS.

    uv run python -m benchmarks.ingest_bench --rows 2000000

The source is a lazily generating ASGI app, so the benchmark itself holds no dataset in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import platform
import resource
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from sqlalchemy import func, select

from app.config import Settings
from app.connectors.client import SourceClient
from app.db import models as m
from app.db.session import create_all, make_engine, make_session_factory, session_scope
from app.ingest.sync import IngestService, ResourceResult
from app.security.crypto import SecretBox

BASE_TS = 1_790_000_000
TYPES = ["page_view", "page_view", "page_view", "optin", "order"]


def make_source(total: int) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/analytics/export/events")
    def events(request: Request, cursor: str | None = None, limit: int = 1000) -> Response:
        start = int(base64.urlsafe_b64decode(cursor.encode() + b"==")) if cursor else 0
        end = min(start + limit, total)
        lines = []
        for i in range(start, end):
            t = TYPES[i % 5]
            lines.append(
                json.dumps(
                    {
                        "id": f"evt_{i:09d}",
                        "ts": BASE_TS + i * 3,
                        "type": t,
                        "funnel_id": f"f{i % 7}",
                        "step_id": f"s{i % 5}",
                        "visitor_key": f"v{i // 3}",
                        "contact_key": f"c{i // 6}" if i % 2 else None,
                        "utm_source": "email" if i % 4 else "google",
                        "device": "mobile",
                        "amount": 49.0 if t == "order" else None,
                        "unique_visit": i % 3 == 0,
                        "extra_future_field": i,
                    }
                )
            )
        headers = {}
        if end < total:
            headers["X-Next-Cursor"] = base64.urlsafe_b64encode(str(end).encode()).decode().rstrip("=")
        return Response("\n".join(lines) + "\n", media_type="application/x-ndjson", headers=headers)

    return app


def rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if platform.system() == "Darwin" else peak / 1024


async def run(rows: int) -> dict[str, object]:
    tmp = Path(tempfile.mkdtemp())
    st = Settings(
        env="test",
        database_url=f"sqlite:///{tmp}/bench.db",
        model_dir=str(tmp / "m"),
        connector_page_limit=1000,
        ingest_chunk_rows=5000,
    )
    eng = make_engine(st.database_url)
    create_all(eng)
    sessions = make_session_factory(eng)
    secrets = SecretBox(st.kek_list)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_source(rows)), base_url="http://bench.test")
    svc = IngestService(sessions, st, secrets, lambda _s: http, lambda: float(BASE_TS + rows * 3 + 10))
    with session_scope(sessions) as s:
        s.add(m.Account(id="a", name="bench", created_at=0, data_version=1, settings={}))
        s.add(
            m.Source(
                id="s",
                account_id="a",
                kind="funnels",
                name="bench",
                base_url="http://bench.test",
                api_key_enc="x",
                pepper_enc="x",
                pepper_fp="x",
                status="active",
                created_at=0,
                updated_at=0,
            )
        )
    client = SourceClient(base_url="http://bench.test", api_key="k", http=http, page_limit=1000)
    rr = ResourceResult("events")
    rss_before = rss_mb()
    t0 = time.monotonic()
    await svc._sync_export(client, "a", "s", "events", rr)
    elapsed = time.monotonic() - t0
    with session_scope(sessions) as s:
        landed = int(s.execute(select(func.count()).select_from(m.Event)).scalar() or 0)
    return {
        "rows": rows,
        "landed": landed,
        "seconds": round(elapsed, 1),
        "rows_per_second": int(rows / elapsed),
        "pages": rr.pages,
        "peak_rss_mb": round(rss_mb(), 1),
        "rss_before_mb": round(rss_before, 1),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "os": platform.platform(),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200_000)
    print(json.dumps(asyncio.run(run(ap.parse_args().rows))))
