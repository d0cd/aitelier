"""Tests for the HTTP service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


# --- Auth middleware (hosted-mode) ---


def test_auth_disabled_by_default(client):
    """When service.api_key is unset, /v1/health and others are public."""
    resp = client.get("/v1/health")
    assert resp.status_code == 200


def test_auth_health_always_public(client):
    """Even with api_key set, /v1/health is exempt for liveness probes."""
    from aitelier.config import get_config
    cfg = get_config()
    cfg.service.api_key = "secret-key"
    try:
        resp = client.get("/v1/health")
        assert resp.status_code == 200
    finally:
        cfg.service.api_key = None


def test_auth_blocks_without_bearer(client):
    from aitelier.config import get_config
    cfg = get_config()
    cfg.service.api_key = "secret-key"
    try:
        resp = client.get("/v1/discovery")
        assert resp.status_code == 401
    finally:
        cfg.service.api_key = None


def test_auth_allows_correct_bearer(client):
    from aitelier.config import get_config
    cfg = get_config()
    cfg.service.api_key = "secret-key"
    try:
        with (
            patch("aitelier.server._probe_litellm", new_callable=AsyncMock,
                  return_value=_ok_litellm()),
            patch("aitelier.server._probe_sandbox_agent", new_callable=AsyncMock,
                  return_value=_ok_sandbox()),
            patch("aitelier.server._probe_traces", return_value={"available": True}),
        ):
            resp = client.get("/v1/discovery",
                              headers={"Authorization": "Bearer secret-key"})
        assert resp.status_code == 200
    finally:
        cfg.service.api_key = None


def test_auth_rejects_wrong_bearer(client):
    from aitelier.config import get_config
    cfg = get_config()
    cfg.service.api_key = "secret-key"
    try:
        resp = client.get("/v1/discovery",
                          headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401
    finally:
        cfg.service.api_key = None


def test_health(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert "known_limitations" in data
    assert isinstance(data["known_limitations"], list)


def _openai_chat_response(content: str = "Hello!") -> dict:
    return {
        "id": "chatcmpl-upstream", "object": "chat.completion",
        "created": 1_700_000_000, "model": "claude-sonnet",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop", "logprobs": None,
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def test_chat_completions_llm_path(client):
    with patch("aitelier.server.chat_completion",
                new_callable=AsyncMock, return_value=_openai_chat_response()):
        resp = client.post("/v1/chat/completions", json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "Hi"}],
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "Hello!"
    assert data["aitelier_run_id"]
    assert data["correlation_id"]


def test_chat_completions_rejects_aitelier_opts_on_llm_path(client):
    resp = client.post("/v1/chat/completions", json={
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "Hi"}],
        "aitelier": {"workspace": "/tmp"},
    })
    assert resp.status_code == 400
    assert "aitelier" in resp.json()["detail"]


def test_chat_completions_rejects_tools_on_agent_path(client, monkeypatch):
    async def fake_call(name, prompt, **kw):
        return {"kind": "agent", "provider": name, "status": "ok"}
    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"type": "function", "function": {"name": "fake"}}],
    })
    assert resp.status_code == 400
    assert "tools" in resp.json()["detail"]


def test_embeddings_passthrough(client):
    upstream = {
        "object": "list",
        "data": [{"object": "embedding", "embedding": [0.1, 0.2, 0.3], "index": 0}],
        "model": "nomic-embed-text",
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }
    with patch("aitelier.server.embeddings",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/embeddings", json={
            "model": "nomic-embed-text", "input": ["hello world"],
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert data["aitelier_run_id"]


def test_models_endpoint(client):
    with patch("aitelier.server.list_models",
                new_callable=AsyncMock,
                return_value=[
                    {"id": "claude-sonnet", "object": "model",
                     "owned_by": "litellm",
                     "response_format": ["json_schema"]},
                    {"id": "local", "object": "model", "owned_by": "litellm",
                     "response_format": []},
                ]):
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert any(m["id"] == "claude-sonnet" for m in data["data"])


def test_filter_chat_capable_drops_non_chat_routes():
    """The LiteLLM catalog includes TTS, image, embedding, moderation,
    transcription, realtime voice, and web-search routes that can't
    drive a code agent. Filter them out of the inner-LLM picklist so
    consumers don't see options that would fail at first call."""
    from aitelier.server import _filter_chat_capable
    ids = [
        "claude-sonnet",
        "openai/gpt-4o",
        "openai/dall-e-3",
        "openai/tts-1",
        "openai/whisper-1",
        "openai/text-embedding-3-small",
        "openai/omni-moderation-latest",
        "openai/sora-2-pro",
        "openai/gpt-image-1",
        "openai/gpt-realtime",
        "openai/gpt-4o-realtime-preview",
        "openai/gpt-4o-mini-realtime-preview-2024-12-17",
        "openai/gpt-4o-search-preview",
        "openai/gpt-4o-audio-preview",
    ]
    kept = _filter_chat_capable(ids)
    assert "claude-sonnet" in kept
    assert "openai/gpt-4o" in kept
    assert "openai/dall-e-3" not in kept
    assert "openai/tts-1" not in kept
    assert "openai/whisper-1" not in kept
    assert "openai/text-embedding-3-small" not in kept
    assert "openai/omni-moderation-latest" not in kept
    assert "openai/sora-2-pro" not in kept
    assert "openai/gpt-image-1" not in kept
    assert "openai/gpt-realtime" not in kept
    assert "openai/gpt-4o-realtime-preview" not in kept
    assert "openai/gpt-4o-mini-realtime-preview-2024-12-17" not in kept
    assert "openai/gpt-4o-search-preview" not in kept
    assert "openai/gpt-4o-audio-preview" not in kept


def test_models_endpoint_declares_agent_request_caps(client):
    """Agent entries declare the stricter request-field gates so consumer
    pickers can pre-strip fields instead of waiting for a 400."""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value=[{
        "id": "claude", "installed": True,
        "capabilities": {"toolCalls": True, "reasoning": False},
    }])
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)

    async def fake_get_shared():
        return fake_client

    with patch("aitelier.server.list_models",
                new_callable=AsyncMock,
                return_value=[{"id": "claude-sonnet", "object": "model",
                                "owned_by": "litellm"}]), \
            patch("aitelier.providers.llm.get_shared_client", fake_get_shared):
        resp = client.get("/v1/models")
    data = resp.json()["data"]
    agent_entry = next(m for m in data if m["id"] == "agent:claude")
    caps = agent_entry["aitelier_request_caps"]
    assert caps["tools"] is False
    assert caps["tool_choice"] is False
    assert caps["n_gt_1"] is False
    assert caps["top_p"] is False
    assert caps["streaming"] is True
    assert caps["response_format"] == ["json_schema"]
    # The ACP backend cap block is preserved separately.
    assert agent_entry["aitelier_capabilities"]["toolCalls"] is True


