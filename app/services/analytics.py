"""Read/compute services over an account's landed data: rankings, scores, segments, anomalies, models."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from app.clock import Clock
from app.config import Settings
from app.db import models as m
from app.db.session import SessionFactory, session_scope
from app.errors import NotFound, Unprocessable
from app.features.contacts import build_contact_profiles, build_contact_profiles_columnar, join_report
from app.features.leads import LeadFrame, build_lead_frame
from app.features.rooms import RoomFrame, build_room_frame
from app.models import lead_model, room_model
from app.models.anomaly import DetectorConfig, detect
from app.models.drift import drift_report
from app.models.funnel_rank import rank_steps
from app.models.gbm import TrainOutcome, heuristic_outcome
from app.models.priority import PriorityWeights, rank_open_rooms
from app.models.registry import ModelRegistry
from app.models.scorers import Scorer, explain_by_occlusion
from app.models.segments import SegmentModel, fit_segments, rule_assign, summarise
from app.security.crypto import SecretBox, contact_key

SEGMENTS = "segments"
MODEL_TYPES = [lead_model.MODEL_TYPE, room_model.MODEL_TYPE, SEGMENTS]


def iso(ts: int | float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AnalyticsService:
    def __init__(
        self, sessions: SessionFactory, settings: Settings, registry: ModelRegistry, secrets: SecretBox, clock: Clock
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._registry = registry
        self._secrets = secrets
        self._clock = clock
        self._frames: OrderedDict[tuple[str, str], tuple[str, Any]] = OrderedDict()
        self._frame_lock = threading.Lock()

    # --- account settings -------------------------------------------------------------------------------
    def _account_settings(self, session: Any, account_id: str) -> dict[str, Any]:
        acct = session.get(m.Account, account_id)
        if acct is None:
            raise NotFound("account not found")
        s = acct.settings or {}
        return {
            "sla_s": float(s.get("sla_first_response_s", self._settings.sla_first_response_s)),
            "vip": float(s.get("vip_revenue_threshold", self._settings.vip_revenue_threshold)),
            "horizon": int(s.get("label_horizon_days", self._settings.label_horizon_days)),
        }

    # --- funnels ------------------------------------------------------------------------------------------
    def funnel_rankings(self, account_id: str, *, funnel_id: str | None = None) -> dict[str, Any]:
        with session_scope(self._sessions) as s:
            conn = s.connection()
            steps = pd.read_sql(select(m.FunnelStep).where(m.FunnelStep.account_id == account_id), conn)
            funnels = pd.read_sql(select(m.Funnel).where(m.Funnel.account_id == account_id), conn)
            structure = pd.read_sql(select(m.FunnelStructure).where(m.FunnelStructure.account_id == account_id), conn)
            currency = funnels["currency"].dropna().iloc[0] if not funnels.empty and funnels["currency"].notna().any() else None
        res = rank_steps(steps, funnels, structure, seed=self._settings.seed)
        if funnel_id:
            res["steps"] = [r for r in res["steps"] if r["funnel_id"] == funnel_id]
            if not res["steps"]:
                raise NotFound("funnel not found")
            res["funnels"] = [r for r in res["funnels"] if r["funnel_id"] == funnel_id]
            for i, r in enumerate(res["steps"], 1):
                r["rank"] = i
        res["currency"] = currency
        return res

    # --- leads --------------------------------------------------------------------------------------------
    def _cached_frame(self, kind: str, account_id: str, build: Any) -> Any:
        """Feature frames are rebuilt only when the account's data_version changed (sync, train, settings)."""
        key = (kind, account_id)
        version = self.data_fingerprint(account_id)
        with self._frame_lock:
            hit = self._frames.get(key)
            if hit is not None and hit[0] == version:
                self._frames.move_to_end(key)
                return hit[1]
        frame = build()
        with self._frame_lock:
            self._frames[key] = (version, frame)
            while len(self._frames) > 8:
                self._frames.popitem(last=False)
        return frame

    def _lead_frame(self, account_id: str) -> LeadFrame:
        def build() -> LeadFrame:
            with session_scope(self._sessions) as s:
                cfg = self._account_settings(s, account_id)
                return build_lead_frame(s, account_id, horizon_days=cfg["horizon"])

        frame: LeadFrame = self._cached_frame("lead", account_id, build)
        return frame

    def _resolve_scorer(
        self, account_id: str, model_type: str, frame_df: pd.DataFrame, builder: Any
    ) -> tuple[Scorer, m.ModelVersion | None, dict[str, Any], str | None]:
        hit = self._registry.active_artifact(account_id, model_type)
        if hit is not None:
            row, artifact = hit
            return artifact["scorer"], row, artifact.get("reference", {}), None
        outcome: TrainOutcome = builder(frame_df)
        return outcome.scorer, None, {}, outcome.cold_start_reason or "no trained model yet; using documented heuristic"

    def _lead_scorer(self, account_id: str, frame: LeadFrame) -> tuple[Scorer, m.ModelVersion | None, dict[str, Any], str | None]:
        def build(df: pd.DataFrame) -> TrainOutcome:
            mature = df[df["mature"]] if not df.empty else df
            return heuristic_outcome(
                mature,
                lead_model.lead_spec(self._settings, frame.horizon_days),
                "no trained model yet; using documented heuristic",
            )

        return self._resolve_scorer(account_id, lead_model.MODEL_TYPE, frame.df, build)

    def lead_scores(
        self,
        account_id: str,
        *,
        min_score: float = 0.0,
        limit: int = 50,
        offset: int = 0,
        scope: str = "open",
        explain: bool = True,
    ) -> dict[str, Any]:
        frame = self._lead_frame(account_id)
        scorer, row, _, cold = self._lead_scorer(account_id, frame)
        df = frame.df
        base: dict[str, Any] = {
            "method": scorer.method,
            "model_version": row.version if row else None,
            "cold_start_reason": cold,
            "as_of": iso(frame.as_of),
            "horizon_days": frame.horizon_days,
            "conversation_features_used": frame.has_conversations,
        }
        if df.empty:
            return {**base, "total": 0, "items": []}
        pool = lead_model.open_leads(frame) if scope == "open" else df.copy()
        if pool.empty:
            return {**base, "total": 0, "items": []}
        pool["score"] = scorer.predict(pool)
        pool = pool[pool["score"] >= min_score].sort_values(["score", "created_at", "lead_id"], ascending=[False, False, True])
        total = len(pool)
        page = pool.iloc[offset : offset + limit]
        factors = explain_by_occlusion(scorer, page) if explain else [[] for _ in range(len(page))]
        items = []
        for i, (r, f) in enumerate(zip(page.itertuples(index=False), factors, strict=True), offset + 1):
            items.append(
                {
                    "rank": i,
                    "lead_id": r.lead_id,
                    "contact_key": r.contact_key,
                    "funnel_id": r.funnel_id,
                    "utm_source": r.utm_source,
                    "created_at": iso(r.created_at),
                    "score": round(float(r.score), 4),
                    "top_factors": f,
                }
            )
        return {**base, "total": int(total), "items": items}

    def score_lead(
        self, account_id: str, *, contact_key_value: str | None, email: str | None, features: dict[str, Any] | None
    ) -> dict[str, Any]:
        frame = self._lead_frame(account_id)
        scorer, row, _, cold = self._lead_scorer(account_id, frame)
        meta = {"method": scorer.method, "model_version": row.version if row else None, "cold_start_reason": cold}
        if features is not None:
            df = pd.DataFrame([{c: features.get(c) for c in scorer.features}])
            for c in scorer.features:
                if c not in features:
                    df[c] = np.nan
            who: dict[str, Any] = {"input": "features"}
        else:
            key = contact_key_value
            if key is None and email:
                key = self._key_from_email(account_id, email)
            df_all = frame.df
            sub = df_all[df_all["contact_key"] == key] if not df_all.empty else df_all
            if sub.empty:
                raise NotFound("no lead found for that contact")
            df = sub.sort_values("created_at").tail(1)
            who = {"input": "contact_key", "contact_key": key, "lead_id": df.iloc[0]["lead_id"]}
        p = scorer.predict(df)
        return {**meta, **who, "score": round(float(p[0]), 4), "top_factors": explain_by_occlusion(scorer, df)[0]}

    def score_batch(
        self, account_id: str, *, features: list[dict[str, Any]] | None, contact_keys: list[str] | None
    ) -> dict[str, Any]:
        """Vectorised scoring of up to `batch_max_rows` feature vectors or known contacts in one call."""
        frame = self._lead_frame(account_id)
        scorer, row, _, cold = self._lead_scorer(account_id, frame)
        meta = {"method": scorer.method, "model_version": row.version if row else None, "cold_start_reason": cold}
        if not (features or contact_keys):
            return {**meta, "count": 0, "scores": [], "not_found": []}
        if features is not None:
            df = pd.DataFrame(features).reindex(columns=scorer.features)
            return {**meta, "count": len(df), "scores": [round(float(x), 4) for x in scorer.predict(df)], "not_found": []}
        keys = contact_keys or []
        latest = (
            frame.df.sort_values("created_at").drop_duplicates("contact_key", keep="last").set_index("contact_key")
            if not frame.df.empty
            else pd.DataFrame()
        )
        df = latest.reindex(keys) if len(latest) else pd.DataFrame(index=pd.Index(keys))
        missing = [i for i, k in enumerate(keys) if k not in latest.index] if len(latest) else list(range(len(keys)))
        found = df.drop(index=[keys[i] for i in missing], errors="ignore") if missing else df
        scores: list[float | None] = [None] * len(keys)
        if len(found):
            pred = scorer.predict(df)
            scores = [None if i in set(missing) else round(float(pred[i]), 4) for i in range(len(keys))]
        return {**meta, "count": len(keys), "scores": scores, "not_found": missing}

    def _key_from_email(self, account_id: str, email: str) -> str:
        with session_scope(self._sessions) as s:
            src = (
                s.execute(
                    select(m.Source)
                    .where(m.Source.account_id == account_id, m.Source.status == "active")
                    .order_by(m.Source.created_at)
                )
                .scalars()
                .first()
            )
            if src is None:
                raise NotFound("no source configured, cannot derive contact key")
            pepper = self._secrets.decrypt(src.pepper_enc)
        return contact_key(pepper, email)

    # --- conversations ----------------------------------------------------------------------------------------
    def _room_frame(self, account_id: str) -> RoomFrame:
        def build() -> RoomFrame:
            with session_scope(self._sessions) as s:
                cfg = self._account_settings(s, account_id)
                return build_room_frame(s, account_id, sla_s=cfg["sla_s"])

        frame: RoomFrame = self._cached_frame("room", account_id, build)
        return frame

    def _room_scorer(self, account_id: str, frame: RoomFrame) -> tuple[Scorer, m.ModelVersion | None, dict[str, Any], str | None]:
        def build(df: pd.DataFrame) -> TrainOutcome:
            lab = df[df["label_known"]] if not df.empty else df
            return heuristic_outcome(
                lab, room_model.room_spec(self._settings, frame.sla_s), "no trained model yet; using documented heuristic"
            )

        return self._resolve_scorer(account_id, room_model.MODEL_TYPE, frame.df, build)

    def open_conversations(self, account_id: str, *, limit: int = 25) -> dict[str, Any]:
        frame = self._room_frame(account_id)
        scorer, row, _, cold = self._room_scorer(account_id, frame)
        with session_scope(self._sessions) as s:
            vip = self._account_settings(s, account_id)["vip"]
        meta = {
            "method": scorer.method,
            "model_version": row.version if row else None,
            "cold_start_reason": cold,
            "as_of": iso(frame.as_of),
            "sla_seconds": frame.sla_s,
            "vip_revenue_threshold": vip,
        }
        if frame.df.empty:
            return {**meta, "total_open": 0, "items": []}
        open_df = frame.df[frame.df["is_open"]]
        items = rank_open_rooms(
            open_df, scorer, as_of=frame.as_of, sla_s=frame.sla_s, vip_threshold=vip, weights=PriorityWeights(), limit=limit
        )
        return {**meta, "total_open": len(open_df), "items": items}

    # --- segments --------------------------------------------------------------------------------------------
    def _profiles(self, account_id: str) -> tuple[pd.DataFrame, int]:
        with session_scope(self._sessions) as s:
            profiles = self._profiles_frame(s, account_id)
            from app.features.leads import data_as_of
            from app.features.rooms import rooms_as_of

            as_of = max(data_as_of(s, account_id), rooms_as_of(s, account_id))
        return profiles, as_of

    def _profiles_frame(self, session: Any, account_id: str) -> pd.DataFrame:
        """Contact profiles from SQL, or out-of-core from the Parquet store when it holds this account's refreshed profiles."""
        if self._settings.columnar_dir:
            from app.columnar.profiles import profiles_lazy
            from app.columnar.store import ParquetEventStore

            store = ParquetEventStore(self._settings.columnar_dir, self._settings.columnar_buckets)
            if any((store.root / account_id / "profiles").glob("bucket=*/profile.parquet")):
                return build_contact_profiles_columnar(
                    session, account_id, profiles_lazy(store, account_id), self._settings.ml_max_training_rows
                )
        return build_contact_profiles(session, account_id)

    def segments(self, account_id: str) -> dict[str, Any]:
        profiles, as_of = self._profiles(account_id)
        if profiles.empty:
            return {"method": "none", "contacts": 0, "segments": [], "notes": ["no contacts synced yet"]}
        row = self._registry.active(account_id, SEGMENTS)
        if row is not None:
            model: SegmentModel = self._registry.load_cached(row)["scorer"]
            note = None
        else:
            model, info = fit_segments(profiles, as_of, self._settings.seed, self._settings.min_contacts_for_segments)
            note = (
                "no trained segment model yet; computed on the fly" if model.method == "kmeans" else info.get("cold_start_reason")
            )
        labels = model.assign(profiles, as_of) if model.method != "rules" else rule_assign(profiles)
        return {
            "method": model.method,
            "model_version": row.version if row else None,
            "contacts": len(profiles),
            "segments": summarise(profiles, np.asarray(labels), model.names),
            "notes": [note] if note else [],
        }

    # --- anomalies ---------------------------------------------------------------------------------------------
    def detect_anomalies(self, account_id: str, *, cfg: DetectorConfig | None = None) -> dict[str, Any]:
        now = int(self._clock())
        with session_scope(self._sessions) as s:
            rows = s.execute(
                select(m.Rollup, m.Source.kind)
                .join(m.Source, m.Source.id == m.Rollup.source_id)
                .where(m.Rollup.account_id == account_id, m.Rollup.interval == "day")
            ).all()
            if not rows:
                return {"status": "no_data", "series_checked": 0, "alerts": [], "notes": ["no rollup series synced yet"]}
            df = pd.DataFrame(
                [
                    {"kind": k, "source_id": r.source_id, "metric": r.metric, "scope": r.scope, "t": r.t, "value": r.value}
                    for r, k in rows
                ]
            )
            df = _with_rate_series(df)
            # never judge days the source has not delivered yet (a stale sync would otherwise look like a traffic collapse)
            data_end = {
                kind: min(now, int(wm))
                for kind, wm in s.execute(
                    select(m.Source.kind, func.max(m.SyncState.watermark))
                    .join(m.SyncState, m.SyncState.source_id == m.Source.id)
                    .where(m.Source.account_id == account_id, m.SyncState.resource.in_(("events", "rooms")))
                    .group_by(m.Source.kind)
                ).all()
                if wm
            }
            alerts: list[dict[str, Any]] = []
            skipped = 0
            for (kind, metric, scope), g in df.groupby(["kind", "metric", "scope"]):
                g = (
                    g.groupby("t", as_index=False)["value"].sum(min_count=1)
                    if metric
                    in (
                        "visits",
                        "unique_visits",
                        "optins",
                        "orders",
                        "gross",
                        "rooms_opened",
                        "messages_in",
                        "messages_out",
                        "backlog",
                    )
                    else g.drop_duplicates("t")
                )
                g = g[g["t"] + 86400 <= data_end.get(str(kind), now)].sort_values("t")  # never judge the still-running bucket
                if g.empty:
                    continue
                full_t = np.arange(int(g.t.min()), int(g.t.max()) + 1, 86400, dtype=np.int64)
                vals = g.set_index("t")["value"].reindex(full_t).to_numpy(dtype=float)
                rate_like = metric.endswith("_rate") or metric.endswith("_p50")
                first_nz = int(np.argmax(np.nan_to_num(vals) != 0)) if np.any(np.nan_to_num(vals) != 0) else len(vals)
                vals, full_t = vals[first_nz:], full_t[first_nz:]  # series start when data starts, not at the request window
                if len(vals) and np.mean(np.nan_to_num(vals) == 0) > 0.3:
                    skipped += 1  # too sparse for a seasonal baseline to say anything useful
                    continue
                res = detect(
                    vals,
                    cfg or (DetectorConfig(count_like=False, min_rel_dev=0.3, z_floor=5.0) if rate_like else DetectorConfig()),
                )
                if res.status != "ok":
                    skipped += 1
                    continue
                for ep in res.episodes:
                    fp = ep.fingerprint(str(metric), str(scope), full_t)
                    detail = {
                        "observed_mean": round(ep.observed, 4),
                        "expected_mean": round(ep.expected, 4),
                        "detectors": ep.detectors,
                        "days": ep.end_idx - ep.start_idx + 1,
                    }
                    alerts.append(
                        self._upsert_alert(
                            s,
                            account_id,
                            fp,
                            str(kind),
                            str(metric),
                            str(scope),
                            ep.direction,
                            ep.kind,
                            int(full_t[ep.start_idx]),
                            int(full_t[ep.end_idx]),
                            ep.severity,
                            detail,
                            now,
                        )
                    )
            s.flush()
            alerts.sort(key=lambda a: (-a["end_t"], -a["severity"]))
        return {
            "status": "ok",
            "series_checked": int(df.groupby(["kind", "metric", "scope"]).ngroups),
            "series_skipped_insufficient_history": skipped,
            "alerts": alerts,
            "notes": [],
        }

    def _upsert_alert(
        self,
        s: Any,
        account_id: str,
        fp: str,
        kind: str,
        metric: str,
        scope: str,
        direction: str,
        ep_kind: str,
        start: int,
        end: int,
        severity: float,
        detail: dict[str, Any],
        now: int,
    ) -> dict[str, Any]:
        row = s.execute(
            select(m.AnomalyAlert).where(m.AnomalyAlert.account_id == account_id, m.AnomalyAlert.fingerprint == fp)
        ).scalar_one_or_none()
        is_new = row is None
        if row is None:
            row = m.AnomalyAlert(
                account_id=account_id,
                fingerprint=fp,
                source_kind=kind,
                metric=metric,
                scope=scope,
                direction=direction,
                kind=ep_kind,
                start_t=start,
                end_t=end,
                severity=severity,
                detail=detail,
                first_seen_at=now,
                last_seen_at=now,
            )
            s.add(row)
        else:
            row.end_t, row.severity, row.detail, row.last_seen_at, row.kind = (
                end,
                max(row.severity, severity),
                detail,
                now,
                ep_kind,
            )
        return {
            "fingerprint": fp,
            "new": is_new,
            "source": kind,
            "metric": metric,
            "scope": scope,
            "direction": direction,
            "kind": ep_kind,
            "start": iso(start),
            "end": iso(end),
            "start_t": start,
            "end_t": end,
            "severity": round(float(severity), 2),
            "detail": detail,
        }

    # --- models ---------------------------------------------------------------------------------------------------
    def train(self, account_id: str, model_types: list[str] | None = None) -> list[dict[str, Any]]:
        types = model_types or MODEL_TYPES
        bad = [t for t in types if t not in MODEL_TYPES]
        if bad:
            raise Unprocessable(f"unknown model type(s): {', '.join(bad)}", code="UNKNOWN_MODEL_TYPE")
        out: list[dict[str, Any]] = []
        for t in types:
            if t == lead_model.MODEL_TYPE:
                outcome: TrainOutcome = lead_model.train_lead_model(self._lead_frame(account_id), self._settings)
                artifact: Any = {"scorer": outcome.scorer, "reference": outcome.reference}
            elif t == room_model.MODEL_TYPE:
                outcome = room_model.train_room_model(self._room_frame(account_id), self._settings)
                artifact = {"scorer": outcome.scorer, "reference": outcome.reference}
            else:
                profiles, as_of = self._profiles(account_id)
                model, info = fit_segments(profiles, as_of, self._settings.seed, self._settings.min_contacts_for_segments)
                card = {
                    "model": SEGMENTS,
                    "method": model.method,
                    "features": ["log1p counts/revenue", "sentiment", "recency", "source flags"],
                    "seed": self._settings.seed,
                    "limitations": ["Cluster names are rule-based labels over centroid statistics, not ground truth."],
                }
                row = self._registry.register(
                    account_id,
                    SEGMENTS,
                    method=model.method,
                    artifact={"scorer": model, "reference": {}},
                    metrics=info,
                    card=card,
                    seed=self._settings.seed,
                    trained_rows=len(profiles),
                )
                out.append(self._finalise(account_id, row, info))
                continue
            row = self._registry.register(
                account_id,
                t,
                method=outcome.method,
                artifact=artifact,
                metrics=outcome.metrics,
                card=outcome.card,
                seed=self._settings.seed,
                trained_rows=outcome.trained_rows,
            )
            out.append(self._finalise(account_id, row, outcome.metrics))
        with session_scope(self._sessions) as s:
            acct = s.get(m.Account, account_id)
            if acct:
                acct.data_version += 1
        return out

    def _finalise(self, account_id: str, row: m.ModelVersion, metrics: dict[str, Any]) -> dict[str, Any]:
        """Auto-promotion policy: promote when nothing is active, or the new model is not meaningfully worse."""
        active = self._registry.active(account_id, row.model_type)
        promote = False
        reason = "kept as candidate"
        if active is None:
            promote, reason = True, "no active version"
        elif row.model_type == SEGMENTS:
            promote, reason = True, "segment models are refreshed on retrain"
        elif row.method == "gbm" and active.method != "gbm":
            promote, reason = True, "first trained model replaces heuristic"
        elif row.method == "gbm" and active.method == "gbm":
            old, new = active.metrics.get("auc") or 0.0, metrics.get("auc") or 0.0
            if new >= old - 0.02:
                promote, reason = True, f"validation AUC {new} within 0.02 of active {old}"
            else:
                reason = f"validation AUC {new} is worse than active {old} by more than 0.02"
        if promote:
            row = self._registry.promote(account_id, row.id)
        return {
            "id": row.id,
            "model_type": row.model_type,
            "version": row.version,
            "status": row.status,
            "method": row.method,
            "promotion": reason,
            "metrics": row.metrics,
        }

    def drift(self, account_id: str, model_type: str, *, recent_days: int = 14) -> dict[str, Any]:
        row = self._registry.active(account_id, model_type)
        if row is None:
            raise NotFound("no active model of that type")
        artifact = self._registry.load_cached(row)
        if model_type == lead_model.MODEL_TYPE:
            frame = self._lead_frame(account_id)
            df = frame.df
            recent = df[df["created_at"] >= frame.as_of - recent_days * 86400] if not df.empty else df
        elif model_type == room_model.MODEL_TYPE:
            rframe = self._room_frame(account_id)
            df = rframe.df
            recent = df[df["created_at"] >= rframe.as_of - recent_days * 86400] if not df.empty else df
        else:
            return {"model_type": model_type, "status": "not_applicable", "features": []}
        rep = drift_report(
            artifact["scorer"],
            artifact.get("reference", {}),
            recent,
            warn=self._settings.psi_warn,
            alert=self._settings.psi_alert,
        )
        return {"model_type": model_type, "version": row.version, "recent_days": recent_days, **rep}

    # --- diagnostics ------------------------------------------------------------------------------------------------
    def join(self, account_id: str) -> dict[str, Any]:
        with session_scope(self._sessions) as s:
            return join_report(s, account_id)

    def data_fingerprint(self, account_id: str) -> str:
        with session_scope(self._sessions) as s:
            acct = s.get(m.Account, account_id)
            return hashlib.sha256(f"{account_id}:{acct.data_version if acct else 0}".encode()).hexdigest()[:16]


def _with_rate_series(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived rate series (opt-in rate, order rate) where both numerator and denominator exist."""
    extra: list[pd.DataFrame] = []
    for (kind, scope), g in df[df.kind == "funnels"].groupby(["kind", "scope"]):
        piv = g.pivot_table(index="t", columns="metric", values="value", aggfunc="sum")
        for num, den, name in (("optins", "unique_visits", "optin_rate"), ("orders", "unique_visits", "order_rate")):
            if num in piv and den in piv:
                rate = (piv[num] / piv[den].where(piv[den] >= 30)).dropna()
                if len(rate):
                    extra.append(
                        pd.DataFrame(
                            {
                                "kind": kind,
                                "source_id": "derived",
                                "metric": name,
                                "scope": scope,
                                "t": rate.index,
                                "value": rate.values,
                            }
                        )
                    )
    return pd.concat([df, *extra], ignore_index=True) if extra else df
