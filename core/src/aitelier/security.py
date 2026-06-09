"""Security helpers — small, pure functions, no external deps."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

_PATH_COMPONENT_CHARSET = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
_PATH_COMPONENT_MAX_LEN = 256


def validate_path_component(value: str, label: str) -> None:
    """Reject path traversal in user-supplied URL path segments.

    Whitelisted charset + explicit `..` ban + length cap. Used wherever
    a segment of a `/v1/<…>/{name}` route is concatenated into a
    filesystem path or a downstream URL — `run_id`, `schedule_id`,
    schema name, agent name, etc. The length cap guards against
    pathological inputs that pass the charset (e.g. a 10 MiB run_id
    matching `[A-Za-z0-9._-]+`).
    """
    if not value or len(value) > _PATH_COMPONENT_MAX_LEN:
        raise HTTPException(
            status_code=400, detail=f"Invalid {label}: length must be 1..{_PATH_COMPONENT_MAX_LEN}",
        )
    if not _PATH_COMPONENT_CHARSET.match(value):
        raise HTTPException(
            status_code=400, detail=f"Invalid {label}: charset",
        )
    if ".." in value:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: path traversal not allowed",
        )


def _is_public_addr(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _public_url_sync(url: str) -> bool:
    """Synchronous version. Performs blocking DNS — see `is_public_url`
    for the async wrapper that callers in an event loop should use."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return all(_is_public_addr(info[4][0]) for info in infos)


async def is_public_url(url: str) -> bool:
    """True iff `url` resolves to a public, externally routable host.

    Used to gate aitelier-initiated outbound requests (webhook delivery)
    against SSRF: a consumer who can POST a `webhook_url` shouldn't be
    able to make aitelier scan the metadata service or internal subnets.

    Rejects: loopback, private, link-local, multicast, reserved, unspecified.
    Rejects names that resolve to any such address. Non-http(s) schemes
    are refused outright.

    Offloads the blocking DNS lookup to a worker thread so async handlers
    don't stall the event loop.
    """
    return await asyncio.to_thread(_public_url_sync, url)
