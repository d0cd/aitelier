"""Tests for the Idempotency-Key handling on the agent path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aitelier.server import app
from aitelier.storage import IdempotencyRecord, get_store
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def _stub_agent(monkeypatch, side_effect=None, returns=None):
    """Patch the agent execution path. Tracks calls so we can assert how
    many times the underlying work actually ran."""
    calls = {"count": 0}

    async def fake_call(name, prompt, **kw):
        calls["count"] += 1
        if side_effect:
            await side_effect()
        return returns or {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kw.get("run_id", "r"),
            "content": f"reply-{calls['count']}",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "finish_reason": "completed", "tool_calls": [], "cost_usd": None,
            "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr("aitelier.providers.sandbox_agent.call_via_sandbox",
                         fake_call)
    return calls


def _chat_body(content: str = "hi") -> dict:
    return {
        "model": "agent:claude/claude-sonnet-4-5",
        "messages": [{"role": "user", "content": content}],
    }


def test_repeat_with_same_key_returns_cached_response(client, monkeypatch):
    calls = _stub_agent(monkeypatch)
    headers = {"Idempotency-Key": "key-abc"}
    body = _chat_body()

    r1 = client.post("/v1/chat/completions", headers=headers, json=body)
    r2 = client.post("/v1/chat/completions", headers=headers, json=body)
    r3 = client.post("/v1/chat/completions", headers=headers, json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    # The agent ran exactly once; the next two requests replayed the cache.
    assert calls["count"] == 1
    # All three responses surface the same content (cached).
    assert (
        r1.json()["choices"][0]["message"]["content"]
        == r2.json()["choices"][0]["message"]["content"]
        == r3.json()["choices"][0]["message"]["content"]
    )


def test_error_response_is_not_cached_under_key(client, monkeypatch):
    """A transient agent error must NOT be cached — a retry under the same
    Idempotency-Key has to re-run the agent, not replay the stale error."""
    error_envelope = {
        "kind": "agent", "provider": "claude", "status": "error",
        "duration_s": 0.0, "run_id": "r",
        "content": "", "usage": None, "finish_reason": "error",
        "tool_calls": [], "cost_usd": None,
        "error_type": "ProviderUnavailable", "error_msg": "503 service unavailable",
    }
    calls = _stub_agent(monkeypatch, returns=error_envelope)
    headers = {"Idempotency-Key": "key-err"}
    body = _chat_body()

    r1 = client.post("/v1/chat/completions", headers=headers, json=body)
    r2 = client.post("/v1/chat/completions", headers=headers, json=body)

    assert r1.status_code >= 400
    assert r2.status_code >= 400
    # The error was not cached: the agent ran again on retry.
    assert calls["count"] == 2


def test_same_key_different_body_returns_422(client, monkeypatch):
    _stub_agent(monkeypatch)
    headers = {"Idempotency-Key": "key-collision"}

    r1 = client.post(
        "/v1/chat/completions", headers=headers, json=_chat_body("first"),
    )
    r2 = client.post(
        "/v1/chat/completions", headers=headers, json=_chat_body("DIFFERENT"),
    )

    assert r1.status_code == 200
    assert r2.status_code == 422
    assert "Idempotency-Key" in r2.json()["detail"]


def test_no_key_means_no_dedup(client, monkeypatch):
    """Without the header, each POST creates a fresh run."""
    calls = _stub_agent(monkeypatch)
    body = _chat_body()

    client.post("/v1/chat/completions", json=body).raise_for_status()
    client.post("/v1/chat/completions", json=body).raise_for_status()
    assert calls["count"] == 2


def test_malformed_idempotency_key_is_rejected(client, monkeypatch):
    """Idempotency-Key must be 1-200 chars of [A-Za-z0-9._:-].
    Anything else is a 400 at the boundary — keeps the
    `idempotency_keys` table from accepting megabyte rows or control
    chars that would propagate into echoed error messages."""
    _stub_agent(monkeypatch)
    body = _chat_body()

    for bad_key in [
        "x" * 201,                         # over length cap
        "has spaces in it",                # space not allowed
        "ctrl-\x01-chars",                 # control byte
        "newline\nattack",                 # newline → log injection vector
    ]:
        resp = client.post(
            "/v1/chat/completions", json=body,
            headers={"Idempotency-Key": bad_key},
        )
        assert resp.status_code in (400, 422), (
            f"bad key {bad_key!r} should reject, got {resp.status_code}"
        )


def test_correlation_id_charset_enforced(client, monkeypatch):
    """An X-Correlation-Id with disallowed characters is replaced with
    a fresh UUID — we never echo control characters or newlines back
    into log lines or response headers."""
    _stub_agent(monkeypatch)
    body = _chat_body()
    resp = client.post(
        "/v1/chat/completions", json=body,
        headers={"X-Correlation-Id": "ok-id_1.2:3-4"},
    )
    # Valid charset: echoed back.
    assert resp.headers["X-Correlation-Id"] == "ok-id_1.2:3-4"

    resp = client.post(
        "/v1/chat/completions", json=body,
        headers={"X-Correlation-Id": "bad id with spaces"},
    )
    echoed = resp.headers["X-Correlation-Id"]
    assert echoed != "bad id with spaces"
    # Replacement is a UUID
    assert len(echoed) >= 32


@pytest.mark.asyncio
async def test_expired_record_is_not_served(client, monkeypatch):
    """A cache entry past its expires_at is ignored — fresh execution."""
    calls = _stub_agent(monkeypatch)
    store = await get_store()
    # Pre-seed an expired entry under the key we're about to send.
    expired = IdempotencyRecord(
        key="key-expired", body_hash="ignored",
        endpoint="/v1/chat/completions", status_code=200,
        response={"stale": True},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await store.record_idempotent(expired)

    r = client.post(
        "/v1/chat/completions",
        headers={"Idempotency-Key": "key-expired"},
        json=_chat_body("fresh"),
    )
    assert r.status_code == 200
    # Real handler ran (cache miss due to expiry).
    assert calls["count"] == 1
    assert "stale" not in r.json()


def test_idempotent_replay_for_retry_scenario(client, monkeypatch):
    """Concrete consumer flow: SDK retry on a transient failure. The retry
    with the same key gets the cached response — the entire workflow
    (prepare + agent) is short-circuited, so any side effects in prepare
    happen exactly once."""
    calls = _stub_agent(monkeypatch)
    prepare_calls = {"count": 0}

    async def stub_sa_proxy(method, path, **kw):
        if path == "/v1/processes/run":
            prepare_calls["count"] += 1
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return {}

    monkeypatch.setattr("aitelier.sandbox_proxy.sa_proxy", stub_sa_proxy)

    headers = {"Idempotency-Key": "retry-uuid-1"}
    body = {
        "model": "agent:claude/claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "do work"}],
        "aitelier": {
            "prepare": {"commands": [{"cmd": "echo", "args": ["x"]}]},
        },
    }

    r1 = client.post("/v1/chat/completions", headers=headers, json=body)
    r2 = client.post("/v1/chat/completions", headers=headers, json=body)
    assert r1.status_code == 200 and r2.status_code == 200

    # Critical assertions: each side effect ran exactly once.
    assert calls["count"] == 1, "agent re-ran on idempotent retry"
    assert prepare_calls["count"] == 1, "prepare.commands re-ran on idempotent retry"
    assert r1.json() == r2.json()
