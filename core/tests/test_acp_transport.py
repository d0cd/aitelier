"""Direct unit tests for providers/acp_transport.py helpers.

`AcpClient` + `AcpError` + `_warn_remote_misconfig` are tested in
`test_sandbox_agent.py` via the re-export pattern. This file covers
the smaller helpers that no other test reaches:
  - `_is_local_url`
  - `_scrub_sandbox_url`
  - `_persist_sandbox_server_id`
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aitelier.providers.acp_transport import (
    _is_local_url,
    _persist_sandbox_server_id,
    _scrub_sandbox_url,
)


# --- _is_local_url ----------------------------------------------------------


def test_is_local_url_recognizes_loopback_variants():
    """Both IPv4 loopback addresses and the 'localhost' label resolve to
    local. IPv6 loopback `::1` is recognized too — it's the SA on Docker
    Desktop's bound address when IPv6 is enabled."""
    for url in (
        "http://localhost:2468",
        "http://127.0.0.1:2468",
        "http://0.0.0.0:2468",
        "http://[::1]:2468",
        "https://localhost/sandbox-agent",
    ):
        assert _is_local_url(url) is True, f"{url!r} should be local"


def test_is_local_url_rejects_remote_hosts():
    """Brig ingress on the host appears as 127.0.0.1; cells behind a
    reverse proxy are remote. Anything not in the loopback set is remote."""
    for url in (
        "http://sandbox.example.com",
        "http://10.0.0.1:2468",
        "https://api.anthropic.com",
        "http://192.168.1.100:8443",
    ):
        assert _is_local_url(url) is False, f"{url!r} should be remote"


def test_is_local_url_tolerates_malformed_input():
    """A bad URL shouldn't raise; treat as non-local so we err on the
    side of preflight warnings rather than silent acceptance."""
    assert _is_local_url("not a url") is False
    assert _is_local_url("") is False


# --- _scrub_sandbox_url -----------------------------------------------------


def test_scrub_sandbox_url_replaces_base_url_with_placeholder():
    """Internal SA topology must not leak through error envelopes —
    replace literal occurrences with `<sandbox>` so consumer dashboards
    don't display container paths or brig ingress URLs."""
    msg = "Server error '502 Bad Gateway' for url 'http://127.0.0.1:8443/sandbox-agent/v1/acp/abc'"
    scrubbed = _scrub_sandbox_url(msg, "http://127.0.0.1:8443/sandbox-agent")
    assert "http://127.0.0.1:8443/sandbox-agent" not in scrubbed
    assert "<sandbox>/v1/acp/abc" in scrubbed


def test_scrub_sandbox_url_noop_when_base_url_missing():
    """SA wasn't configured — nothing to scrub. Caller may still pass an
    arbitrary error message; return it unchanged."""
    msg = "Some error mentioning http://example.com"
    assert _scrub_sandbox_url(msg, None) == msg
    assert _scrub_sandbox_url(msg, "") == msg


def test_scrub_sandbox_url_only_replaces_exact_base_url():
    """The function uses straight string replacement, so a base URL that
    happens to be a substring of another won't accidentally over-redact.
    Verify that nothing else changes when no match is present."""
    msg = "GET http://other.example.com failed"
    scrubbed = _scrub_sandbox_url(msg, "http://127.0.0.1:2468")
    assert scrubbed == msg


# --- _persist_sandbox_server_id --------------------------------------------


@pytest.mark.asyncio
async def test_persist_sandbox_server_id_writes_through_to_store():
    """The function stamps the run row with sandbox_url, server_id, and a
    derived `sandbox_backend = "local"` for loopback / `"remote"` otherwise.
    A future restart-recovery pass uses these to find the session and
    dashboards distinguish local vs remote runs."""
    mock_store = AsyncMock()

    async def fake_get_store():
        return mock_store

    with patch("aitelier.storage.get_store", new=fake_get_store):
        await _persist_sandbox_server_id(
            run_id="r-1",
            sandbox_url="http://127.0.0.1:2468",
            server_id="srv-abc",
        )

    mock_store.update_run_sandbox.assert_awaited_once_with(
        "r-1",
        sandbox_url="http://127.0.0.1:2468",
        sandbox_server_id="srv-abc",
        sandbox_backend="local",
    )


@pytest.mark.asyncio
async def test_persist_sandbox_server_id_classifies_remote_correctly():
    """A non-loopback base URL → `sandbox_backend = "remote"`. Without
    this dashboards can't filter on hosted vs local-dev runs."""
    mock_store = AsyncMock()

    async def fake_get_store():
        return mock_store

    with patch("aitelier.storage.get_store", new=fake_get_store):
        await _persist_sandbox_server_id(
            run_id="r-2",
            sandbox_url="https://sandbox.hosted.example/v1",
            server_id="srv-xyz",
        )

    call = mock_store.update_run_sandbox.await_args
    assert call.kwargs["sandbox_backend"] == "remote"


@pytest.mark.asyncio
async def test_persist_sandbox_server_id_silent_no_run_id():
    """Called from the call_via_sandbox path even when run_id is unset
    (e.g., a direct call from an out-of-band caller). No store hit, no
    raise — the function returns early."""
    mock_store = AsyncMock()

    async def fake_get_store():
        return mock_store

    with patch("aitelier.storage.get_store", new=fake_get_store):
        await _persist_sandbox_server_id(
            run_id="",
            sandbox_url="http://127.0.0.1:2468",
            server_id="srv-abc",
        )

    mock_store.update_run_sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_persist_sandbox_server_id_swallows_store_errors():
    """Best-effort: the run is more important than the metadata stamp.
    Storage hiccups must not bubble up and abort an in-flight agent run."""
    async def boom():
        raise RuntimeError("store unavailable")

    with patch("aitelier.storage.get_store", new=boom):
        # Must not raise.
        await _persist_sandbox_server_id(
            run_id="r-3",
            sandbox_url="http://127.0.0.1:2468",
            server_id="srv-abc",
        )
