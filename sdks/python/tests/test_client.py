"""Tests for the SDK client — verify each method calls the right URL with
the right body/headers. Server responses are mocked at the httpx layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aitelier_client import Aitelier


def _ok_result_dict():
    return {
        "kind": "complete", "provider": "claude-sonnet", "status": "ok",
        "duration_s": 0.1, "run_id": "r1", "trace_id": "r1",
        "content": "hello", "parsed": None,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "finish_reason": "stop", "cost_usd": 0.0,
        "error_type": None, "error_msg": None,
    }


def _stub_http(client: Aitelier, fake_resp_dict, *, status_code=200):
    """Replace the client's internal httpx client with a mock returning fake_resp_dict."""
    fake = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value=fake_resp_dict)
    fake.post = AsyncMock(return_value=fake_resp)
    fake.get = AsyncMock(return_value=fake_resp)
    client._client = fake
    return fake


@pytest.mark.asyncio
async def test_complete_sends_correlation_id_header():
    sdk = Aitelier()
    fake = _stub_http(sdk, _ok_result_dict())
    await sdk.complete(model="claude-sonnet",
                       messages=[{"role": "user", "content": "hi"}],
                       correlation_id="cid-abc")
    args, kwargs = fake.post.call_args
    assert args[0] == "/v1/complete"
    assert kwargs["headers"] == {"X-Correlation-Id": "cid-abc"}


@pytest.mark.asyncio
async def test_default_correlation_id_applies_when_unset():
    sdk = Aitelier(default_correlation_id="default-cid")
    fake = _stub_http(sdk, _ok_result_dict())
    await sdk.complete(model="claude-sonnet",
                       messages=[{"role": "user", "content": "hi"}])
    _, kwargs = fake.post.call_args
    assert kwargs["headers"] == {"X-Correlation-Id": "default-cid"}


@pytest.mark.asyncio
async def test_per_call_cid_overrides_default():
    sdk = Aitelier(default_correlation_id="default-cid")
    fake = _stub_http(sdk, _ok_result_dict())
    await sdk.complete(model="x", messages=[],
                       correlation_id="explicit-cid")
    _, kwargs = fake.post.call_args
    assert kwargs["headers"] == {"X-Correlation-Id": "explicit-cid"}


@pytest.mark.asyncio
async def test_cancel_run_hits_post_cancel_endpoint():
    sdk = Aitelier()
    fake = _stub_http(sdk, {"run_id": "abc", "cancelled": True})
    result = await sdk.cancel_run("abc")
    assert result.run_id == "abc"
    assert result.cancelled is True
    args, _ = fake.post.call_args
    assert args[0] == "/v1/runs/abc/cancel"


@pytest.mark.asyncio
async def test_list_active_runs():
    sdk = Aitelier()
    fake = _stub_http(sdk, {"active": ["r1", "r2"]})
    result = await sdk.list_active_runs()
    assert result.active == ["r1", "r2"]
    args, _ = fake.get.call_args
    assert args[0] == "/v1/runs/active"


@pytest.mark.asyncio
async def test_discovery():
    sdk = Aitelier()
    fake_disc = {
        "service": "aitelier", "version": "0.1.0", "api_version": "v1",
        "timestamp": "2026-05-12T00:00:00Z",
        "endpoints": [],
        "capabilities": {},
        "dependencies": {
            "litellm": {"reachable": True, "base_url": "http://localhost:4000"},
            "sandbox_agent": {"reachable": True, "base_url": "http://localhost:2468"},
        },
        "schemas": {},
        "known_limitations": [],
    }
    fake = _stub_http(sdk, fake_disc)
    result = await sdk.discovery()
    assert result.service == "aitelier"
    assert result.dependencies.litellm.reachable is True
    args, _ = fake.get.call_args
    assert args[0] == "/v1/discovery"


@pytest.mark.asyncio
async def test_get_schema():
    sdk = Aitelier()
    fake_schema = {"$schema": "...", "type": "object"}
    fake = _stub_http(sdk, fake_schema)
    result = await sdk.get_schema("task")
    assert result["type"] == "object"
    args, _ = fake.get.call_args
    assert args[0] == "/v1/schemas/task"