def test_models_endpoint_logs_warning_when_sa_probe_fails(client, caplog):
    """A failed SA probe must surface as a WARN log so operators can
    diagnose zero-agent-row symptoms without enabling debug logging."""
    async def boom():
        raise RuntimeError("connection refused")
    with patch("aitelier.server.list_models",
                new_callable=AsyncMock,
                return_value=[{"id": "claude-sonnet", "object": "model",
                                "owned_by": "litellm",
                                "response_format": ["json_schema"]}]), \
            patch("aitelier.providers.llm.get_shared_client",
                  side_effect=boom):
        with caplog.at_level("WARNING", logger="aitelier"):
            resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert any(
        "agent model enumeration" in rec.message
        and "connection refused" in rec.message
        for rec in caplog.records
    )


def test_embeddings_endpoint_encodes_base64_when_upstream_returns_floats(client):
    """When the consumer asks for `encoding_format: "base64"`, aitelier
    must honor it even if the upstream route (Ollama via LiteLLM) ignores
    the field and returns floats. Otherwise OpenAI SDK v6 — which defaults
    to base64 — silently decodes a float array as base64 and yields a
    192-element vector of zeros."""
    import base64
    import struct
    upstream = {
        "object": "list",
        "data": [{"embedding": [0.1, -0.2, 0.3], "index": 0, "object": "embedding"}],
        "model": "nomic-embed-text",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    with patch("aitelier.server.embeddings",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/embeddings", json={
            "model": "nomic-embed-text",
            "input": "hi",
            "encoding_format": "base64",
        })
    assert resp.status_code == 200
    emb = resp.json()["data"][0]["embedding"]
    assert isinstance(emb, str), "base64 means a string, not a list"
    decoded = struct.unpack("<3f", base64.b64decode(emb))
    assert decoded[0] == pytest.approx(0.1, rel=1e-5)
    assert decoded[1] == pytest.approx(-0.2, rel=1e-5)
    assert decoded[2] == pytest.approx(0.3, rel=1e-5)


def test_embeddings_endpoint_noop_when_upstream_already_base64(client):
    """If the upstream (e.g. OpenAI) honored encoding_format and returned
    a base64 string, the post-processor must not double-encode it."""
    upstream = {
        "object": "list",
        "data": [{"embedding": "AAAAAA==", "index": 0, "object": "embedding"}],
        "model": "openai/text-embedding-3-small",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    with patch("aitelier.server.embeddings",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/embeddings", json={
            "model": "openai/text-embedding-3-small",
            "input": "hi", "encoding_format": "base64",
        })
    assert resp.json()["data"][0]["embedding"] == "AAAAAA=="


def test_embeddings_endpoint_preserves_float_when_format_omitted(client):
    """No `encoding_format` (or explicit `float`): keep the float list."""
    upstream = {
        "object": "list",
        "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0, "object": "embedding"}],
        "model": "nomic-embed-text",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    with patch("aitelier.server.embeddings",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/embeddings", json={
            "model": "nomic-embed-text", "input": "hi",
        })
    assert resp.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]


def test_metrics_endpoint_shape(client):
    """`/v1/metrics` returns the runtime counters operators need to
    diagnose process-level anomalies without resorting to `ps`. Shape
    sanity check (this endpoint is the one we'd reach for when memory
    or in-flight counts misbehave)."""
    resp = client.get("/v1/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["uptime_seconds"] >= 0
    proc = data["process"]
    assert proc["rss_mb"] > 0
    assert proc["cpu_user_seconds"] >= 0
    assert proc["cpu_system_seconds"] >= 0
    runs = data["runs"]
    assert runs["in_flight"] == 0
    assert runs["recent_5min"]["total"] == 0
    assert isinstance(runs["recent_5min"]["by_status"], dict)
    assert data["webhooks"]["pending"] == 0


def test_run_not_found(client):
    resp = client.get("/v1/runs/nonexistent")
    assert resp.status_code == 404


# --- aitelier extras: reasoning, parsed, empty-exit ---


def test_chat_completions_surfaces_aitelier_parsed_for_json_response_format(client):
    fenced = "```json\n{\"answer\": 42}\n```"
    upstream = _openai_chat_response(content=fenced)
    with patch("aitelier.server.chat_completion",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/chat/completions", json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "Return JSON"}],
            "response_format": {"type": "json_object"},
        })
    assert resp.status_code == 200
    body = resp.json()
    msg = body["choices"][0]["message"]
    # Server-side fence stripping surfaces parsed JSON for consumers.
    assert msg.get("aitelier_parsed") == {"answer": 42}


def test_chat_completions_stamps_aitelier_exit_empty(client):
    """When the model burned tokens with no visible content and no reasoning,
    aitelier surfaces an `aitelier_exit: "empty"` signal."""
    upstream = {
        "id": "chatcmpl-x", "object": "chat.completion",
        "created": 1, "model": "local",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop", "logprobs": None,
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 30, "total_tokens": 35},
    }
    with patch("aitelier.server.chat_completion",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/chat/completions", json={
            "model": "local",
            "messages": [{"role": "user", "content": "go"}],
        })
    body = resp.json()
    assert body["choices"][0].get("aitelier_exit") == "empty"


def test_chat_completions_no_empty_signal_when_reasoning_present(client):
    """Reasoning content counts as visible output — empty signal should not fire."""
    upstream = {
        "id": "chatcmpl-x", "object": "chat.completion",
        "created": 1, "model": "local",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Thinking out loud...",
            },
            "finish_reason": "stop", "logprobs": None,
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 30, "total_tokens": 35},
    }
    with patch("aitelier.server.chat_completion",
                new_callable=AsyncMock, return_value=upstream):
        resp = client.post("/v1/chat/completions", json={
            "model": "local",
            "messages": [{"role": "user", "content": "go"}],
        })
    body = resp.json()
    assert "aitelier_exit" not in body["choices"][0]


# --- allow_tool_drop opt-in ---


def test_chat_completions_agent_path_allows_tool_drop_opt_in(client, monkeypatch):
    """With aitelier.allow_tool_drop=true, `tools` is accepted (and silently
    dropped on the wire to Sandbox Agent)."""
    captured = {}

    async def fake_call(name, prompt, **kw):
        captured["called"] = True
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kw.get("run_id", ""),
            "trace_id": kw.get("run_id", ""),
            "content": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "fake"}}],
        "aitelier": {"allow_tool_drop": True},
    })
    assert resp.status_code == 200, resp.text
    assert captured.get("called") is True


def test_llm_path_classifies_connection_refused_as_provider_unavailable(
    client, monkeypatch,
):
    """When LiteLLM is unreachable (connection refused), aitelier must
    return a typed `ProviderUnavailable` envelope with HTTP 503, not a
    bare 500. Earlier deepread report cited bare 500 in this scenario."""
    import httpx as _httpx
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        side_effect=_httpx.ConnectError("Connection refused"),
    )

    async def fake_get_shared():
        return fake_client

    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "claude-haiku",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 503, (
        f"connection-refused should surface as 503 ProviderUnavailable, "
        f"got {resp.status_code} {resp.text[:200]}"
    )
    body = resp.json()
    assert body["error"]["type"] == "ProviderUnavailable"
    assert "Connection refused" in body["error"]["message"] \
        or "refused" in body["error"]["message"].lower()


