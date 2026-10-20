"""Accounts, API keys, JWT exchange and source registration (all strictly account-scoped)."""

from __future__ import annotations

from typing import Any

import jwt
from sqlalchemy import bindparam, delete, func, select, update

from app.clock import Clock
from app.config import Settings
from app.db import models as m
from app.db.session import SessionFactory, session_scope, upsert
from app.errors import Conflict, NotFound, Unauthorized, Unprocessable
from app.security.crypto import SecretBox, contact_key, generate_api_key, new_id, pepper_fingerprint, sha256_hex
from app.security.rbac import ROLE_PERMISSIONS, Principal, effective_permissions
from app.security.ssrf import Resolver, default_resolver, validate_source_url

SOURCE_KINDS = ("funnels", "conversations")


class AccountService:
    def __init__(
        self,
        sessions: SessionFactory,
        settings: Settings,
        secrets: SecretBox,
        clock: Clock,
        resolver: Resolver = default_resolver,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._secrets = secrets
        self._clock = clock
        self._resolver = resolver

    # --- accounts -----------------------------------------------------------------------------------------
    def create_account(self, name: str, settings: dict[str, Any] | None = None) -> m.Account:
        with session_scope(self._sessions) as s:
            acct = m.Account(
                id=new_id(),
                name=name.strip(),
                created_at=int(self._clock()),
                data_version=1,
                settings=settings or {},
                disabled=False,
            )
            s.add(acct)
            s.flush()
            s.expunge(acct)
            return acct

    def get_account(self, account_id: str) -> m.Account:
        with session_scope(self._sessions) as s:
            acct = s.get(m.Account, account_id)
            if acct is None:
                raise NotFound("account not found")
            s.expunge(acct)
            return acct

    def list_accounts(self) -> list[m.Account]:
        with session_scope(self._sessions) as s:
            rows = list(s.execute(select(m.Account).order_by(m.Account.created_at, m.Account.id)).scalars())
            for r in rows:
                s.expunge(r)
            return rows

    def update_account(
        self, account_id: str, *, name: str | None, settings: dict[str, Any] | None, disabled: bool | None = None
    ) -> m.Account:
        with session_scope(self._sessions) as s:
            acct = s.get(m.Account, account_id)
            if acct is None:
                raise NotFound("account not found")
            if name:
                acct.name = name.strip()
            if settings is not None:
                acct.settings = {**(acct.settings or {}), **settings}
            if disabled is not None:
                acct.disabled = disabled
            acct.data_version += 1
            s.flush()
            s.expunge(acct)
            return acct

    # --- keys ---------------------------------------------------------------------------------------------------
    def create_key(
        self, account_id: str, *, name: str, role: str, scopes: list[str] | None = None, expires_at: int | None = None
    ) -> tuple[m.ApiKey, str]:
        if role not in ROLE_PERMISSIONS:
            raise Unprocessable("role must be admin, analyst or viewer", code="INVALID_ROLE")
        bad = [x for x in (scopes or []) if x not in ROLE_PERMISSIONS["admin"]]
        if bad:
            raise Unprocessable(f"unknown scopes: {', '.join(bad)}", code="INVALID_SCOPE")
        plaintext, prefix, digest = generate_api_key()
        with session_scope(self._sessions) as s:
            if s.get(m.Account, account_id) is None:
                raise NotFound("account not found")
            row = m.ApiKey(
                id=new_id(),
                account_id=account_id,
                name=name,
                prefix=prefix,
                key_hash=digest,
                role=role,
                scopes=scopes or [],
                expires_at=expires_at,
                created_at=int(self._clock()),
            )
            s.add(row)
            s.flush()
            s.expunge(row)
        return row, plaintext

    def list_keys(self, account_id: str) -> list[m.ApiKey]:
        with session_scope(self._sessions) as s:
            rows = list(
                s.execute(
                    select(m.ApiKey).where(m.ApiKey.account_id == account_id).order_by(m.ApiKey.created_at, m.ApiKey.id)
                ).scalars()
            )
            for r in rows:
                s.expunge(r)
            return rows

    def revoke_key(self, account_id: str, key_id: str) -> None:
        with session_scope(self._sessions) as s:
            row = s.get(m.ApiKey, key_id)
            if row is None or row.account_id != account_id:
                raise NotFound("api key not found")
            row.revoked_at = int(self._clock())

    def authenticate_key(self, plaintext: str) -> Principal:
        digest = sha256_hex(plaintext)
        now = int(self._clock())
        with session_scope(self._sessions) as s:
            row = s.execute(select(m.ApiKey).where(m.ApiKey.key_hash == digest)).scalar_one_or_none()
            if row is None or row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
                raise Unauthorized("invalid, revoked or expired API key")
            acct = s.get(m.Account, row.account_id)
            if acct is None or acct.disabled:
                raise Unauthorized("account is disabled")
            if row.last_used_at is None or now - row.last_used_at > 60:
                s.execute(update(m.ApiKey).where(m.ApiKey.id == row.id).values(last_used_at=now))
            return Principal(row.account_id, row.role, effective_permissions(row.role, row.scopes), key_id=row.id)

    # --- JWT --------------------------------------------------------------------------------------------------------
    def issue_jwt(self, principal: Principal) -> tuple[str, int]:
        if not self._settings.jwt_secret:
            raise Conflict("JWT is not enabled on this deployment", code="JWT_DISABLED")
        now = int(self._clock())
        claims = {
            "sub": principal.key_id,
            "account_id": principal.account_id,
            "role": principal.role,
            "perms": sorted(principal.permissions),
            "iat": now,
            "exp": now + self._settings.jwt_ttl_s,
            "iss": "intelligence",
        }
        return jwt.encode(claims, self._settings.jwt_secret, algorithm="HS256"), self._settings.jwt_ttl_s

    def authenticate_jwt(self, token: str) -> Principal:
        if not self._settings.jwt_secret:
            raise Unauthorized("invalid credentials")
        try:
            claims = jwt.decode(
                token,
                self._settings.jwt_secret,
                algorithms=["HS256"],
                issuer="intelligence",
                options={"require": ["exp", "account_id", "role"], "verify_exp": False},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid or expired token") from exc
        if float(claims["exp"]) + 5 < self._clock():  # expiry is judged on the injected clock
            raise Unauthorized("invalid or expired token")
        with session_scope(self._sessions) as s:
            acct = s.get(m.Account, claims["account_id"])
            if acct is None or acct.disabled:
                raise Unauthorized("account is disabled")
        perms = frozenset(claims.get("perms", [])) & ROLE_PERMISSIONS.get(claims["role"], frozenset())
        return Principal(claims["account_id"], claims["role"], perms, key_id=claims.get("sub"), via="jwt")

    # --- sources --------------------------------------------------------------------------------------------------------
    def create_source(self, account_id: str, *, kind: str, name: str, base_url: str, api_key: str, pepper: str) -> m.Source:
        if kind not in SOURCE_KINDS:
            raise Unprocessable("kind must be 'funnels' or 'conversations'", code="INVALID_SOURCE_KIND")
        url = validate_source_url(
            base_url,
            allow_private=self._settings.allow_private_sources,
            require_https=self._settings.env == "production",
            resolver=self._resolver,
        )
        now = int(self._clock())
        with session_scope(self._sessions) as s:
            if s.get(m.Account, account_id) is None:
                raise NotFound("account not found")
            if s.execute(select(m.Source.id).where(m.Source.account_id == account_id, m.Source.name == name)).first():
                raise Conflict("a source with that name already exists", code="SOURCE_EXISTS")
            row = m.Source(
                id=new_id(),
                account_id=account_id,
                kind=kind,
                name=name,
                base_url=url,
                api_key_enc=self._secrets.encrypt(api_key),
                pepper_enc=self._secrets.encrypt(pepper),
                pepper_fp=pepper_fingerprint(pepper),
                status="active",
                created_at=now,
                updated_at=now,
            )
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def list_sources(self, account_id: str) -> list[m.Source]:
        with session_scope(self._sessions) as s:
            rows = list(
                s.execute(
                    select(m.Source).where(m.Source.account_id == account_id).order_by(m.Source.created_at, m.Source.id)
                ).scalars()
            )
            for r in rows:
                s.expunge(r)
            return rows

    def get_source(self, account_id: str, source_id: str) -> m.Source:
        with session_scope(self._sessions) as s:
            row = s.get(m.Source, source_id)
            if row is None or row.account_id != account_id:
                raise NotFound("source not found")
            s.expunge(row)
            return row

    def update_source(
        self,
        account_id: str,
        source_id: str,
        *,
        name: str | None,
        base_url: str | None,
        api_key: str | None,
        pepper: str | None,
        status: str | None,
    ) -> m.Source:
        url = None
        if base_url:
            url = validate_source_url(
                base_url,
                allow_private=self._settings.allow_private_sources,
                require_https=self._settings.env == "production",
                resolver=self._resolver,
            )
        with session_scope(self._sessions) as s:
            row = s.get(m.Source, source_id)
            if row is None or row.account_id != account_id:
                raise NotFound("source not found")
            if name:
                row.name = name
            if url:
                row.base_url = url
            if api_key:
                row.api_key_enc = self._secrets.encrypt(api_key)
            if pepper:
                row.pepper_enc, row.pepper_fp = self._secrets.encrypt(pepper), pepper_fingerprint(pepper)
                self._unlink(s, source_id)  # links were computed with the old pepper
            if status:
                if status not in ("active", "paused"):
                    raise Unprocessable("status must be active or paused", code="INVALID_STATUS")
                row.status = status
            row.updated_at = int(self._clock())
            s.flush()
            s.expunge(row)
            return row

    def delete_source(self, account_id: str, source_id: str) -> None:
        with session_scope(self._sessions) as s:
            row = s.get(m.Source, source_id)
            if row is None or row.account_id != account_id:
                raise NotFound("source not found")
            tables: tuple[Any, ...] = (
                m.Event,
                m.Lead,
                m.Funnel,
                m.FunnelStep,
                m.FunnelStructure,
                m.Room,
                m.MessageMeta,
                m.Rollup,
                m.SyncState,
            )
            for table in tables:
                s.execute(delete(table).where(table.source_id == source_id))
            s.execute(delete(m.ContactLink).where(m.ContactLink.source_id == source_id))
            s.delete(row)
            s.execute(update(m.Account).where(m.Account.id == account_id).values(data_version=m.Account.data_version + 1))

    # --- identity bridge ---------------------------------------------------------------------------------------
    @staticmethod
    def _unlink(s: Any, source_id: str) -> None:
        s.execute(delete(m.ContactLink).where(m.ContactLink.source_id == source_id))
        s.execute(
            update(m.Room)
            .where(m.Room.source_id == source_id, m.Room.contact_key_raw.is_not(None))
            .values(contact_key=m.Room.contact_key_raw, contact_key_raw=None)
        )

    def links_for(self, source_id: str) -> dict[str, str]:
        """source contact_key -> canonical key, for ingest-time re-keying of new rows."""
        with session_scope(self._sessions) as s:
            return dict(
                s.execute(
                    select(m.ContactLink.contact_key, m.ContactLink.canonical_key).where(m.ContactLink.source_id == source_id)
                ).all()
            )

    def apply_identity_bridge(self, account_id: str, emails: list[str], *, replace: bool = False) -> dict[str, Any]:
        """Link the same people across sources whose identity peppers differ.

        `emails` is a contact list the customer already holds (CRM export, newsletter list). For each address the
        contact_key under every active source's pepper is computed in memory; the address itself is discarded. The
        canonical key of a person is the key of the first `funnels` source (else the first source); rooms of the other
        sources are re-keyed to it, and rows ingested later are re-keyed on arrival. Calls accumulate (a long list can be
        sent in several calls); `replace=True` first drops the existing links."""
        norm = sorted({e.strip().lower() for e in emails if isinstance(e, str) and "@" in e})
        if not norm:
            raise Unprocessable("no usable e-mail addresses in the list", code="EMPTY_BRIDGE")
        with session_scope(self._sessions) as s:
            sources = list(
                s.execute(
                    select(m.Source)
                    .where(m.Source.account_id == account_id, m.Source.status == "active")
                    .order_by(m.Source.created_at, m.Source.id)
                ).scalars()
            )
            if len(sources) < 2:
                raise Unprocessable("an identity bridge needs at least two active sources", code="BRIDGE_NEEDS_TWO_SOURCES")
            peppers = {src.id: self._secrets.decrypt(src.pepper_enc) for src in sources}
            canon_src = next((x for x in sources if x.kind == "funnels"), sources[0])
            now = int(self._clock())
            linked: dict[str, int] = {}
            relinked = 0
            for src in sources:
                if src.id == canon_src.id:
                    continue
                if replace:
                    self._unlink(s, src.id)
                rows = []
                for e in norm:
                    k = contact_key(peppers[src.id], e)
                    c = contact_key(peppers[canon_src.id], e)
                    if k != c:
                        rows.append(
                            {
                                "source_id": src.id,
                                "contact_key": k,
                                "account_id": account_id,
                                "canonical_key": c,
                                "created_at": now,
                            }
                        )
                for i in range(0, len(rows), 5000):
                    upsert(s, m.ContactLink.__table__, rows[i : i + 5000])
                linked[src.id] = len(rows)
                stmt = (
                    update(m.Room)
                    .where(m.Room.source_id == bindparam("b_sid"), m.Room.contact_key == bindparam("b_raw"))
                    .values(contact_key_raw=m.Room.contact_key, contact_key=bindparam("b_canon"))
                )
                params = [{"b_sid": r["source_id"], "b_raw": r["contact_key"], "b_canon": r["canonical_key"]} for r in rows]
                conn = s.connection()
                for i in range(0, len(params), 2000):
                    res = conn.execute(stmt, params[i : i + 2000])
                    relinked += max(0, int(getattr(res, "rowcount", 0) or 0))
            s.execute(update(m.Account).where(m.Account.id == account_id).values(data_version=m.Account.data_version + 1))
            return {
                "emails_received": len(emails),
                "emails_usable": len(norm),
                "canonical_source_id": canon_src.id,
                "links_by_source": linked,
                "rooms_rekeyed": relinked,
            }

    def identity_bridge_status(self, account_id: str) -> dict[str, Any]:
        with session_scope(self._sessions) as s:
            rows = s.execute(
                select(m.ContactLink.source_id, func.count())
                .where(m.ContactLink.account_id == account_id)
                .group_by(m.ContactLink.source_id)
            ).all()
            return {"links_by_source": {sid: int(n) for sid, n in rows}, "total_links": int(sum(n for _, n in rows))}
