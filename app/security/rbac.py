"""Roles, permissions and the principal attached to a request."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["admin", "analyst", "viewer"]

P_READ = "data:read"
P_ASK = "ask"
P_SYNC = "sync:run"
P_TRAIN = "models:train"
P_MODELS = "models:manage"
P_SOURCES = "sources:manage"
P_KEYS = "keys:manage"
P_ACCOUNT = "account:manage"

ALL_PERMISSIONS = frozenset({P_READ, P_ASK, P_SYNC, P_TRAIN, P_MODELS, P_SOURCES, P_KEYS, P_ACCOUNT})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({P_READ, P_ASK}),
    "analyst": frozenset({P_READ, P_ASK, P_SYNC, P_TRAIN}),
    "admin": ALL_PERMISSIONS,
}


def effective_permissions(role: str, scopes: list[str] | None) -> frozenset[str]:
    base = ROLE_PERMISSIONS.get(role, frozenset())
    if scopes:
        return base & frozenset(scopes)
    return base


@dataclass(frozen=True)
class Principal:
    account_id: str
    role: str
    permissions: frozenset[str]
    key_id: str | None = None
    via: str = "api_key"
    extra: dict[str, str] = field(default_factory=dict)

    def can(self, permission: str) -> bool:
        return permission in self.permissions