def test_agent_path_classifies_bad_backend_as_provider_error(client, monkeypatch):
    """Unknown agent backend used to surface as `error.type: "RuntimeError"`
    — the streamed error event's classified type was thrown away by a
    re-wrap in `call_via_sandbox`. The fix preserves the type so consumer
    retry policies keying on the documented vocabulary work."""

    async def fake_stream(name, prompt, **kwargs):
        # Simulate what `call_via_sandbox_stream` emits when sandbox-agent
        # returns 400 for an unknown backend — the producer classifies via
        # classify_error, so the event carries the typed name.
        yield {
            "type": "error",
            "error_type": "ProviderError",
            "error_msg": "Client error '400 Bad Request' for url <sandbox>",
        }

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream",
        fake_stream,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:not-a-real-agent",
        "messages": [{"role": "user", "content": "hi"}],
    })
    body = resp.json()
    assert body["error"]["type"] == "ProviderError", (
        f"agent path must classify backend errors to the documented "
        f"vocabulary, got {body['error']['type']!r}"
    )


def test_chat_completions_agent_path_preserves_token_invariant(client, monkeypatch):
    """OpenAI invariant `total == prompt + completion` holds even when
    inner-agent overhead reports a much larger total upstream; the
    overhead lands in `aitelier_inner_tokens`."""

    async def fake_call(name, prompt, **kw):
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kw.get("run_id", ""),
            "trace_id": kw.get("run_id", ""),
            "content": "ok",
            "usage": {
                "input_tokens": 6, "output_tokens": 16,
                "total_tokens": 31788,
            },
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200, resp.text
    usage = resp.json()["usage"]
    assert usage["prompt_tokens"] == 6
    assert usage["completion_tokens"] == 16
    assert usage["total_tokens"] == 22, "OpenAI invariant: total = prompt + completion"
    assert usage["aitelier_inner_tokens"] == 31766


def test_chat_completions_agent_stream_preserves_token_invariant(
    client, monkeypatch,
):
    """Streaming wire format must match non-streaming: the final SSE chunk
    carries `usage` with `total == prompt + completion`, plus
    `aitelier_inner_tokens` for the inner-agent overhead."""

    async def fake_stream(name, prompt, **kwargs):
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "kind": "agent", "provider": name,
               "status": "ok", "duration_s": 0.1, "run_id": "",
               "trace_id": "", "content": "ok",
               "usage": {"input_tokens": 6, "output_tokens": 16,
                          "total_tokens": 31788},
               "finish_reason": "completed", "tool_calls": [],
               "cost_usd": None, "error_type": None, "error_msg": None}

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
    assert resp.status_code == 200
    body = resp.text
    # Final chunk carries usage; invariant holds.
    assert '"total_tokens": 22' in body or '"total_tokens":22' in body
    assert '"aitelier_inner_tokens": 31766' in body or '"aitelier_inner_tokens":31766' in body
    # Raw upstream total never reaches the wire.
    assert "31788" not in body


def test_chat_completions_agent_path_no_inner_tokens_when_invariant_holds(
    client, monkeypatch,
):
    """When the inner agent's total matches prompt+completion (no hidden
    overhead reported), aitelier_inner_tokens is omitted — we don't add
    noise where there's nothing to report."""

    async def fake_call(name, prompt, **kw):
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kw.get("run_id", ""),
            "trace_id": kw.get("run_id", ""),
            "content": "ok",
            "usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "hi"}],
    })
    usage = resp.json()["usage"]
    assert usage["total_tokens"] == 15
    assert "aitelier_inner_tokens" not in usage


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
    assert ("POST", "/v1/chat/completions") in paths
    assert ("POST", "/v1/embeddings") in paths
    assert ("POST", "/v1/runs") in paths
    assert ("GET", "/v1/models") in paths
    assert ("GET", "/v1/traces") in paths
    assert ("GET", "/v1/health") in paths
    assert ("GET", "/v1/discovery") in paths

    caps = data["capabilities"]
    for name in ("chat_completions", "embeddings", "agent", "traces"):
        assert name in caps
        assert "available" in caps[name]
    assert caps["agent"]["available"] is True  # sandbox reachable → agent capability on

    assert data["dependencies"]["litellm"]["reachable"] is True
    assert data["dependencies"]["litellm"]["models"] == ["claude-sonnet", "nomic-embed-text"]
    assert data["dependencies"]["sandbox_agent"]["reachable"] is True
    assert data["dependencies"]["sandbox_agent"]["agents"] == ["claude-code", "codex"]

    assert isinstance(data["schemas"], dict)


def test_discovery_hides_internal_base_urls_in_hosted_mode(client, monkeypatch):
    """In hosted mode (api_key set), discovery scrubs `base_url` from
    each dep block. Error envelopes already do this; surfacing topology
    here would let any authenticated caller lift the literal address."""
    from aitelier.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.service, "api_key", "secret-token")
    # Bust the discovery cache so this call re-renders with new policy.
    monkeypatch.setattr("aitelier.server._discovery_cache", {"at": 0.0, "value": None})

    with (
        patch("aitelier.server._probe_litellm", new_callable=AsyncMock,
              return_value={"reachable": True, "base_url": "http://localhost:4000",
                            "models": ["claude-sonnet"]}),
        patch("aitelier.server._probe_sandbox_agent", new_callable=AsyncMock,
              return_value={"reachable": True, "base_url": "http://127.0.0.1:2468",
                            "agents": ["claude"]}),
        patch("aitelier.server._probe_traces", return_value={"available": True}),
    ):
        resp = client.get("/v1/discovery", headers={"Authorization": "Bearer secret-token"})
    deps = resp.json()["dependencies"]
    assert "base_url" not in deps["litellm"]
    assert "base_url" not in deps["sandbox_agent"]
    # Reachability + agent/model lists stay — still useful for ops.
    assert deps["litellm"]["reachable"] is True
    assert deps["sandbox_agent"]["agents"] == ["claude"]


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
    # chat_completions stays available as long as *some* backend is reachable
    # (sandbox here). embeddings strictly requires LiteLLM.
    assert caps["chat_completions"]["available"] is True
    assert caps["embeddings"]["available"] is False
    assert "reason" in caps["embeddings"]


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
    resp = client.get("/v1/schemas/run")
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


def test_correlation_id_echoed(client):
    with patch("aitelier.server.chat_completion", new_callable=AsyncMock,
               return_value=_openai_chat_response()):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Correlation-Id": "abc-123"},
        )
    assert resp.status_code == 200
    assert resp.headers["X-Correlation-Id"] == "abc-123"
    assert resp.json()["correlation_id"] == "abc-123"


