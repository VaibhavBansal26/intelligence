"""SSRF protection for user-supplied source URLs."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from app.errors import AppError

Resolver = Callable[[str, int], list[str]]


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(i[4][0]) for i in infos]


def _is_forbidden(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def validate_source_url(
    url: str,
    *,
    allow_private: bool,
    require_https: bool,
    resolver: Resolver = default_resolver,
) -> str:
    """Return the normalised base URL or raise AppError(INVALID_SOURCE_URL).

    Redirects are never followed by the connector, so a validated host cannot bounce us elsewhere.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise AppError("source URL must be http(s)", code="INVALID_SOURCE_URL")
    if require_https and parts.scheme != "https":
        raise AppError("source URL must use https", code="INVALID_SOURCE_URL")
    if not parts.hostname:
        raise AppError("source URL has no host", code="INVALID_SOURCE_URL")
    if parts.username or parts.password:
        raise AppError("credentials in URL are not allowed", code="INVALID_SOURCE_URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not allow_private:
        try:
            addresses = resolver(parts.hostname, port)
        except OSError as exc:
            raise AppError("source host cannot be resolved", code="INVALID_SOURCE_URL") from exc
        if not addresses:
            raise AppError("source host cannot be resolved", code="INVALID_SOURCE_URL")
        for addr in addresses:
            if _is_forbidden(addr):
                raise AppError("source host resolves to a non-public address", code="INVALID_SOURCE_URL")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
