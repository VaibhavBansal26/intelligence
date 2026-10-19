"""DB-backed job queue and an asyncio runner. A Celery/Redis implementation can replace `JobQueue`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sqlalchemy import func, select, update

from app.clock import Clock
from app.connectors.client import ConnectorError
from app.db import models as m
from app.db.session import SessionFactory, session_scope
from app.security.crypto import new_id

logger = logging.getLogger(__name__)

Handler = Callable[[m.Job], Awaitable[dict[str, Any]]]
MAX_ATTEMPTS = 3


class JobQueue(Protocol):
    def enqueue(self, account_id: str, job_type: str, payload: dict[str, Any]) -> str: ...
    def claim(self, job_id: str | None = None) -> m.Job | None: ...
    def finish(self, job_id: str, result: dict[str, Any]) -> None: ...
    def fail(self, job_id: str, error: str, *, retry_in_s: float | None) -> None: ...


class DbJobQueue:
    def __init__(self, sessions: SessionFactory, clock: Clock, max_running_per_account: int = 0) -> None:
        self._sessions = sessions
        self._clock = clock
        self._max_running = max_running_per_account  # 0 = unlimited

    def enqueue(self, account_id: str, job_type: str, payload: dict[str, Any]) -> str:
        jid = new_id()
        with session_scope(self._sessions) as s:
            s.add(
                m.Job(
                    id=jid,
                    account_id=account_id,
                    type=job_type,
                    status="queued",
                    payload=payload,
                    attempts=0,
                    created_at=int(self._clock()),
                )
            )
        return jid

    def claim(self, job_id: str | None = None) -> m.Job | None:
        now = int(self._clock())
        with session_scope(self._sessions) as s:
            q = select(m.Job).where(m.Job.status == "queued").order_by(m.Job.created_at, m.Job.id)
            if job_id:
                q = q.where(m.Job.id == job_id)
            running: dict[str, int] = dict(
                s.execute(
                    select(m.Job.account_id, func.count()).where(m.Job.status == "running").group_by(m.Job.account_id)
                ).all()
            )
            candidates = [j for j in s.execute(q.limit(200)).scalars() if (j.payload or {}).get("not_before", 0) <= now]
            if self._max_running:
                candidates = [j for j in candidates if running.get(j.account_id, 0) < self._max_running]
            # fair scheduling: the account with the fewest running jobs goes first, then oldest job
            candidates.sort(key=lambda j: (running.get(j.account_id, 0), j.created_at, j.id))
            for job in candidates:
                res = s.execute(
                    update(m.Job)
                    .where(m.Job.id == job.id, m.Job.status == "queued")
                    .values(status="running", started_at=now, attempts=m.Job.attempts + 1)
                )
                if getattr(res, "rowcount", 0) == 1:
                    s.flush()
                    s.refresh(job)
                    s.expunge(job)
                    return job
        return None

    def finish(self, job_id: str, result: dict[str, Any]) -> None:
        with session_scope(self._sessions) as s:
            s.execute(
                update(m.Job)
                .where(m.Job.id == job_id)
                .values(status="succeeded", result=result, finished_at=int(self._clock()), error=None)
            )

    def fail(self, job_id: str, error: str, *, retry_in_s: float | None) -> None:
        now = int(self._clock())
        with session_scope(self._sessions) as s:
            job = s.get(m.Job, job_id)
            if job is None:
                return
            if retry_in_s is not None and job.attempts < MAX_ATTEMPTS:
                job.status = "queued"
                job.payload = {**(job.payload or {}), "not_before": now + int(retry_in_s)}
                job.error = error
            else:
                job.status, job.error, job.finished_at = "failed", error, now

    def recover_stuck(self, max_age_s: int = 3600) -> int:
        """Jobs left `running` by a crashed process are re-queued (their work is idempotent)."""
        cutoff = int(self._clock()) - max_age_s
        with session_scope(self._sessions) as s:
            res = s.execute(update(m.Job).where(m.Job.status == "running", m.Job.started_at < cutoff).values(status="queued"))
            return int(getattr(res, "rowcount", 0) or 0)


class JobRunner:
    def __init__(self, queue: DbJobQueue, handlers: dict[str, Handler]) -> None:
        self._queue = queue
        self._handlers = handlers
        self._stop = asyncio.Event()

    async def run_job(self, job_id: str | None = None) -> m.Job | None:
        job = self._queue.claim(job_id)
        if job is None:
            return None
        handler = self._handlers.get(job.type)
        try:
            if handler is None:
                raise ValueError(f"no handler for job type {job.type}")
            result = await handler(job)
            self._queue.finish(job.id, result)
        except ConnectorError as exc:
            retry = getattr(exc, "retry_after_s", 30.0) if exc.retryable else None
            self._queue.fail(job.id, f"{type(exc).__name__}: {exc}", retry_in_s=retry)
        except Exception as exc:
            logger.exception("job failed", extra={"job_id": job.id, "job_type": job.type})
            self._queue.fail(job.id, f"{type(exc).__name__}: {exc}", retry_in_s=None)
        return job

    async def run_forever(self, poll_s: float, tick: Callable[[], None] | None = None) -> None:
        self._queue.recover_stuck()
        while not self._stop.is_set():
            try:
                if tick:
                    await asyncio.to_thread(tick)
                job = await self.run_job()
                if job is None:
                    await asyncio.wait_for(self._stop.wait(), timeout=poll_s)
            except TimeoutError:
                continue
            except Exception:
                logger.exception("worker loop error")
                await asyncio.sleep(poll_s)

    def stop(self) -> None:
        self._stop.set()