def test_correlation_id_generated_when_absent(client):
    with patch("aitelier.server.chat_completion", new_callable=AsyncMock,
               return_value=_openai_chat_response()):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    cid = resp.headers.get("X-Correlation-Id")
    assert cid and len(cid) >= 8
    assert resp.json()["correlation_id"] == cid


def test_correlation_id_persisted_in_trace_metadata(client):
    """Correlation ID should land on the durable run record."""
    with patch("aitelier.server.chat_completion", new_callable=AsyncMock,
                return_value=_openai_chat_response()):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Correlation-Id": "trace-abc"},
        )

    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == "trace-abc"

    runs = _runs_from_store()
    assert any(r.correlation_id == "trace-abc" for r in runs)


# --- Streaming chat completions ---


def test_chat_completions_stream_yields_openai_chunks(client):
    async def fake_stream(body, *, timeout):
        yield {
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": "Hello"},
                "finish_reason": None,
            }],
        }
        yield {
            "choices": [{
                "index": 0,
                "delta": {"content": " world"},
                "finish_reason": None,
            }],
        }
        yield {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    with patch("aitelier.server.chat_completion_stream", fake_stream):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    # OpenAI streaming uses unnamed `data:` lines (no `event:` field).
    assert "Hello" in body
    assert "data: [DONE]" in body
    assert "aitelier_run_id" in body


def test_chat_completions_stream_error_event_on_failure(client):
    async def fake_stream(body, *, timeout):
        if False:  # pragma: no cover
            yield {}
        from aitelier.providers.llm import LLMError
        raise LLMError("ProviderError", "boom")

    with patch("aitelier.server.chat_completion_stream", fake_stream):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
        )

    assert resp.status_code == 200
    body = resp.text
    assert "ProviderError" in body
    assert "boom" in body


# --- Trace recording for /v1/complete, /v1/embed, /v1/complete/stream ---


def _runs_from_store():
    """Synchronously fetch all runs from the conftest-provided InMemoryStore.

    TestClient may hold an event loop, so reach into the store directly
    rather than going through `get_store()` which would await.
    """
    from aitelier.storage._store import _store as _module_store
    if _module_store is None:
        return []
    # InMemoryStore exposes ._runs as a plain dict
    return list(_module_store._runs.values())


def test_chat_completions_records_trace(client):
    with patch("aitelier.server.chat_completion", new_callable=AsyncMock,
                return_value=_openai_chat_response()):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Correlation-Id": "trace-cmpl"},
        )
    assert resp.status_code == 200
    runs = _runs_from_store()
    assert runs, "expected at least one run recorded"
    assert any(r.correlation_id == "trace-cmpl" for r in runs)


