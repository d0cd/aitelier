"""Tests for the HTTP service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aitelier.server import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Discovery has a TTL cache; clear it between tests so mocks aren't stale."""
    from aitelier.server import _discovery_cache
    _discovery_cache["value"] = None
    _discovery_cache["at"] = 0.0
    yield
    _discovery_cache["value"] = None
    _discovery_cache["at"] = 0.0


def test_health(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert "known_limitations" in data
    assert isinstance(data["known_limitations"], list)


def test_complete(client):
    mock_result = {
        "kind": "complete",
        "provider": "claude-sonnet",
        "status": "ok",
        "duration_s": 0.5,
        "run_id": "",
        "trace_id": "",
        "content": "Hello!",
        "parsed": None,
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        "finish_reason": "stop",
        "cost_usd": 0.001,
        "error_type": None,
        "error_msg": None,
    }

    with patch("aitelier.providers.llm.complete", new_callable=AsyncMock, return_value=mock_result):
        resp = client.post("/v1/complete", json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "Hi"}],
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["content"] == "Hello!"


def test_embed(client):
    mock_result = {
        "kind": "embed",
        "provider": "nomic-embed-text",
        "status": "ok",
        "duration_s": 0.2,
        "run_id": "",
        "trace_id": "",
        "embeddings": [[0.1, 0.2, 0.3]],
        "dimensions": 3,
        "content": None,
        "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
        "finish_reason": "stop",
        "cost_usd": None,
        "error_type": None,
        "error_msg": None,
    }

    with patch("aitelier.providers.llm.embed", new_callable=AsyncMock, return_value=mock_result):
        resp = client.post("/v1/embed", json={
            "texts": ["hello world"],
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["dimensions"] == 3
    assert len(data["embeddings"]) == 1


def test_execute_stream(client):
    mock_result = {
        "kind": "complete",
        "provider": "claude-sonnet",
        "status": "ok",
        "duration_s": 1.0,
        "run_id": "test-run",
        "trace_id": "test-run",
        "content": "Streamed",
        "parsed": None,
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        "finish_reason": "stop",
        "cost_usd": 0.001,
        "error_type": None,
        "error_msg": None,
    }

    with (
        patch("aitelier.runner.complete", new_callable=AsyncMock, return_value=mock_result),
        patch("aitelier.runner.record_trace"),
    ):
        resp = client.post("/v1/execute/stream", json={
            "name": "test",
            "kind": "complete",
            "prompt": "Hello",
        })

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "run.started" in body
    assert "run.completed" in body


def test_run_not_found(client):
    resp = client.get("/v1/runs/nonexistent")
    assert resp.status_code == 404


def test_path_traversal_in_run_id(client):
    resp = client.get("/v1/runs/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)  # rejected or not found


def test_path_traversal_dotdot(client):
    resp = client.get("/v1/runs/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)  # either rejected or not found


def test_run_id_with_slashes(client):
    resp = client.get("/v1/runs/foo/bar")
    # FastAPI routes this differently, but direct slash in run_id should fail
    assert resp.status_code in (400, 404, 422)


# --- Discovery ---


def _ok_litellm():
    return {"reachable": True, "base_url": "http://localhost:4000",
            "models": ["claude-sonnet", "nomic-embed-text"]}


def _ok_sandbox():
    return {"reachable": True, "base_url": "http://localhost:2468",
            "agents": ["claude-code", "codex"]}


def test_discovery_shape(client):
    with (
        patch("aitelier.server._probe_litellm", new_callable=AsyncMock,
              return_value=_ok_litellm()),
        patch("aitelier.server._probe_sandbox_agent", new_callable=AsyncMock,
              return_value=_ok_sandbox()),
        patch("aitelier.server._probe_traces", return_value={"available": True}),
    ):
        resp = client.get("/v1/discovery")
    assert resp.status_code == 200
    data = resp.json()

    assert data["service"] == "aitelier"
    assert data["api_version"] == "v1"
    assert data["version"]
    assert "timestamp" in data
    assert "known_limitations" in data and isinstance(data["known_limitations"], list)

    paths = {(e["method"], e["path"]) for e in data["endpoints"]}
    assert ("POST", "/v1/complete") in paths
    assert ("POST", "/v1/embed") in paths
    assert ("POST", "/v1/agent") in paths
    assert ("GET", "/v1/traces") in paths
    assert ("GET", "/v1/health") in paths
    assert ("GET", "/v1/discovery") in paths

    caps = data["capabilities"]
    for name in ("complete", "embed", "agent", "traces"):
        assert name in caps
        assert "available" in caps[name]
    assert caps["agent"]["available"] is True  # sandbox reachable → agent capability on

    assert data["dependencies"]["litellm"]["reachable"] is True
    assert data["dependencies"]["litellm"]["models"] == ["claude-sonnet", "nomic-embed-text"]
    assert data["dependencies"]["sandbox_agent"]["reachable"] is True
    assert data["dependencies"]["sandbox_agent"]["agents"] == ["claude-code", "codex"]

    assert isinstance(data["schemas"], dict)


def test_discovery_capabilities_when_litellm_down(client):
    with (
        patch("aitelier.server._probe_litellm", new_callable=AsyncMock, return_value={
            "reachable": False, "base_url": "http://localhost:4000",
            "reason": "ConnectError: connection refused",
        }),
        patch("aitelier.server._probe_sandbox_agent", new_callable=AsyncMock,
              return_value=_ok_sandbox()),
        patch("aitelier.server._probe_traces", return_value={"available": True}),
    ):
        resp = client.get("/v1/discovery")
    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["complete"]["available"] is False
    assert "reason" in caps["complete"]
    assert caps["embed"]["available"] is False


def test_discovery_capabilities_when_sandbox_down(client):
    with (
        patch("aitelier.server._probe_litellm", new_callable=AsyncMock,
              return_value=_ok_litellm()),
        patch("aitelier.server._probe_sandbox_agent", new_callable=AsyncMock, return_value={
            "reachable": False, "base_url": "http://localhost:2468",
            "reason": "ConnectError: connection refused",
        }),
        patch("aitelier.server._probe_traces", return_value={"available": True}),
    ):
        resp = client.get("/v1/discovery")
    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["agent"]["available"] is False
    assert "Sandbox Agent" in caps["agent"]["reason"]


def test_discovery_caches_within_ttl(client):
    """Two rapid requests should share probe calls (the cache TTL is 5s)."""
    litellm_mock = AsyncMock(return_value=_ok_litellm())
    sandbox_mock = AsyncMock(return_value=_ok_sandbox())
    with (
        patch("aitelier.server._probe_litellm", litellm_mock),
        patch("aitelier.server._probe_sandbox_agent", sandbox_mock),
        patch("aitelier.server._probe_traces", return_value={"available": True}),
    ):
        r1 = client.get("/v1/discovery")
        r2 = client.get("/v1/discovery")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Probes should each have been called exactly once
    assert litellm_mock.await_count == 1
    assert sandbox_mock.await_count == 1


def test_discovery_capabilities_when_traces_broken(client):
    with (
        patch("aitelier.server._probe_litellm", new_callable=AsyncMock,
              return_value=_ok_litellm()),
        patch("aitelier.server._probe_sandbox_agent", new_callable=AsyncMock,
              return_value=_ok_sandbox()),
        patch("aitelier.server._probe_traces", return_value={
            "available": False, "reason": "OperationalError: no such table",
        }),
    ):
        resp = client.get("/v1/discovery")
    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["traces"]["available"] is False
    assert "reason" in caps["traces"]


# --- Schemas ---


def test_schema_get(client):
    resp = client.get("/v1/schemas/task")
    assert resp.status_code == 200
    # Should be valid JSON schema content
    data = resp.json()
    assert isinstance(data, dict)


def test_schema_not_found(client):
    resp = client.get("/v1/schemas/nonexistent")
    assert resp.status_code == 404


def test_schema_path_traversal(client):
    resp = client.get("/v1/schemas/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


# --- Correlation IDs ---


def _complete_mock(**overrides):
    base = {
        "kind": "complete", "provider": "claude-sonnet", "status": "ok",
        "duration_s": 0.1, "run_id": "r1", "trace_id": "r1",
        "content": "ok", "parsed": None,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "finish_reason": "stop", "cost_usd": 0.0,
        "error_type": None, "error_msg": None,
    }
    base.update(overrides)
    return base


def test_correlation_id_echoed(client):
    with patch("aitelier.providers.llm.complete", new_callable=AsyncMock,
               return_value=_complete_mock()):
        resp = client.post("/v1/complete",
                           json={"model": "claude-sonnet",
                                 "messages": [{"role": "user", "content": "hi"}]},
                           headers={"X-Correlation-Id": "abc-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Correlation-Id"] == "abc-123"
    assert resp.json()["correlation_id"] == "abc-123"


def test_correlation_id_generated_when_absent(client):
    with patch("aitelier.providers.llm.complete", new_callable=AsyncMock,
               return_value=_complete_mock()):
        resp = client.post(
            "/v1/complete",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    cid = resp.headers.get("X-Correlation-Id")
    assert cid and len(cid) >= 8
    assert resp.json()["correlation_id"] == cid


def test_correlation_id_persisted_in_trace_metadata(client):
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    with (
        patch("aitelier.runner.complete", new_callable=AsyncMock,
              return_value=_complete_mock()),
        patch("aitelier.runner.record_trace", side_effect=fake_record),
    ):
        resp = client.post("/v1/execute",
                           json={"name": "t", "kind": "complete", "prompt": "hi"},
                           headers={"X-Correlation-Id": "trace-abc"})

    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == "trace-abc"
    meta = captured.get("metadata") or {}
    assert meta.get("correlation_id") == "trace-abc"


# --- Streaming /v1/complete/stream ---


def test_complete_stream_yields_deltas_then_done(client):
    async def fake_stream(**kwargs):
        yield {"type": "delta", "content": "Hello"}
        yield {"type": "delta", "content": " world"}
        yield {
            "type": "done",
            "content": "Hello world",
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            "finish_reason": "stop",
            "trace_id": "",
            "run_id": "",
            "cost_usd": None,
        }

    with patch("aitelier.providers.llm.complete_stream", fake_stream):
        resp = client.post(
            "/v1/complete/stream",
            json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Correlation-Id": "cs-1"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: complete.delta" in body
    assert "Hello" in body
    assert "event: complete.done" in body
    assert "cs-1" in body  # correlation_id present


def test_complete_stream_error_event_on_failure(client):
    async def fake_stream(**kwargs):
        if False:  # pragma: no cover
            yield {}
        raise RuntimeError("boom")

    with patch("aitelier.providers.llm.complete_stream", fake_stream):
        resp = client.post(
            "/v1/complete/stream",
            json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 200
    body = resp.text
    assert "event: complete.error" in body
    assert "RuntimeError" in body
    assert "boom" in body


# --- Trace recording for /v1/complete, /v1/embed, /v1/complete/stream ---


def test_complete_records_trace(client):
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    with (
        patch("aitelier.providers.llm.complete", new_callable=AsyncMock,
              return_value=_complete_mock()),
        patch("aitelier.server.record_trace", side_effect=fake_record),
    ):
        resp = client.post("/v1/complete",
                           json={"model": "claude-sonnet",
                                 "messages": [{"role": "user", "content": "hi"}]},
                           headers={"X-Correlation-Id": "trace-cmpl"})
    assert resp.status_code == 200
    assert captured.get("trace_id")
    md = captured.get("metadata") or {}
    assert md.get("correlation_id") == "trace-cmpl"


def test_embed_records_trace(client):
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    mock_result = {
        "kind": "embed", "provider": "nomic-embed-text", "status": "ok",
        "duration_s": 0.2, "run_id": "", "trace_id": "",
        "embeddings": [[0.1]], "dimensions": 1, "content": None,
        "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
        "finish_reason": "stop", "cost_usd": None,
        "error_type": None, "error_msg": None,
    }
    with (
        patch("aitelier.providers.llm.embed", new_callable=AsyncMock,
              return_value=mock_result),
        patch("aitelier.server.record_trace", side_effect=fake_record),
    ):
        resp = client.post("/v1/embed",
                           json={"texts": ["hi"]},
                           headers={"X-Correlation-Id": "trace-emb"})
    assert resp.status_code == 200
    assert captured.get("trace_id")


def test_complete_stream_records_trace_at_done(client):
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    async def fake_stream(**kwargs):
        yield {"type": "delta", "content": "ok"}
        yield {
            "type": "done", "content": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "finish_reason": "stop", "cost_usd": None, "trace_id": "", "run_id": "",
        }

    with (
        patch("aitelier.providers.llm.complete_stream", fake_stream),
        patch("aitelier.server.record_trace", side_effect=fake_record),
    ):
        resp = client.post("/v1/complete/stream",
                           json={"model": "claude-sonnet",
                                 "messages": [{"role": "user", "content": "hi"}]},
                           headers={"X-Correlation-Id": "trace-strm"})
    assert resp.status_code == 200
    # Body fully read by TestClient before returning; record_trace ran
    assert captured.get("trace_id")


# --- Cancellation ---


def test_runs_active_empty(client):
    from aitelier.server import _active_runs
    # ensure clean
    _active_runs.clear()
    resp = client.get("/v1/runs/active")
    assert resp.status_code == 200
    assert resp.json() == {"active": []}


def test_runs_active_lists_in_flight(client):
    from unittest.mock import MagicMock

    from aitelier.server import _active_runs
    _active_runs["fake-run-1"] = MagicMock()
    try:
        resp = client.get("/v1/runs/active")
        assert resp.status_code == 200
        assert "fake-run-1" in resp.json()["active"]
    finally:
        _active_runs.pop("fake-run-1", None)


def test_cancel_unknown_returns_404(client):
    resp = client.post("/v1/runs/nonexistent/cancel")
    assert resp.status_code == 404


def test_cancel_running_task(client):
    from unittest.mock import MagicMock

    from aitelier.server import _active_runs
    fake = MagicMock()
    _active_runs["cancel-me"] = fake
    try:
        resp = client.post("/v1/runs/cancel-me/cancel")
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        assert resp.json()["run_id"] == "cancel-me"
        fake.cancel.assert_called_once()
    finally:
        _active_runs.pop("cancel-me", None)


def test_cancel_path_traversal_rejected(client):
    resp = client.post("/v1/runs/..%2F..%2Fetc/cancel")
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_lifespan_shutdown_cancels_active_runs():
    """When the server shuts down, in-flight runs should be cancelled."""
    import asyncio

    from aitelier.server import _active_runs, app, lifespan

    async def long_running():
        await asyncio.sleep(60)

    task = asyncio.create_task(long_running())
    _active_runs["shutdown-test"] = task
    try:
        # Drive the lifespan: enter, yield, exit
        async with lifespan(app):
            assert "shutdown-test" in _active_runs
        # After lifespan exit, the task should be cancelled
        assert task.cancelled() or task.done()
    finally:
        _active_runs.pop("shutdown-test", None)
        if not task.done():
            task.cancel()


def test_correlation_id_propagates_to_log_records(client, caplog):
    """Logging inside a request should pick up correlation_id from contextvar."""
    import logging

    from aitelier.server import logger

    with patch("aitelier.providers.llm.complete", new_callable=AsyncMock,
               return_value=_complete_mock()):
        with caplog.at_level(logging.INFO, logger="aitelier"):
            # Hook a side effect to emit a log line during the request
            async def emit_and_complete(**kwargs):
                logger.info("inside-request")
                return _complete_mock()

            with patch("aitelier.providers.llm.complete",
                       new_callable=AsyncMock, side_effect=emit_and_complete):
                resp = client.post(
                    "/v1/complete",
                    json={"model": "claude-sonnet",
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"X-Correlation-Id": "log-cid-1"},
                )
    assert resp.status_code == 200
    # The "inside-request" log line should carry correlation_id="log-cid-1"
    matched = [r for r in caplog.records if r.getMessage() == "inside-request"]
    assert matched, "expected a log line emitted during the request"
    assert getattr(matched[0], "correlation_id", None) == "log-cid-1"


def test_correlation_id_in_sse_events(client):
    with (
        patch("aitelier.runner.complete", new_callable=AsyncMock,
              return_value=_complete_mock()),
        patch("aitelier.runner.record_trace"),
    ):
        resp = client.post("/v1/execute/stream",
                           json={"name": "t", "kind": "complete", "prompt": "hi"},
                           headers={"X-Correlation-Id": "sse-cid"})
    assert resp.status_code == 200
    body = resp.text
    # Every event payload should carry correlation_id
    assert '"correlation_id": "sse-cid"' in body or '"correlation_id":"sse-cid"' in body
