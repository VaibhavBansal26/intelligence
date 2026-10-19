"""Authentication, RBAC and rate limiting dependencies, plus ETag and idempotency helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request, Response
from sqlalchemy.exc import IntegrityError

from app.db import models as m
from app.db.session import session_scope
from app.deps import Container, get_container
from app.errors import Conflict, Forbidden, RateLimited, Unauthorized
from app.security.crypto import constant_time_equals
from app.security.rbac import Principal


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("missing bearer token")
    return token.strip()


def authenticate(request: Request, c: Container = Depends(get_container)) -> Principal:
    token = _bearer(request)
    ok, wait = c.key_limiter.check("tok:" + hashlib.sha256(token.encode()).hexdigest()[:16])
    if not ok:
        raise RateLimited(
            f"rate limit exceeded; retry in {wait:.0f}s",
        ) from None
    principal = c.accounts.authenticate_key(token) if token.startswith("isk_") else c.accounts.authenticate_jwt(token)
    request.state.principal = principal
    return principal


def require(permission: str) -> Callable[..., Principal]:
    def dep(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.can(permission):
            raise Forbidden(f"missing permission {permission}")
        return principal

    return dep


def require_admin(request: Request, c: Container = Depends(get_container)) -> None:
    key = c.settings.admin_api_key
    header = request.headers.get("x-admin-key", "")
    if not key or not header or not constant_time_equals(header, key):
        raise Unauthorized("invalid admin key")


def etag_or_304(request: Request, c: Container, account_id: str, salt: str = "") -> tuple[str, Response | None]:
    """ETag derived from the account's data version (bumped by sync/train/promotion) + the exact request."""
    tag = (
        '"'
        + hashlib.sha256(
            f"{c.analytics.data_fingerprint(account_id)}|{request.url.path}?{request.url.query}|{salt}".encode()
        ).hexdigest()[:24]
        + '"'
    )
    if request.headers.get("if-none-match") == tag:
        return tag, Response(status_code=304, headers={"ETag": tag})
    return tag, None


def idempotent(
    c: Container, account_id: str, key: str | None, body: Any, fn: Callable[[], tuple[int, dict[str, Any]]]
) -> tuple[int, dict[str, Any], bool]:
    """Run `fn` once per (account, Idempotency-Key); replays return the stored response."""
    if not key:
        status, resp = fn()
        return status, resp, False
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    with session_scope(c.sessions) as s:
        row = s.get(m.IdempotencyKey, (account_id, key))
        if row is not None:
            if row.request_hash != digest:
                raise Conflict("Idempotency-Key reused with a different request", code="IDEMPOTENCY_MISMATCH")
            return row.status_code, row.response, True
    status, resp = fn()
    try:
        with session_scope(c.sessions) as s:
            s.add(
                m.IdempotencyKey(
                    account_id=account_id,
                    key=key,
                    request_hash=digest,
                    status_code=status,
                    response=resp,
                    created_at=int(c.clock()),
                )
            )
    except IntegrityError:
        pass  # concurrent duplicate: first writer wins
    return status, resp, False