def test_embeddings_records_trace(client):
    upstream = {
        "object": "list",
        "data": [{"object": "embedding", "embedding": [0.1], "index": 0}],
        "model": "nomic-embed-text",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    with patch("aitelier.server.embeddings", new_callable=AsyncMock,
                return_value=upstream):
        resp = client.post(
            "/v1/embeddings",
            json={"model": "nomic-embed-text", "input": ["hi"]},
            headers={"X-Correlation-Id": "trace-emb"},
        )
    assert resp.status_code == 200
    runs = _runs_from_store()
    assert any(r.kind == "embed" for r in runs)


def test_chat_completions_stream_records_trace_at_done(client):
    async def fake_stream(body, *, timeout):
        yield {"choices": [{"index": 0, "delta": {"content": "ok"},
                            "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}

    with patch("aitelier.server.chat_completion_stream", fake_stream):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            headers={"X-Correlation-Id": "trace-strm"},
        )
    assert resp.status_code == 200
    runs = _runs_from_store()
    assert any(r.correlation_id == "trace-strm" and r.kind == "complete"
                for r in runs)


# --- Cancellation ---


# --- Sandbox passthrough endpoints ---


def _stub_sa_response(status: int = 200, json_body=None, text: str = ""):
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = {"content-type": "application/json"}
    resp.json = MagicMock(return_value=json_body or {})
    resp.content = b""
    return resp


def _patch_sa_proxy(status: int = 200, json_body=None):
    """Stub the shared httpx client used by _sa_proxy."""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.request = AsyncMock(
        return_value=_stub_sa_response(status=status, json_body=json_body),
    )

    async def fake_get_shared():
        return fake_client

    return patch("aitelier.providers.llm.get_shared_client",
                 side_effect=fake_get_shared), fake_client


# --- Agent prepare + artifacts orchestration (via /v1/chat/completions) ---


def _patch_sa_request(routes):
    """Stub the shared httpx client. `routes` maps a substring of the URL
    (or method:URL_substring) to a (status, json_body) tuple. Default 200/{}."""
    from unittest.mock import MagicMock

    async def fake_request(method, url, **kwargs):
        for key, (status, body) in routes.items():
            if ":" in key:
                k_method, k_path = key.split(":", 1)
                if method != k_method:
                    continue
                if k_path in url:
                    return _stub_sa_response(status=status, json_body=body)
            elif key in url:
                return _stub_sa_response(status=status, json_body=body)
        return _stub_sa_response(status=200, json_body={})

    fake_client = MagicMock()
    fake_client.request = AsyncMock(side_effect=fake_request)

    async def fake_get_shared():
        return fake_client

    return patch("aitelier.providers.llm.get_shared_client",
                 side_effect=fake_get_shared), fake_client


def _agent_chat_body(content: str = "hi", **aitelier_opts) -> dict:
    body = {
        "model": "agent:claude",
        "messages": [{"role": "user", "content": content}],
    }
    if aitelier_opts:
        body["aitelier"] = aitelier_opts
    return body


def test_agent_prepare_runs_setup_commands_and_files(client):
    """prepare.commands + prepare.files run before agent; both succeed."""
    async def fake_call(name, prompt, **kwargs):
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kwargs.get("run_id", ""),
            "trace_id": kwargs.get("run_id", ""),
            "content": "ok",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    p, fake_client = _patch_sa_request({
        "/v1/processes/run": (200, {"exit_code": 0, "stdout": "ok"}),
        "/v1/fs/file":       (200, {"ok": True}),
    })
    with (p,
          patch("aitelier.providers.sandbox_agent.call_via_sandbox",
                side_effect=fake_call)):
        resp = client.post("/v1/chat/completions", json=_agent_chat_body(
            prepare={
                "commands": [{"cmd": "apt-get", "args": ["install", "jq"]}],
                "files": [{"path": "/workspace/in.txt", "content": "hello"}],
            },
        ))
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "ok"
    methods_and_paths = [
        (c.args[0], c.args[1]) for c in fake_client.request.call_args_list
    ]
    assert any(m == "POST" and "/v1/processes/run" in p for m, p in methods_and_paths)
    assert any(m == "PUT" and "/v1/fs/file" in p for m, p in methods_and_paths)


def test_agent_prepare_command_failure_short_circuits_agent(client):
    """A non-zero exit in prepare.commands aborts the workflow — agent never runs.
    The chat-completions endpoint surfaces it as a 500 error response."""
    called_agent = False

    async def fake_call(name, prompt, **kwargs):
        nonlocal called_agent
        called_agent = True
        return {"kind": "agent", "provider": name, "status": "ok"}

    p, _ = _patch_sa_request({
        "/v1/processes/run": (200, {"exit_code": 1, "stderr": "no jq"}),
    })
    with (p,
          patch("aitelier.providers.sandbox_agent.call_via_sandbox",
                side_effect=fake_call)):
        resp = client.post("/v1/chat/completions", json=_agent_chat_body(
            prepare={"commands": [{"cmd": "false", "args": []}]},
        ))
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["type"] == "PrepareFailed"
    assert called_agent is False


def test_agent_artifacts_fetched_after_run(client):
    async def fake_call(name, prompt, **kwargs):
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": "", "trace_id": "",
            "content": "ok",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    p, _ = _patch_sa_request({
        "GET:/v1/fs/file": (200, {"content": "{\"hello\": \"world\"}"}),
    })
    with (p,
          patch("aitelier.providers.sandbox_agent.call_via_sandbox",
                side_effect=fake_call)):
        resp = client.post("/v1/chat/completions", json=_agent_chat_body(
            artifacts={"fetch": ["/workspace/out.json"]},
        ))
    assert resp.status_code == 200
    data = resp.json()
    assert "aitelier_artifacts" in data
    assert data["aitelier_artifacts"]["/workspace/out.json"] == "{\"hello\": \"world\"}"


def test_agent_sidecars_stopped_even_after_agent_error(client):
    """Sidecars start in prepare; even if the agent errors, they're stopped."""
    started_ids = []
    stop_calls = []

    async def fake_request(method, url, **kwargs):
        if method == "POST" and "/v1/processes" in url and "/stop" not in url:
            started_ids.append("sc1")
            return _stub_sa_response(status=200, json_body={"id": "sc1"})
        if method == "POST" and url.endswith("/processes/sc1/stop"):
            stop_calls.append(url)
            return _stub_sa_response(status=200, json_body={"stopped": True})
        return _stub_sa_response(status=200, json_body={})

    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.request = AsyncMock(side_effect=fake_request)

    async def fake_get_shared():
        return fake_client

    async def fake_call(name, prompt, **kwargs):
        raise RuntimeError("agent kaboom")

    with (
        patch("aitelier.providers.llm.get_shared_client",
              side_effect=fake_get_shared),
        patch("aitelier.providers.sandbox_agent.call_via_sandbox",
              side_effect=fake_call),
    ):
        resp = client.post("/v1/chat/completions", json=_agent_chat_body(
            prepare={
                "sidecars": [{"name": "mockapi", "cmd": "python",
                              "args": ["x.py"]}],
            },
        ))
    # Agent errored → 500 from chat/completions; sidecars stopped anyway.
    assert resp.status_code == 500
    assert len(started_ids) == 1
    assert len(stop_calls) == 1


# --- /v1/runs (read API) ---


def test_list_runs_filters(client):
    """Seed runs via the store; verify list_runs endpoint honors filters."""
    from datetime import UTC, datetime

    from aitelier.storage._store import _store
    now = datetime.now(UTC)
    from aitelier.storage.models import Run as _R
    _store._runs["r-ok"] = _R(
        run_id="r-ok", state="completed", kind="agent",
        started_at=now, ended_at=now, agent_id="claude",
        trace_tag="A", status="ok", total_tokens=100,
    )
    _store._runs["r-fail"] = _R(
        run_id="r-fail", state="failed", kind="agent",
        started_at=now, ended_at=now, agent_id="claude",
        trace_tag="A", status="error", error_type="Timeout",
    )
    _store._runs["r-other"] = _R(
        run_id="r-other", state="completed", kind="complete",
        started_at=now, ended_at=now, model="claude-sonnet",
        trace_tag="B",
    )

    # All
    resp = client.get("/v1/runs")
    assert resp.status_code == 200
    ids = {r["run_id"] for r in resp.json()}
    assert ids == {"r-ok", "r-fail", "r-other"}

    # By trace_tag
    resp = client.get("/v1/runs?trace_tag=A")
    ids = {r["run_id"] for r in resp.json()}
    assert ids == {"r-ok", "r-fail"}

    # By kind
    resp = client.get("/v1/runs?kind=agent")
    ids = {r["run_id"] for r in resp.json()}
    assert "r-other" not in ids


def test_list_run_events(client):
    from datetime import UTC, datetime

    from aitelier.storage._store import _store
    from aitelier.storage.models import Run as _R
    from aitelier.storage.models import RunEvent as _E

    now = datetime.now(UTC)
    _store._runs["r1"] = _R(run_id="r1", state="completed", kind="agent",
                             started_at=now, ended_at=now)
    _store._events["r1"] = [
        _E(run_id="r1", seq=1, kind="start", payload={}, ts=now, event_id=1),
        _E(run_id="r1", seq=2, kind="delta", payload={"content": "hi"},
            ts=now, event_id=2),
        _E(run_id="r1", seq=3, kind="finish", payload={}, ts=now, event_id=3),
    ]

    resp = client.get("/v1/runs/r1/events")
    assert resp.status_code == 200
    data = resp.json()
    assert [e["kind"] for e in data] == ["start", "delta", "finish"]

    # since_seq pagination
    resp = client.get("/v1/runs/r1/events?since_seq=1")
    seqs = [e["seq"] for e in resp.json()]
    assert seqs == [2, 3]


def test_events_stream_for_terminal_run(client):
    """For a run that's already terminal, the stream yields the backlog then closes."""
    from datetime import UTC, datetime

    from aitelier.storage._store import _store
    from aitelier.storage.models import Run as _R
    from aitelier.storage.models import RunEvent as _E

    now = datetime.now(UTC)
    _store._runs["r1"] = _R(run_id="r1", state="completed", kind="agent",
                             started_at=now, ended_at=now)
    _store._events["r1"] = [
        _E(run_id="r1", seq=1, kind="start", payload={}, ts=now, event_id=1),
        _E(run_id="r1", seq=2, kind="finish", payload={"finish_reason": "ok"},
            ts=now, event_id=2),
    ]
    resp = client.get("/v1/runs/r1/events/stream")
    assert resp.status_code == 200
    body = resp.text
    assert "event: run.start" in body
    assert "event: run.finish" in body


def test_events_stream_404_for_unknown(client):
    resp = client.get("/v1/runs/nonexistent/events/stream")
    assert resp.status_code == 404


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


# --- POST /v1/runs (async agent) ---


def test_async_run_returns_run_id_immediately(client):
    """POST /v1/runs returns {run_id, status:'accepted'} without blocking."""
    async def slow_call(name, prompt, **kwargs):
        import asyncio
        await asyncio.sleep(0.05)
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.05, "run_id": kwargs.get("run_id", ""),
            "trace_id": kwargs.get("run_id", ""),
            "content": "done",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    with patch("aitelier.providers.sandbox_agent.call_via_sandbox",
                side_effect=slow_call):
        resp = client.post("/v1/runs", json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["run_id"]
    assert data["webhook_url"] is None


def test_async_run_rejects_non_agent_model(client):
    """/v1/runs is agent-only; LLM models should be 400."""
    resp = client.post("/v1/runs", json={
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 400
    assert "agent" in resp.json()["detail"].lower()


# --- /v1/schedules ---


def test_create_and_list_schedule(client):
    """Schedules persist via the store fixture; no file I/O needed."""
    resp = client.post("/v1/schedules", json={
        "name": "audit-daily",
        "task": {
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "audit"}],
        },
        "interval_seconds": 86400,
    })
    assert resp.status_code == 200
    sid = resp.json()["id"]

    resp = client.get("/v1/schedules")
    ids = [s["id"] for s in resp.json()]
    assert sid in ids

    resp = client.get(f"/v1/schedules/{sid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "audit-daily"

    resp = client.delete(f"/v1/schedules/{sid}")
    assert resp.status_code == 200

    resp = client.get(f"/v1/schedules/{sid}")
    assert resp.status_code == 404


def test_create_schedule_rejects_invalid(client):
    """Missing task field → 400."""
    resp = client.post("/v1/schedules", json={"interval_seconds": 60})
    # Pydantic will 422 for missing required field
    assert resp.status_code in (400, 422)


# --- Streaming agent (via /v1/chat/completions with stream=true) ---


def test_chat_completions_agent_stream_yields_openai_chunks(client):
    async def fake_stream(name, prompt, **kwargs):
        yield {"type": "delta", "content": "thinking..."}
        yield {"type": "tool_call", "server": "example-mcp",
               "tool": "query_corpus", "input": {"q": "foo"}}
        yield {"type": "tool_result", "tool": "query_corpus",
               "output": ["doc1"], "elapsed_ms": 42}
        yield {"type": "done", "kind": "agent", "provider": name,
               "status": "ok", "duration_s": 0.5, "run_id": "",
               "trace_id": "", "content": "result text",
               "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
               "finish_reason": "completed", "tool_calls": [],
               "cost_usd": None, "error_type": None, "error_msg": None}

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [{"role": "user", "content": "What's in the corpus?"}],
            "stream": True,
        }, headers={"X-Correlation-Id": "stream-cid"})

    assert resp.status_code == 200
    body = resp.text
    # Agent stream yields OpenAI chunks; first chunk seeds the assistant role.
    assert "thinking..." in body
    assert "result text" not in body  # tool deltas + final usage, not full content
    assert '"finish_reason": "stop"' in body or '"finish_reason":"stop"' in body
    assert "data: [DONE]" in body


def test_chat_completions_agent_stream_error_event_on_failure(client):
    async def fake_stream(name, prompt, **kwargs):
        if False:  # pragma: no cover
            yield {}
        yield {"type": "error", "error_type": "ProviderError",
               "error_msg": "agent crashed"}

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [{"role": "user", "content": "boom"}],
            "stream": True,
        })
    assert resp.status_code == 200
    body = resp.text
    assert "ProviderError" in body
    assert "agent crashed" in body


def test_chat_completions_agent_stream_emits_keepalive_during_silence(
    client, monkeypatch,
):
    """A long silent planning phase from the inner agent emits an SSE
    comment frame, keeping reverse proxies and consumer read timeouts
    from tearing down the connection mid-run."""
    monkeypatch.setattr("aitelier.server._SSE_KEEPALIVE_SECONDS", 0.05)

    async def fake_stream(name, prompt, **kwargs):
        import asyncio
        await asyncio.sleep(0.2)  # silent planning — covers >1 keepalive interval
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "kind": "agent", "provider": name,
               "status": "ok", "duration_s": 0.2, "run_id": "",
               "trace_id": "", "content": "ok",
               "usage": {"input_tokens": 1, "output_tokens": 1,
                          "total_tokens": 2},
               "finish_reason": "completed", "tool_calls": [],
               "cost_usd": None, "error_type": None, "error_msg": None}

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
    assert resp.status_code == 200
    assert ": keepalive" in resp.text
    assert "data: [DONE]" in resp.text


