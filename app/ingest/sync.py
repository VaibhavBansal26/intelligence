"""Incremental, idempotent sync of a source into the local landing tables."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update

from app.clock import Clock
from app.config import Settings
from app.connectors.client import (
    BudgetExceeded,
    ConnectorAuthError,
    ConnectorError,
    ConnectorSchemaError,
    SourceClient,
    StreamStats,
)
from app.connectors.records import (
    EXPORT_MODELS,
    FunnelRec,
    PointRec,
    StepRec,
    StructureFunnelRec,
    parse_ts,
)
from app.db import models as m
from app.db.session import SessionFactory, insert_ignore, session_scope, upsert
from app.security.crypto import SecretBox

logger = logging.getLogger(__name__)

HttpFactory = Callable[[m.Source], httpx.AsyncClient]

RESOURCES = {
    "funnels": ["funnel_structure", "funnels", "events", "leads", "rollups"],
    "conversations": ["rooms", "messages_meta", "rollups"],
}
EXPORT_PATH = {
    "events": "/export/events",
    "leads": "/export/leads",
    "rooms": "/export/rooms",
    "messages_meta": "/export/messages-meta",
}
EXPORT_TABLE = {"events": m.Event, "leads": m.Lead, "rooms": m.Room, "messages_meta": m.MessageMeta}
TS_FIELD = {"events": "ts", "leads": "created_at", "rooms": "created_at", "messages_meta": "ts"}
MUTABLE = {"leads", "rooms"}  # re-read a trailing window and upsert; others are append-only
FUNNEL_METRICS = ["visits", "unique_visits", "optins", "orders", "gross"]
CONV_METRICS = ["rooms_opened", "messages_in", "messages_out", "first_response_p50", "resolution_p50", "backlog"]
ADDITIVE = {"visits", "unique_visits", "optins", "orders", "gross"}


@dataclass
class ResourceResult:
    resource: str
    rows: int = 0
    bad_lines: int = 0
    pages: int = 0
    retries: int = 0
    resumed: bool = False
    error: str | None = None


@dataclass
class SyncResult:
    source_id: str
    kind: str
    resources: list[ResourceResult] = field(default_factory=list)
    schema_version: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "warnings": self.warnings,
            "resources": [r.__dict__ for r in self.resources],
        }


class IngestService:
    def __init__(
        self,
        sessions: SessionFactory,
        settings: Settings,
        secrets: SecretBox,
        http_factory: HttpFactory,
        clock: Clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._secrets = secrets
        self._http_factory = http_factory
        self._clock = clock
        self._sleep = sleep

    # ------------------------------------------------------------------------------------------------
    async def sync_source(self, account_id: str, source_id: str, *, full: bool = False) -> SyncResult:
        with session_scope(self._sessions) as s:
            src = s.get(m.Source, source_id)
            if src is None or src.account_id != account_id:
                raise ConnectorError("source not found")
            api_key = self._secrets.decrypt(src.api_key_enc)
            kind, base_url = src.kind, src.base_url
            s.expunge(src)
        result = SyncResult(source_id=source_id, kind=kind)
        http = self._http_factory(src)
        try:
            client = SourceClient(
                base_url=base_url,
                api_key=api_key,
                http=http,
                timeout_s=self._settings.connector_timeout_s,
                max_attempts=self._settings.connector_max_attempts,
                backoff_base_s=self._settings.connector_backoff_base_s,
                backoff_max_s=self._settings.connector_backoff_max_s,
                max_bad_line_ratio=self._settings.connector_max_bad_line_ratio,
                page_limit=self._settings.connector_page_limit,
                sleep=self._sleep,
                now=self._clock,
            )
            await self._check_schema(client, source_id, result)
            for resource in RESOURCES[kind]:
                if full:
                    self._reset_state(account_id, source_id, resource)
                rr = ResourceResult(resource)
                result.resources.append(rr)
                try:
                    await self._sync_resource(client, account_id, source_id, kind, resource, rr)
                    self._mark(account_id, source_id, resource, "ok", None)
                except ConnectorAuthError as exc:
                    rr.error = str(exc)
                    self._mark(account_id, source_id, resource, "error", rr.error)
                    break  # credentials are bad: no point trying other resources
                except (ConnectorError, httpx.HTTPError, ValidationError, ValueError) as exc:
                    rr.error = f"{type(exc).__name__}: {exc}"
                    self._mark(account_id, source_id, resource, "error", rr.error)
                    logger.warning(
                        "sync resource failed", extra={"source_id": source_id, "resource": resource, "error": rr.error}
                    )
        finally:
            await http.aclose()
        with session_scope(self._sessions) as s:
            s.execute(update(m.Account).where(m.Account.id == account_id).values(data_version=m.Account.data_version + 1))
        return result

    # ------------------------------------------------------------------------------------------------
    async def _check_schema(self, client: SourceClient, source_id: str, result: SyncResult) -> None:
        schema = await client.schema()
        version: int | None = None
        if schema is None:
            result.warnings.append("source has no /schema endpoint; assuming schema_version 1")
        else:
            raw = schema.get("schema_version")
            version = int(raw) if isinstance(raw, int | float | str) and str(raw).isdigit() else None
        if version is not None and version < self._settings.min_schema_version:
            raise ConnectorSchemaError(
                f"source schema_version {version} is older than supported {self._settings.min_schema_version}"
            )
        if version is not None and version > self._settings.max_schema_version:
            result.warnings.append(
                f"source schema_version {version} is newer than tested {self._settings.max_schema_version}; unknown fields ignored"
            )
        result.schema_version = version
        with session_scope(self._sessions) as s:
            s.execute(update(m.Source).where(m.Source.id == source_id).values(last_schema_version=version))

    def _state(self, s: Any, account_id: str, source_id: str, resource: str) -> m.SyncState:
        st = s.execute(
            select(m.SyncState).where(m.SyncState.source_id == source_id, m.SyncState.resource == resource)
        ).scalar_one_or_none()
        if st is None:
            st = m.SyncState(account_id=account_id, source_id=source_id, resource=resource, rows_total=0, bad_lines_total=0)
            s.add(st)
            s.flush()
        return st

    def _reset_state(self, account_id: str, source_id: str, resource: str) -> None:
        with session_scope(self._sessions) as s:
            st = self._state(s, account_id, source_id, resource)
            st.watermark = None
            st.cursor = None

    def _mark(self, account_id: str, source_id: str, resource: str, status: str, error: str | None) -> None:
        with session_scope(self._sessions) as s:
            st = self._state(s, account_id, source_id, resource)
            st.last_status, st.error, st.last_run_at = status, error, int(self._clock())

    def _from_ts(self, watermark: int | None, resource: str) -> int:
        now = int(self._clock())
        if watermark is None:
            return now - self._settings.backfill_days * 86400
        back = self._settings.lookback_days * 86400 if resource in MUTABLE else 0
        return max(now - 365 * 86400, watermark - back)

    # ------------------------------------------------------------------------------------------------
    async def _sync_resource(
        self, client: SourceClient, account_id: str, source_id: str, kind: str, resource: str, rr: ResourceResult
    ) -> None:
        if resource in EXPORT_PATH:
            await self._sync_export(client, account_id, source_id, resource, rr)
        elif resource == "funnels":
            await self._sync_funnels(client, account_id, source_id, rr)
        elif resource == "funnel_structure":
            await self._sync_structure(client, account_id, source_id, rr)
        elif resource == "rollups":
            await self._sync_rollups(client, account_id, source_id, kind, rr)

    async def _sync_export(
        self, client: SourceClient, account_id: str, source_id: str, resource: str, rr: ResourceResult
    ) -> None:
        model = EXPORT_MODELS[resource]
        table = EXPORT_TABLE[resource]
        ts_field = TS_FIELD[resource]
        with session_scope(self._sessions) as s:
            st = self._state(s, account_id, source_id, resource)
            watermark, saved_cursor = st.watermark, st.cursor
        cursor: str | None = None
        if saved_cursor:
            saved = json.loads(saved_cursor)
            cursor, from_ts = saved["c"], int(saved["from"])
            rr.resumed = True
        else:
            from_ts = self._from_ts(watermark, resource)
        params = {"from": _iso(from_ts)}
        buffer: list[dict[str, Any]] = []
        max_ts = watermark or 0
        stats = StreamStats()
        bad = 0
        chunk_rows = self._settings.ingest_chunk_rows

        async def flush(next_cursor: str | None) -> None:
            nonlocal buffer
            rows, buffer = buffer, []
            await asyncio.to_thread(
                self._write_rows, resource, table, rows, account_id, source_id, max_ts, next_cursor, from_ts, watermark
            )

        async def on_page(next_cursor: str | None) -> None:
            if next_cursor is None or len(buffer) >= chunk_rows:
                await flush(next_cursor)

        async for raw in client.stream_ndjson(EXPORT_PATH[resource], params, cursor=cursor, on_page=on_page, stats=stats):
            try:
                rec = model.model_validate(raw)
            except ValidationError:
                bad += 1
                continue
            row = rec.to_row(account_id, source_id)  # type: ignore[attr-defined]
            buffer.append(row)
            ts = row[ts_field]
            if ts > max_ts:
                max_ts = ts
        if buffer:  # defensive: the final on_page flush normally empties it
            await flush(None)
        rr.rows, rr.pages, rr.retries, rr.bad_lines = stats.records - bad, stats.pages, stats.retries, stats.bad_lines + bad
        with session_scope(self._sessions) as s:
            st = self._state(s, account_id, source_id, resource)
            st.bad_lines_total += rr.bad_lines
            st.rows_total += rr.rows

    def _write_rows(
        self,
        resource: str,
        table: Any,
        rows: list[dict[str, Any]],
        account_id: str,
        source_id: str,
        max_ts: int,
        next_cursor: str | None,
        from_ts: int,
        prev_watermark: int | None = None,
    ) -> None:
        with session_scope(self._sessions) as s:
            writer = upsert if resource in MUTABLE else insert_ignore
            for i in range(0, len(rows), 2000):
                writer(s, table.__table__, rows[i : i + 2000])
            st = self._state(s, account_id, source_id, resource)
            if max_ts:
                st.watermark = max(st.watermark or 0, max_ts) if next_cursor is None else st.watermark
            st.cursor = json.dumps({"c": next_cursor, "from": from_ts}) if next_cursor else None
            cap = self._settings.account_max_event_rows
            if resource == "events" and cap:
                total = int(
                    s.execute(select(func.count()).select_from(m.Event).where(m.Event.account_id == account_id)).scalar() or 0
                )
                if total + len(rows) > cap:
                    s.commit()
                    raise BudgetExceeded(
                        f"account event budget reached ({cap} rows); raise ACCOUNT_MAX_EVENT_ROWS or archive data"
                    )
        if resource == "events" and self._settings.columnar_dir and rows:
            self._tee_columnar(account_id, rows, prev_watermark)

    def _tee_columnar(self, account_id: str, rows: list[dict[str, Any]], prev_watermark: int | None) -> None:
        """Append newly seen events to the Parquet store. Only rows after the previous watermark are teed, so a
        re-read of the same window is not double counted (boundary-second ties are covered by the SQL tables)."""
        import polars as pl

        from app.columnar.store import ParquetEventStore, contact_hash_expr

        fresh = [r for r in rows if prev_watermark is None or r["ts"] > prev_watermark]
        if not fresh:
            return
        df = pl.DataFrame(
            {
                "ts": [r["ts"] for r in fresh],
                "type": [r["type"] for r in fresh],
                "funnel_id": [r["funnel_id"] for r in fresh],
                "step_id": [r["step_id"] for r in fresh],
                "contact_key": [r["contact_key"] for r in fresh],
                "amount": [r["amount"] for r in fresh],
            },
            schema={
                "ts": pl.Int64,
                "type": pl.String,
                "funnel_id": pl.String,
                "step_id": pl.String,
                "contact_key": pl.String,
                "amount": pl.Float32,
            },
        ).with_columns(contact_hash_expr().alias("contact_h"))
        ParquetEventStore(self._settings.columnar_dir, self._settings.columnar_buckets).append(account_id, df)

    async def _sync_funnels(self, client: SourceClient, account_id: str, source_id: str, rr: ResourceResult) -> None:
        now = int(self._clock())
        raw_funnels = await client.list_all("/funnels")
        funnels: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        for raw in raw_funnels:
            try:
                f = FunnelRec.model_validate(raw)
            except ValidationError:
                rr.bad_lines += 1
                continue
            funnels.append(
                {
                    "source_id": source_id,
                    "funnel_id": f.id,
                    "account_id": account_id,
                    "name": f.name,
                    "status": f.status,
                    "steps_count": f.steps_count,
                    "visits": f.visits,
                    "unique_visits": f.unique_visits,
                    "optins": f.optins,
                    "orders": f.orders,
                    "gross": f.gross,
                    "currency": f.currency,
                    "synced_at": now,
                }
            )
            for rs in await client.list_all(f"/funnels/{f.id}/steps"):
                try:
                    st = StepRec.model_validate(rs)
                except ValidationError:
                    rr.bad_lines += 1
                    continue
                steps.append(
                    {
                        "source_id": source_id,
                        "funnel_id": f.id,
                        "step_id": st.step_id,
                        "account_id": account_id,
                        "name": st.name,
                        "ord": st.order,
                        "visits": st.visits,
                        "unique_visits": st.unique_visits,
                        "optins": st.optins,
                        "orders": st.orders,
                        "gross": st.gross,
                        "exit_rate": st.exit_rate,
                        "avg_time_on_page_s": st.avg_time_on_page_s,
                        "synced_at": now,
                    }
                )
        with session_scope(self._sessions) as s:
            s.execute(delete(m.FunnelStep).where(m.FunnelStep.source_id == source_id))
            s.execute(delete(m.Funnel).where(m.Funnel.source_id == source_id))
            insert_ignore(s, m.Funnel.__table__, funnels)
            insert_ignore(s, m.FunnelStep.__table__, steps)
        rr.rows = len(funnels) + len(steps)

    async def _sync_structure(self, client: SourceClient, account_id: str, source_id: str, rr: ResourceResult) -> None:
        rows: list[dict[str, Any]] = []
        for raw in await client.get_records("/export/funnel-structure"):
            raw = dict(raw)
            raw.setdefault("funnel_id", raw.get("id"))
            if isinstance(raw.get("steps"), list):
                raw["steps"] = [{**x, "step_id": x.get("step_id", x.get("id"))} for x in raw["steps"] if isinstance(x, dict)]
            try:
                f = StructureFunnelRec.model_validate(raw)
            except ValidationError:
                rr.bad_lines += 1
                continue
            for st in f.steps:
                b = st.blocks
                rows.append(
                    {
                        "source_id": source_id,
                        "funnel_id": f.funnel_id,
                        "step_id": st.step_id,
                        "account_id": account_id,
                        "funnel_name": f.name,
                        "step_name": st.name,
                        "ord": st.order,
                        "step_type": st.type,
                        "n_form": b.get("form", 0),
                        "n_checkout": b.get("checkout", 0),
                        "n_video": b.get("video", 0),
                        "n_countdown": b.get("countdown", 0),
                        "word_count": st.word_count,
                        "has_order_bump": st.has_order_bump,
                        "has_upsell": st.has_upsell,
                    }
                )
        with session_scope(self._sessions) as s:
            s.execute(delete(m.FunnelStructure).where(m.FunnelStructure.source_id == source_id))
            insert_ignore(s, m.FunnelStructure.__table__, rows)
        rr.rows = len(rows)

    async def _sync_rollups(self, client: SourceClient, account_id: str, source_id: str, kind: str, rr: ResourceResult) -> None:
        with session_scope(self._sessions) as s:
            st = self._state(s, account_id, source_id, "rollups")
            watermark = st.watermark
            funnel_ids = list(s.execute(select(m.Funnel.funnel_id).where(m.Funnel.source_id == source_id)).scalars())
        from_ts = self._from_ts(watermark, "leads")  # rollups are re-read over the lookback window
        params_base = {"interval": "day", "from": _iso(from_ts)}
        out: list[dict[str, Any]] = []
        totals: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        scopes = (
            [(f"/funnels/{fid}/timeseries", fid, FUNNEL_METRICS) for fid in funnel_ids]
            if kind == "funnels"
            else [("/timeseries", "all", CONV_METRICS)]
        )
        max_t = watermark or 0
        for path, scope, metrics in scopes:
            for metric in metrics:
                payload = await client.get_json(path, {**params_base, "metric": metric})
                points = payload.get("points", []) if isinstance(payload, dict) else []
                for p in points:
                    try:
                        pt = PointRec.model_validate(p)
                    except ValidationError:
                        rr.bad_lines += 1
                        continue
                    out.append(
                        {
                            "source_id": source_id,
                            "metric": metric,
                            "scope": scope,
                            "interval": "day",
                            "t": pt.t,
                            "account_id": account_id,
                            "value": pt.value,
                        }
                    )
                    max_t = max(max_t, pt.t)
                    if kind == "funnels" and metric in ADDITIVE:
                        totals[metric][pt.t] += pt.value
        for metric, series in totals.items():
            out.extend(
                {
                    "source_id": source_id,
                    "metric": metric,
                    "scope": "all",
                    "interval": "day",
                    "t": t,
                    "account_id": account_id,
                    "value": v,
                }
                for t, v in series.items()
            )
        with session_scope(self._sessions) as s:
            for i in range(0, len(out), 5000):
                upsert(s, m.Rollup.__table__, out[i : i + 5000])
            st = self._state(s, account_id, source_id, "rollups")
            st.watermark = max_t or st.watermark
            st.rows_total += len(out)
        rr.rows = len(out)


def _iso(ts: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["IngestService", "ResourceResult", "SyncResult", "parse_ts"]
