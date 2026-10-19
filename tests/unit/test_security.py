from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import AppError
from app.security.crypto import SecretBox, contact_key, generate_api_key, pepper_fingerprint, sha256_hex
from app.security.limits import RateLimiter
from app.security.rbac import ROLE_PERMISSIONS, effective_permissions
from app.security.ssrf import validate_source_url


def fake_resolver(ip: str):  # type: ignore[no-untyped-def]
    return lambda host, port: [ip]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost:8080",
        "http://10.0.0.5",
        "http://192.168.1.2",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "http://0.0.0.0",
    ],
)
def test_ssrf_blocks_private_ranges(url: str) -> None:
    from app.security.ssrf import default_resolver

    with pytest.raises(AppError) as e:
        validate_source_url(url, allow_private=False, require_https=False, resolver=default_resolver)
    assert e.value.code == "INVALID_SOURCE_URL"


def test_ssrf_blocks_hostnames_resolving_private_and_v4_mapped() -> None:
    for ip in ("10.1.2.3", "::ffff:127.0.0.1", "fe80::1"):
        with pytest.raises(AppError):
            validate_source_url("https://evil.example.com", allow_private=False, require_https=True, resolver=fake_resolver(ip))


def test_ssrf_allows_public_and_dev_override() -> None:
    assert (
        validate_source_url(
            "https://api.example.com/", allow_private=False, require_https=True, resolver=fake_resolver("93.184.216.34")
        )
        == "https://api.example.com"
    )
    assert validate_source_url("http://127.0.0.1:9000", allow_private=True, require_https=False) == "http://127.0.0.1:9000"


@pytest.mark.parametrize("url", ["ftp://x.com", "file:///etc/passwd", "https://user:pw@x.com", "https://"])
def test_ssrf_rejects_bad_schemes_and_credentials(url: str) -> None:
    with pytest.raises(AppError):
        validate_source_url(url, allow_private=True, require_https=False)


def test_https_required_in_production() -> None:
    with pytest.raises(AppError):
        validate_source_url(
            "http://api.example.com", allow_private=False, require_https=True, resolver=fake_resolver("93.184.216.34")
        )


def test_production_settings_guards() -> None:
    with pytest.raises(ValueError):
        Settings(env="production")
    with pytest.raises(ValueError):
        Settings(env="production", kek="x", admin_api_key="y", allow_private_sources=True)


def test_secretbox_roundtrip_rotation_and_tamper() -> None:
    from cryptography.fernet import Fernet

    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old = SecretBox([k1]).encrypt("s3cret")
    assert "s3cret" not in old
    rotated = SecretBox([k2, k1])
    assert rotated.decrypt(old) == "s3cret"
    with pytest.raises(AppError):
        SecretBox([k2]).decrypt(old)
    with pytest.raises(AppError):
        SecretBox(["not-a-key"])


def test_api_key_hashed_and_contact_key_matches_apps_construction() -> None:
    plain, prefix, digest = generate_api_key()
    assert plain.startswith("isk_" + prefix) and digest == sha256_hex(plain) and plain not in digest
    import hashlib
    import hmac

    expected = hmac.new(b"pep", b"a@b.co", hashlib.sha256).hexdigest()
    assert contact_key("pep", "  A@B.co ") == expected
    assert pepper_fingerprint("a") != pepper_fingerprint("b")


def test_rate_limiter_bucket_and_refill() -> None:
    now = [0.0]
    rl = RateLimiter(60, 2, lambda: now[0])
    assert rl.check("k")[0] and rl.check("k")[0]
    ok, wait = rl.check("k")
    assert not ok and wait > 0
    now[0] += 1.0
    assert rl.check("k")[0]
    assert rl.check("other")[0]


def test_scopes_only_narrow_permissions() -> None:
    assert effective_permissions("viewer", ["sync:run", "data:read"]) == frozenset({"data:read"})
    assert effective_permissions("admin", []) == ROLE_PERMISSIONS["admin"]
    assert effective_permissions("nobody", None) == frozenset()