def test_chat_completions_agent_stream_keepalive_fires_during_dropped_events(
    client, monkeypatch,
):
    """The keepalive must fire even when the producer is emitting
    tool_call / tool_result events that we drop from the wire — those
    events feed the queue but produce no visible chunks, so the
    consumer sees a silent stream without compensating keepalive frames.
    Track time-since-last-yield, not queue-get timeouts."""
    monkeypatch.setattr("aitelier.server._SSE_KEEPALIVE_SECONDS", 0.05)

    async def fake_stream(name, prompt, **kwargs):
        import asyncio
        # Steady stream of dropped events (tool_call/tool_result) over
        # 0.3s; under the buggy implementation the queue.get() timer
        # resets on each one and no keepalive ever fires.
        for _ in range(15):
            await asyncio.sleep(0.02)
            yield {"type": "tool_call", "server": "x", "tool": "y", "input": {}}
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "kind": "agent", "provider": name,
               "status": "ok", "duration_s": 0.3, "run_id": "",
               "trace_id": "", "content": "ok",
               "usage": {"input_tokens": 1, "output_tokens": 1,
                          "total_tokens": 2},
               "finish_reason": "completed", "tool_calls": [],
               "cost_usd": None, "error_type": None, "error_msg": None}

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
    assert resp.status_code == 200
    assert ": keepalive" in resp.text


