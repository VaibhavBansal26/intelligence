"""Secret encryption at rest (Fernet, rotating keys) and hashing helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.errors import AppError


class SecretBox:
    """Encrypts source credentials with a KEK from the environment. First key encrypts, all decrypt."""

    def __init__(self, keys: list[str]) -> None:
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:  # invalid key material
            raise AppError("KEK is not a valid urlsafe-base64 32-byte key", code="CONFIG_ERROR") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise AppError("cannot decrypt stored secret (KEK changed?)", code="SECRET_UNREADABLE") from exc


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(sha256_hex(a), sha256_hex(b))


def new_id() -> str:
    return secrets.token_hex(8)


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, sha256). Plaintext is shown once and never stored."""
    prefix = secrets.token_hex(4)
    plaintext = f"isk_{prefix}_{secrets.token_urlsafe(32)}"
    return plaintext, prefix, sha256_hex(plaintext)


def pepper_fingerprint(pepper: str) -> str:
    """Short non-reversible fingerprint used to detect pepper mismatch between sources."""
    return hashlib.sha256(b"pepper-fp:" + pepper.encode()).hexdigest()[:12]


def contact_key(pepper: str, email: str) -> str:
    """Same construction as the source apps: HMAC-SHA256(pepper, lower(trim(email))) hex."""
    return hmac.new(pepper.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()
