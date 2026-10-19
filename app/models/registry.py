"""Versioned model registry: joblib artifacts on disk (sha256-verified) + metadata rows, promote and rollback."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import func, select

from app.clock import Clock
from app.db import models as m
from app.db.session import SessionFactory, session_scope
from app.errors import Conflict, NotFound
from app.security.crypto import new_id


class ModelRegistry:
    def __init__(self, sessions: SessionFactory, model_dir: str, clock: Clock, cache_size: int = 32) -> None:
        self._sessions = sessions
        self._dir = Path(model_dir)
        self._clock = clock
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self.cache_hits = 0
        self.cache_loads = 0

    def register(
        self,
        account_id: str,
        model_type: str,
        *,
        method: str,
        artifact: Any,
        metrics: dict[str, Any],
        card: dict[str, Any],
        seed: int,
        trained_rows: int,
    ) -> m.ModelVersion:
        with session_scope(self._sessions) as s:
            version = (
                s.execute(
                    select(func.max(m.ModelVersion.version)).where(
                        m.ModelVersion.account_id == account_id, m.ModelVersion.model_type == model_type
                    )
                ).scalar()
                or 0
            ) + 1
            path = self._dir / account_id / model_type / f"v{version}.joblib"
            path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(artifact, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            row = m.ModelVersion(
                id=new_id(),
                account_id=account_id,
                model_type=model_type,
                version=version,
                status="candidate",
                method=method,
                metrics=_json_safe(metrics),
                card=_json_safe({**card, "version": version}),
                artifact_path=str(path),
                artifact_sha256=digest,
                seed=seed,
                trained_rows=trained_rows,
                created_at=int(self._clock()),
            )
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def load(self, row: m.ModelVersion) -> Any:
        path = Path(row.artifact_path or "")
        if not path.is_file():
            raise NotFound("model artifact missing on disk")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.artifact_sha256:
            raise Conflict("model artifact failed integrity check", code="ARTIFACT_INTEGRITY")
        return joblib.load(path)

    def load_cached(self, row: m.ModelVersion) -> Any:
        """Artifact of a version, loaded (and sha256-verified) once and kept in an LRU. Versions are immutable, so the
        cache is always valid; hot swap happens because callers resolve the ACTIVE version id on every request."""
        with self._lock:
            if row.id in self._cache:
                self._cache.move_to_end(row.id)
                self.cache_hits += 1
                return self._cache[row.id]
        artifact = self.load(row)
        with self._lock:
            self._cache[row.id] = artifact
            self.cache_loads += 1
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return artifact

    def active_artifact(self, account_id: str, model_type: str) -> tuple[m.ModelVersion, Any] | None:
        row = self.active(account_id, model_type)
        return None if row is None else (row, self.load_cached(row))

    def active(self, account_id: str, model_type: str) -> m.ModelVersion | None:
        with session_scope(self._sessions) as s:
            row = s.execute(
                select(m.ModelVersion).where(
                    m.ModelVersion.account_id == account_id,
                    m.ModelVersion.model_type == model_type,
                    m.ModelVersion.status == "active",
                )
            ).scalar_one_or_none()
            if row:
                s.expunge(row)
            return row

    def get(self, account_id: str, version_id: str) -> m.ModelVersion:
        with session_scope(self._sessions) as s:
            row = s.get(m.ModelVersion, version_id)
            if row is None or row.account_id != account_id:
                raise NotFound("model version not found")
            s.expunge(row)
            return row

    def list(self, account_id: str, model_type: str | None = None) -> list[m.ModelVersion]:
        with session_scope(self._sessions) as s:
            q = (
                select(m.ModelVersion)
                .where(m.ModelVersion.account_id == account_id)
                .order_by(m.ModelVersion.model_type, m.ModelVersion.version.desc())
            )
            if model_type:
                q = q.where(m.ModelVersion.model_type == model_type)
            rows = list(s.execute(q).scalars())
            for r in rows:
                s.expunge(r)
            return rows

    def promote(self, account_id: str, version_id: str) -> m.ModelVersion:
        with session_scope(self._sessions) as s:
            row = s.get(m.ModelVersion, version_id)
            if row is None or row.account_id != account_id:
                raise NotFound("model version not found")
            for other in s.execute(
                select(m.ModelVersion).where(
                    m.ModelVersion.account_id == account_id,
                    m.ModelVersion.model_type == row.model_type,
                    m.ModelVersion.status == "active",
                )
            ).scalars():
                other.status = "archived"
            row.status, row.promoted_at = "active", int(self._clock())
            s.flush()
            s.expunge(row)
            return row

    def rollback(self, account_id: str, model_type: str) -> m.ModelVersion:
        with session_scope(self._sessions) as s:
            current = s.execute(
                select(m.ModelVersion).where(
                    m.ModelVersion.account_id == account_id,
                    m.ModelVersion.model_type == model_type,
                    m.ModelVersion.status == "active",
                )
            ).scalar_one_or_none()
            if current is None:
                raise Conflict("no active version to roll back", code="NO_ACTIVE_MODEL")
            prev = (
                s.execute(
                    select(m.ModelVersion)
                    .where(
                        m.ModelVersion.account_id == account_id,
                        m.ModelVersion.model_type == model_type,
                        m.ModelVersion.version < current.version,
                        m.ModelVersion.status == "archived",
                    )
                    .order_by(m.ModelVersion.version.desc())
                )
                .scalars()
                .first()
            )
            if prev is None:
                raise Conflict("no earlier version to roll back to", code="NO_PREVIOUS_MODEL")
            current.status = "rolled_back"
            prev.status, prev.promoted_at = "active", int(self._clock())
            s.flush()
            s.expunge(prev)
            return prev


def _json_safe(obj: Any) -> Any:
    import json

    return json.loads(json.dumps(obj, default=_default))


def _default(o: Any) -> Any:
    try:
        import numpy as np

        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return None if np.isnan(o) else float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(o)