def test_traces_aggregates_groups_by_status(client):
    """/v1/traces/aggregates rolls up runs by the requested grouping.
    Should return a {groups, total} shape with one bucket per distinct
    status value across the queried runs."""
    import asyncio

    from aitelier.storage import RunSpec, get_store

    async def seed():
        store = await get_store()
        for i, status in enumerate(["ok", "ok", "error"]):
            spec = RunSpec(run_id=f"r-agg-{i}", kind="agent")
            await store.create_run(spec)
            await store.update_run_state(spec.run_id, "running")
            await store.finalize_run(spec.run_id, {
                "status": status, "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            })
    asyncio.new_event_loop().run_until_complete(seed())

    resp = client.get("/v1/traces/aggregates?group_by=status")
    assert resp.status_code == 200
    body = resp.json()
    assert "groups" in body and "total" in body
    by_key = {g["key"]: g["count"] for g in body["groups"]}
    assert by_key.get("ok") == 2
    assert by_key.get("error") == 1


def test_traces_aggregates_rejects_unknown_group_by(client):
    """Unknown grouping → 400 from the store's value validation, not
    a 500. The list of allowed keys is canonical."""
    resp = client.get("/v1/traces/aggregates?group_by=not_a_field")
    assert resp.status_code == 400


def test_run_events_stream_yields_recent_events(client):
    """`/v1/runs/{id}/events/stream` tails the event log. Even after a
    run is terminal, the stream replays the events already recorded so
    consumers can backfill without polling /events."""
    import asyncio

    from aitelier.storage import RunEvent, RunSpec, get_store

    async def seed():
        store = await get_store()
        await store.create_run(RunSpec(run_id="r-stream", kind="agent"))
        await store.update_run_state("r-stream", "running")
        await store.append_event(RunEvent(
            run_id="r-stream", seq=1, kind="delta", payload={"content": "hi"},
        ))
        await store.append_event(RunEvent(
            run_id="r-stream", seq=2, kind="finish",
            payload={"finish_reason": "stop"},
        ))
        await store.finalize_run("r-stream", {"status": "ok"})
    asyncio.new_event_loop().run_until_complete(seed())

    with client.stream(
        "GET", "/v1/runs/r-stream/events/stream",
        params={"timeout": "1"},
    ) as resp:
        body = b"".join(resp.iter_bytes()).decode()

    assert resp.status_code == 200
    assert "delta" in body
    assert "finish" in body


def test_chat_completions_503s_when_saturated(client, monkeypatch):
    """When `service.max_in_flight_runs` is reached, new requests get a
    typed 503 instead of being queued behind the event loop until they
    time out. Consumers' retry policies on ProviderUnavailable will
    back off and re-attempt — the cap protects the asyncpg pool and
    sandbox-agent slots from a flood."""
    from aitelier.server import _active_runs
    fake_task = MagicMock()
    fake_task.done = MagicMock(return_value=False)
    cap = 4
    monkeypatch.setattr(
        "aitelier.server.get_config",
        lambda: MagicMock(service=MagicMock(max_in_flight_runs=cap)),
    )
    # Fill the registry so the next call hits the cap.
    for i in range(cap):
        _active_runs[f"saturating-run-{i}"] = fake_task
    try:
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 503
        assert "capacity" in resp.text.lower()
    finally:
        for i in range(cap):
            _active_runs.pop(f"saturating-run-{i}", None)


def test_chat_completions_records_parent_run_id_on_agent_path(
    client, monkeypatch,
):
    """The `aitelier.parent_run_id` field flows from request → RunSpec
    → run row → /v1/runs filter. No FK / no cycle check — just a
    pass-through pointer the consumer can later query by."""
    import asyncio

    from aitelier.storage import RunFilter, get_store

    async def fake_call(name, prompt, **kw):
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kw.get("run_id", ""),
            "trace_id": kw.get("run_id", ""),
            "content": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call,
    )
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "child task"}],
        "aitelier": {"parent_run_id": "parent-xyz",
                      "trace_tag": "workflow-1"},
    })
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["aitelier_run_id"]

    async def _check():
        store = await get_store()
        run = await store.get_run(run_id)
        assert run.parent_run_id == "parent-xyz"
        children = await store.list_runs(RunFilter(parent_run_id="parent-xyz"))
        assert any(r.run_id == run_id for r in children)
    asyncio.new_event_loop().run_until_complete(_check())


def test_v1_runs_filter_by_parent_run_id(client, monkeypatch):
    """/v1/runs?parent_run_id=X surfaces children to consumers building
    multi-agent workflow visualizations."""
    import asyncio

    from aitelier.storage import RunSpec, get_store

    async def seed():
        store = await get_store()
        await store.create_run(RunSpec(run_id="parent", kind="agent"))
        for i in range(3):
            await store.create_run(RunSpec(
                run_id=f"child-{i}", kind="agent", parent_run_id="parent",
            ))
        await store.create_run(RunSpec(run_id="unrelated", kind="agent"))
    asyncio.new_event_loop().run_until_complete(seed())

    resp = client.get("/v1/runs?parent_run_id=parent&limit=10")
    assert resp.status_code == 200
    ids = sorted(r["run_id"] for r in resp.json())
    assert ids == ["child-0", "child-1", "child-2"]


def test_v1_runs_response_includes_parent_run_id(client, monkeypatch):
    """GET /v1/runs/{id} echoes `parent_run_id` so consumers building
    trees can reconstruct edges from a leaf upward."""
    import asyncio

    from aitelier.storage import RunSpec, get_store

    async def seed():
        store = await get_store()
        await store.create_run(RunSpec(
            run_id="leaf", kind="agent", parent_run_id="root",
        ))
    asyncio.new_event_loop().run_until_complete(seed())

    resp = client.get("/v1/runs/leaf")
    assert resp.status_code == 200
    assert resp.json()["parent_run_id"] == "root"


def test_chat_completions_rejects_unknown_aitelier_field(client):
    """`aitelier_request.schema.json` is `additionalProperties: false`.
    The Pydantic model must enforce that at the wire so consumers
    misplacing `timeout` (a top-level body field) under `aitelier.*`
    get a clean 422 — not a downstream `KeyError: 'sessionId'` leaking
    from the runner."""
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "x"}],
        "aitelier": {"workspace": "/tmp", "timeout": 999},
    })
    assert resp.status_code == 422, resp.text
    body = resp.json()
    text = str(body).lower()
    assert "timeout" in text, "validation error must name the offending field"
    assert "extra" in text or "forbidden" in text or "not permitted" in text


def test_chat_completions_rejects_empty_messages(client):
    """Empty `messages: []` must be a clean 422 validation error at the
    request boundary, not an opaque downstream RuntimeError from ACP
    mentioning cache_control (the prior path leaked an internal failure
    mode and didn't tell the consumer the real problem)."""
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude",
        "messages": [],
    })
    assert resp.status_code == 422
    body = resp.json()
    # Whatever the framework's error envelope looks like, the underlying
    # complaint must mention messages and must NOT mention ACP internals.
    text = str(body).lower()
    assert "messages" in text
    assert "cache_control" not in text
    assert "acp" not in text


def test_chat_completions_agent_stream_terminal_chunk_has_tool_summary(
    client, monkeypatch,
):
    """Streaming terminal chunk should mirror the non-streaming response
    shape: `aitelier_tool_call_count` + `aitelier_tool_names` present,
    so consumers don't need a separate code path to find out which tools
    the inner agent ran."""

    async def fake_stream(name, prompt, **kwargs):
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "kind": "agent", "provider": name,
               "status": "ok", "duration_s": 0.1, "run_id": "",
               "trace_id": "", "content": "ok",
               "usage": {"input_tokens": 1, "output_tokens": 1,
                          "total_tokens": 2},
               "finish_reason": "completed",
               "tool_calls": [{"tool": "Read"}, {"tool": "Edit"}],
               "cost_usd": None, "error_type": None, "error_msg": None}

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
    body = resp.text
    assert '"aitelier_tool_call_count": 2' in body
    assert '"Read"' in body
    assert '"Edit"' in body


