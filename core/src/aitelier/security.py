"""Security helpers — small, pure functions, no external deps."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path
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


def _has_symlinked_component(path: Path) -> bool:
    """True if any component of `path` (including itself) is a symlink.

    Walks the path top-down with `O_NOFOLLOW` semantics. Mirrors the
    shape of brig.workspace.validation.safe_open but only reports; the
    caller decides how to react. Returns False for paths that don't
    yet exist — the consumer can dispatch against a workspace that the
    agent will populate, and we only veto when the *current* state of
    the FS shows a symlink-bearing prefix."""
    parts = path.parts
    cursor = Path(parts[0]) if parts else Path()
    for part in parts[1:]:
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                return True
        except OSError:
            return False
    return False


def validate_workspace_path(
    value: str | None, *,
    roots: list[str] | None,
    label: str = "workspace",
) -> None:
    """Refuse symlink-traversal and out-of-root paths at the aitelier
    boundary.

    Three layered defenses for agent-path `aitelier.workspace`,
    `aitelier.artifacts.fetch[*]`, and `aitelier.prepare.files[*].path`:

      1. `..` ban — refuses relative traversal regardless of resolved
         location.
      2. Symlink check — if any component of the path is a symlink, the
         path is refused. brig's mount-side `nosymfollow` will eventually
         close this at the kernel level; until then, this is the consumer-
         side defense that brig's safe_open API expects.
      3. Allowlist — when `roots` is set (`service.allowed_workspace_roots`),
         the resolved path must be a descendant of one of them. Empty list
         = no allowlist (current behavior).

    `value` is None or empty → no-op (the field is optional).
    Aitelier never reads the file itself; the agent's tool calls do.
    These checks bound *what aitelier will hand to SA*, not what the
    agent's `Read` tool can later traverse — that remains an SA-side
    fix (track upstream).
    """
    if not value:
        return
    if ".." in value.split(os.sep):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: '..' path components not allowed",
        )
    path = Path(value)
    if path.is_absolute() and _has_symlinked_component(path):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid {label}: path contains a symlinked component. "
                "Aitelier refuses symlinked workspace paths to bound the "
                "agent's filesystem access to the declared root."
            ),
        )
    if roots:
        resolved = path.resolve()
        if not any(
            _is_descendant(resolved, Path(root).resolve()) for root in roots
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid {label}: not under any "
                    f"`service.allowed_workspace_roots` entry"
                ),
            )


def _is_descendant(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


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
