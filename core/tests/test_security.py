"""Tests for aitelier.security helpers and the API-layer guards that use them."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aitelier.security import is_public_url
from aitelier.server import app
from fastapi.testclient import TestClient

# --- is_public_url ---------------------------------------------------------


def _resolves_to(addr: str):
    """Patch socket.getaddrinfo to return one IPv4 result."""
    return patch(
        "aitelier.security.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", (addr, 0))],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.0.0.5", "0.0.0.0",
    "10.0.0.1", "172.16.0.1", "192.168.1.1",
    "169.254.169.254",  # AWS/GCP/Azure metadata service — classic SSRF target
    "224.0.0.1",        # multicast
])
async def test_is_public_url_rejects_private_ranges(addr):
    with _resolves_to(addr):
        assert await is_public_url("http://example.invalid/") is False


@pytest.mark.asyncio
async def test_is_public_url_accepts_external_address():
    with _resolves_to("93.184.216.34"):   # example.com
        assert await is_public_url("https://example.com/hook") is True


@pytest.mark.asyncio
async def test_is_public_url_rejects_non_http_schemes():
    # No DNS lookup needed — scheme alone is enough.
    assert await is_public_url("file:///etc/passwd") is False
    assert await is_public_url("gopher://evil/") is False


@pytest.mark.asyncio
async def test_is_public_url_rejects_unresolvable_host():
    import socket as _sock
    with patch("aitelier.security.socket.getaddrinfo",
                side_effect=_sock.gaierror):
        assert await is_public_url("https://nope.invalid/") is False


# --- API guards ------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def hosted_mode():
    """Enable hosted-mode auth so the SSRF guard activates. Returns the
    Bearer header dict so test calls can authenticate."""
    from aitelier.config import get_config
    cfg = get_config()
    cfg.service.api_key = "test-key"
    try:
        yield {"Authorization": "Bearer test-key"}
    finally:
        cfg.service.api_key = None


def test_async_agent_rejects_loopback_webhook_by_default(client, hosted_mode):
    """SSRF guard is on by default regardless of hosted/localhost-trust mode.
    A loopback webhook target is rejected unless `service.allow_loopback_webhooks`
    is set."""
    with _resolves_to("127.0.0.1"):
        resp = client.post("/v1/runs", headers=hosted_mode, json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "hi"}],
            "webhook_url": "http://localhost:9999/cb",
        })
    assert resp.status_code == 400
    assert "loopback" in resp.json()["detail"].lower()


def test_schedule_rejects_metadata_service_webhook(client, hosted_mode):
    """`169.254.169.254` (AWS IMDS) — the classic SSRF target. Reject
    independent of mode."""
    with _resolves_to("169.254.169.254"):
        resp = client.post("/v1/schedules", headers=hosted_mode, json={
            "name": "ssrf-attempt",
            "task": {
                "model": "claude-sonnet",
                "messages": [{"role": "user", "content": "x"}],
            },
            "interval_seconds": 60,
            "webhook_url": "http://meta.invalid/latest/meta-data/",
        })
    assert resp.status_code == 400
    assert "loopback" in resp.json()["detail"].lower()


def test_schedule_accepts_external_webhook(client, hosted_mode):
    with _resolves_to("93.184.216.34"):  # example.com
        resp = client.post("/v1/schedules", headers=hosted_mode, json={
            "name": "ok",
            "task": {
                "model": "claude-sonnet",
                "messages": [{"role": "user", "content": "x"}],
            },
            "interval_seconds": 60,
            "webhook_url": "https://example.com/hook",
        })
    assert resp.status_code == 200


def test_allow_loopback_webhooks_opt_in_permits_localhost(client, monkeypatch):
    """The dev-mode escape hatch: set `service.allow_loopback_webhooks = true`
    and localhost webhook targets become accepted (SSRF guard disabled).
    Default of False keeps consumers from accidentally pointing at IMDS."""
    from aitelier.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.service, "allow_loopback_webhooks", True)
    resp = client.post("/v1/schedules", json={
        "name": "local-dev",
        "task": {
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "x"}],
        },
        "interval_seconds": 60,
        "webhook_url": "http://localhost:9999/cb",
    })
    assert resp.status_code == 200


# --- Bearer auth: timing-safe + still authoritative ------------------------


def test_bearer_compare_is_timing_safe(client):
    """Wrong key still rejects; correct key passes. Doesn't prove timing-safety
    directly (that requires statistical testing), but documents the API contract
    and exercises the hmac.compare_digest path. Use /v1/runs/active rather than
    /v1/discovery so we don't spin up the shared httpx client and pollute the
    pytest-asyncio loop for subsequent tests."""
    from aitelier.config import get_config
    cfg = get_config()
    cfg.service.api_key = "correct-key"
    try:
        bad = client.get("/v1/runs/active",
                         headers={"Authorization": "Bearer almost-correct"})
        good = client.get("/v1/runs/active",
                          headers={"Authorization": "Bearer correct-key"})
        assert bad.status_code == 401
        assert good.status_code == 200
    finally:
        cfg.service.api_key = None