def test_chat_completions_agent_stream_finalizes_on_consumer_disconnect(
    client, monkeypatch,
):
    """When the consumer disconnects mid-stream (FastAPI calls aclose()
    on the generator), the run must transition to a terminal state
    rather than stay state=running forever and contaminate /v1/metrics
    in_flight counts. Verified by reading the run state after a stream
    that's consumed only partially."""
    import asyncio

    from aitelier.storage import get_store

    async def fake_stream(name, prompt, **kwargs):
        for _ in range(20):
            await asyncio.sleep(0.05)
            yield {"type": "delta", "content": "x"}
        # never reaches done

    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }) as resp:
            # Drain only one chunk then drop the connection.
            iter_ = resp.iter_lines()
            for _ in range(3):
                next(iter_, None)
            # Drop the connection — context manager close triggers aclose()
            # on the server's generator.

    async def _check():
        store = await get_store()
        from aitelier.storage import RunFilter
        runs = await store.list_runs(RunFilter(limit=5))
        agent_runs = [r for r in runs if r.kind == "agent"]
        assert agent_runs, "expected at least one agent run recorded"
        latest = agent_runs[0]
        assert latest.state != "running", (
            f"run {latest.run_id} stuck in state=running after consumer "
            f"disconnect — orphan risk + /v1/metrics in_flight pollution"
        )
        assert latest.state in ("cancelled", "completed", "failed")
    asyncio.new_event_loop().run_until_complete(_check())


def test_chat_completions_agent_stream_idempotency_replays_chunks(client):
    """Same Idempotency-Key + same body + stream=true: second call replays
    the cached SSE stream rather than re-running the inner agent — a
    dropped first call must not double-bill the subscription or
    re-execute side effects on reconnect."""
    inner_call_count = {"n": 0}

    async def fake_stream(name, prompt, **kwargs):
        inner_call_count["n"] += 1
        yield {"type": "delta", "content": "hello"}
        yield {"type": "done", "kind": "agent", "provider": name,
               "status": "ok", "duration_s": 0.1, "run_id": "",
               "trace_id": "", "content": "hello",
               "usage": {"input_tokens": 1, "output_tokens": 1,
                          "total_tokens": 2},
               "finish_reason": "completed", "tool_calls": [],
               "cost_usd": None, "error_type": None, "error_msg": None}

    body = {
        "model": "agent:claude-code",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    headers = {"Idempotency-Key": "k-stream-1"}
    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        first = client.post("/v1/chat/completions", json=body, headers=headers)
        second = client.post("/v1/chat/completions", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert inner_call_count["n"] == 1, "second call must replay, not re-execute"
    # Same run_id appears in both responses, proving replay (not a fresh run).
    import re
    rid_pattern = re.compile(r'"aitelier_run_id":\s*"([^"]+)"')
    first_ids = set(rid_pattern.findall(first.text))
    second_ids = set(rid_pattern.findall(second.text))
    assert first_ids == second_ids
    # Both responses end with the SSE terminator.
    assert "data: [DONE]" in second.text
    assert "hello" in second.text


def test_chat_completions_agent_stream_idempotency_skips_failed_streams(client):
    """Failed streams are NOT cached — a retrying consumer should get a
    fresh attempt at success, not a locked-in error."""
    inner_call_count = {"n": 0}

    async def fake_stream(name, prompt, **kwargs):
        inner_call_count["n"] += 1
        if False:  # pragma: no cover
            yield {}
        yield {"type": "error", "error_type": "ProviderError",
               "error_msg": "transient blip"}

    body = {
        "model": "agent:claude-code",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    headers = {"Idempotency-Key": "k-stream-fail"}
    with patch(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream", fake_stream,
    ):
        client.post("/v1/chat/completions", json=body, headers=headers)
        client.post("/v1/chat/completions", json=body, headers=headers)

    assert inner_call_count["n"] == 2, "failed stream must not be cached"


def test_fold_examples_into_system_prompt():
    from aitelier.server import _fold_examples
    out = _fold_examples(
        "You are a curator.",
        [{"user": "Q1", "assistant": "A1"}, {"user": "Q2", "assistant": "A2"}],
    )
    assert "You are a curator." in out
    assert "## Examples" in out
    assert "User: Q1" in out
    assert "Assistant: A1" in out
    assert "User: Q2" in out


def test_fold_examples_no_examples_passes_through():
    from aitelier.server import _fold_examples
    assert _fold_examples("hello", None) == "hello"
    assert _fold_examples("hello", []) == "hello"
    assert _fold_examples(None, None) is None


def test_fold_examples_only_examples_no_system_prompt():
    from aitelier.server import _fold_examples
    out = _fold_examples(None, [{"user": "Q", "assistant": "A"}])
    assert out.startswith("## Examples")
    assert "User: Q" in out


def test_chat_completions_agent_path_folds_examples(client):
    """`aitelier.examples` should merge into the system prompt sent downstream."""
    captured = {}

    async def fake_call(name, prompt, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt")
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": "r", "trace_id": "r",
            "content": "ok",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    with patch("aitelier.providers.sandbox_agent.call_via_sandbox",
                side_effect=fake_call):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude-code",
            "messages": [
                {"role": "system", "content": "You are a curator."},
                {"role": "user", "content": "Process today's feeds."},
            ],
            "aitelier": {
                "examples": [{"user": "old item", "assistant": "yes, archive"}],
            },
        })
    assert resp.status_code == 200
    sp = captured.get("system_prompt") or ""
    assert "You are a curator." in sp
    assert "## Examples" in sp
    assert "User: old item" in sp


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


@pytest.mark.asyncio
async def test_lifespan_sweeps_orphaned_runs_on_startup():
    """A run left in `running` from a previous process must be flipped to
    `orphaned` before traffic is accepted, so dashboards don't show ghosts."""
    from aitelier.server import app, lifespan
    from aitelier.storage import RunSpec, get_store

    store = await get_store()
    await store.create_run(RunSpec(run_id="ghost", kind="agent"))
    await store.update_run_state("ghost", "running")

    async with lifespan(app):
        run = await store.get_run("ghost")
        assert run.state == "orphaned"
        assert run.ended_at is not None


def test_correlation_id_propagates_to_log_records(client, caplog):
    """Logging inside a request should pick up correlation_id from contextvar."""
    import logging

    from aitelier.server import logger

    async def emit_and_complete(body, *, timeout=60):
        logger.info("inside-request")
        return _openai_chat_response()

    with caplog.at_level(logging.INFO, logger="aitelier"):
        with patch("aitelier.server.chat_completion",
                   new_callable=AsyncMock, side_effect=emit_and_complete):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "claude-sonnet",
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Correlation-Id": "log-cid-1"},
            )
    assert resp.status_code == 200
    matched = [r for r in caplog.records if r.getMessage() == "inside-request"]
    assert matched, "expected a log line emitted during the request"
    assert getattr(matched[0], "correlation_id", None) == "log-cid-1"


def test_correlation_id_in_sse_events(client):
    async def fake_stream(body, *, timeout):
        yield {"choices": [{"index": 0, "delta": {"content": "hi"},
                            "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}

    with patch("aitelier.server.chat_completion_stream", fake_stream):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            headers={"X-Correlation-Id": "sse-cid"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert '"correlation_id": "sse-cid"' in body or '"correlation_id":"sse-cid"' in body
