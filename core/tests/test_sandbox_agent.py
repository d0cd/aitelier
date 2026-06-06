"""Tests for providers/sandbox_agent.py — the ACP client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aitelier.providers.sandbox_agent import (
    AcpClient,
    AcpError,
    _adapt_mcp_servers,
    _aggregate_result,
    call_via_sandbox,
)

# --- Low-level client --------------------------------------------------------


@pytest.mark.asyncio
async def test_call_returns_result_from_synchronous_post():
    fake_http = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    fake_resp.raise_for_status = MagicMock()
    fake_http.post = AsyncMock(return_value=fake_resp)

    async with AcpClient("http://x:2468", "claude-code", http_client=fake_http) as c:
        result = await c.call("initialize", {"protocolVersion": 1}, first=True)

    assert result == {"ok": True}
    # First POST should include the agent query param
    args, kwargs = fake_http.post.call_args
    assert "agent=claude-code" in args[0]


@pytest.mark.asyncio
async def test_call_raises_on_jsonrpc_error():
    fake_http = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32601, "message": "Method not found"},
    })
    fake_resp.raise_for_status = MagicMock()
    fake_http.post = AsyncMock(return_value=fake_resp)

    async with AcpClient("http://x:2468", "claude-code", http_client=fake_http) as c:
        with pytest.raises(AcpError) as exc_info:
            await c.call("nonexistent", {}, first=True)

    assert exc_info.value.code == -32601


@pytest.mark.asyncio
async def test_client_threads_per_request_timeout():
    """AcpClient should construct its httpx client with the requested timeout,
    not the old hardcoded 60s — real agentic runs take minutes."""
    import httpx as _httpx
    captured = {}

    real_init = _httpx.AsyncClient.__init__

    def spying_init(self, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_init(self, *args, **kwargs)

    with patch.object(_httpx.AsyncClient, "__init__", spying_init):
        async with AcpClient("http://x:2468", "claude-code", timeout=300.0):
            pass

    t = captured["timeout"]
    # httpx.Timeout exposes .read for the read-timeout component
    assert t.read == 300.0 or t == 300.0


def test_error_result_includes_url_and_elapsed():
    from aitelier.providers.sandbox_agent import _error_result
    r = _error_result("claude", "r1", Exception(""), 60.5,
                       base_url="http://localhost:2468")
    assert "url=http://localhost:2468" in r["error_msg"]
    assert "elapsed=60.5s" in r["error_msg"]
    # Even when str(exc) is empty, error_msg is non-empty
    assert r["error_msg"]


@pytest.mark.asyncio
async def test_notify_accepts_202():
    fake_http = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 202
    fake_http.post = AsyncMock(return_value=fake_resp)

    async with AcpClient("http://x:2468", "claude-code", http_client=fake_http) as c:
        await c.notify("session/cancel", {"sessionId": "abc"})

    fake_http.post.assert_awaited_once()


# --- Result aggregation ------------------------------------------------------


def test_aggregate_result_collects_message_chunks():
    notifications = [
        {"method": "session/update", "params": {"update": {
            "type": "messageChunk", "content": "Hello"}}},
        {"method": "session/update", "params": {"update": {
            "type": "messageChunk", "content": " world"}}},
    ]
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed"},
        notifications=notifications,
        elapsed=1.5,
        response_format=None,
    )
    assert result["status"] == "ok"
    assert result["content"] == "Hello world"
    assert result["finish_reason"] == "completed"
    assert result["provider"] == "claude-code"
    assert result["run_id"] == "r1"


def test_aggregate_result_captures_tool_calls():
    notifications = [
        {"method": "session/update", "params": {"update": {
            "type": "toolCall",
            "name": "Read",
            "arguments": {"path": "/tmp/foo"},
        }}},
    ]
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed"},
        notifications=notifications,
        elapsed=0.5,
        response_format=None,
    )
    assert len(result["tool_calls"]) == 1
    # Aligned with result.schema.json: tool_calls[].tool, not .name
    assert result["tool_calls"][0]["tool"] == "Read"
    assert result["tool_calls"][0]["input"] == {"path": "/tmp/foo"}


def test_aggregate_result_extracts_usage_when_agent_surfaces_it():
    """Agent backends that surface token usage should populate result.usage."""
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={
            "stopReason": "completed", "content": "hi",
            "usage": {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165},
        },
        notifications=[],
        elapsed=0.1,
        response_format=None,
    )
    assert result["usage"] == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }


def test_aggregate_result_extracts_usage_openai_flavored_keys():
    """Some backends use OpenAI's prompt_tokens/completion_tokens."""
    result = _aggregate_result(
        agent="codex", run_id="r1",
        turn_result={
            "stopReason": "completed", "content": "hi",
            "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        },
        notifications=[],
        elapsed=0.1,
        response_format=None,
    )
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 30
    assert result["usage"]["total_tokens"] == 130  # auto-summed


def test_aggregate_result_falls_back_to_turn_result_content():
    """When no streaming chunks arrived, use the turn result's content."""
    result = _aggregate_result(
        agent="codex", run_id="r2",
        turn_result={
            "stopReason": "completed",
            "content": [{"type": "text", "text": "Done."}],
        },
        notifications=[],
        elapsed=0.1,
        response_format=None,
    )
    assert result["content"] == "Done."


def test_aggregate_result_parses_json_schema_output():
    notifications = [
        {"method": "session/update", "params": {"update": {
            "type": "messageChunk",
            "content": '{"answer": "42"}',
        }}},
    ]
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed"},
        notifications=notifications,
        elapsed=0.2,
        response_format={"type": "json_schema", "schema": {}},
    )
    assert result["parsed"] == {"answer": "42"}


def test_aggregate_result_handles_unparseable_json():
    notifications = [
        {"method": "session/update", "params": {"update": {
            "type": "messageChunk", "content": "not json"}}},
    ]
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed"},
        notifications=notifications,
        elapsed=0.2,
        response_format={"type": "json_schema", "schema": {}},
    )
    assert result["parsed"] is None


# --- MCP server adaptation ---------------------------------------------------


def test_adapt_mcp_servers_http():
    """ACP schema requires `type` discriminator and `headers` (empty list ok)."""
    out = _adapt_mcp_servers([
        {"name": "myproject", "transport": "http", "url": "http://localhost:3001/mcp"},
    ])
    assert out == [{
        "type": "http",
        "name": "myproject",
        "url": "http://localhost:3001/mcp",
        "headers": [],
    }]


def test_adapt_mcp_servers_http_passes_headers_through():
    out = _adapt_mcp_servers([
        {"name": "x", "transport": "http", "url": "http://h/",
         "headers": [{"name": "Authorization", "value": "Bearer t"}]},
    ])
    assert out[0]["headers"] == [{"name": "Authorization", "value": "Bearer t"}]


def test_adapt_mcp_servers_stdio():
    """ACP schema requires `type`, `env` (empty list ok), preserves command + args."""
    out = _adapt_mcp_servers([
        {"name": "local", "transport": "stdio", "command": "uv", "args": ["run", "mcp"]},
    ])
    assert out == [{
        "type": "stdio",
        "name": "local",
        "command": "uv",
        "args": ["run", "mcp"],
        "env": [],
    }]


def test_adapt_mcp_servers_empty():
    assert _adapt_mcp_servers(None) == []
    assert _adapt_mcp_servers([]) == []


def test_adapt_mcp_servers_uses_type_not_transport():
    """Regression: prevent schema drift back to `transport` (ACP -32602)."""
    out = _adapt_mcp_servers([
        {"name": "x", "transport": "http", "url": "http://h/"},
    ])
    assert "transport" not in out[0]
    assert out[0]["type"] == "http"


# --- End-to-end mocked flow --------------------------------------------------


@pytest.mark.asyncio
async def test_call_via_sandbox_orchestrates_full_turn():
    """initialize → session/new → session/prompt → session/close, with one notification."""
    fake_http = MagicMock()

    posts: list[dict] = []

    async def fake_post(url, json=None, headers=None):
        posts.append({"url": url, "envelope": json})
        method = json.get("method")
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if method == "initialize":
            resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": json["id"],
                                                 "result": {"protocolVersion": 1}})
        elif method == "session/new":
            resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": json["id"],
                                                 "result": {"sessionId": "sess-1"}})
        elif method == "session/prompt":
            resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": json["id"],
                                                 "result": {"stopReason": "completed",
                                                            "content": "answer: 42"}})
        else:
            # notifications: 202
            resp.status_code = 202
        return resp

    fake_http.post = AsyncMock(side_effect=fake_post)

    # SSE: emit one chunk and finish
    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            base = '{"method":"session/update","params":{"update":'
            yield f'data: {base}{{"type":"messageChunk","content":"hi "}}}}'
            yield f'data: {base}{{"type":"messageChunk","content":"there"}}}}'

    fake_http.stream = MagicMock(return_value=_FakeStream())

    # Patch AcpClient to use our fake http instead of constructing one
    import aitelier.providers.sandbox_agent as mod
    orig_aenter = mod.AcpClient.__aenter__

    async def patched_aenter(self):
        self._http = fake_http
        self._owns_http = False
        return self

    mod.AcpClient.__aenter__ = patched_aenter
    try:
        result = await call_via_sandbox(
            "claude-code", "what is 6*7?",
            workspace="/tmp/ws",
            run_id="run-x",
            timeout=10,
        )
    finally:
        mod.AcpClient.__aenter__ = orig_aenter

    assert result["status"] == "ok"
    assert result["provider"] == "claude-code"
    assert result["run_id"] == "run-x"
    # Either streamed chunks or turn result content — both are valid
    assert result["content"]
    assert result["finish_reason"] == "completed"

    # Verify the call sequence
    methods = [p["envelope"].get("method") for p in posts]
    assert methods[0] == "initialize"
    assert methods[1] == "session/new"
    assert "session/prompt" in methods


@pytest.mark.asyncio
async def test_call_via_sandbox_returns_timeout_on_overrun():
    import aitelier.providers.sandbox_agent as mod

    async def patched_run(*args, **kwargs):
        import asyncio
        await asyncio.sleep(10)
        return {}

    orig = mod._run_one_turn
    mod._run_one_turn = patched_run
    try:
        result = await call_via_sandbox(
            "claude-code", "anything",
            run_id="run-t",
            timeout=1,
        )
    finally:
        mod._run_one_turn = orig

    assert result["status"] == "error"
    assert result["error_type"] == "Timeout"
    assert result["finish_reason"] == "timeout"


# --- SSE: agent → client requests (permission handshake) -------------------


@pytest.mark.asyncio
async def test_sse_consumer_auto_approves_permission_requests():
    """When the agent sends session/request_permission (a JSON-RPC REQUEST,
    not a notification), the SSE consumer must POST back an 'allow' response
    or the agent will hang forever waiting for permission to invoke a tool."""
    fake_http = MagicMock()

    # SSE delivers one permission request envelope
    permission_req = (
        'data: {"jsonrpc":"2.0","id":42,'
        '"method":"session/request_permission",'
        '"params":{"toolCall":{"name":"mcp__stub__hello"},'
        '"options":[{"optionId":"allow","kind":"allow_once"},'
        '{"optionId":"deny","kind":"deny"}]}}'
    )

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield permission_req

    fake_http.stream = MagicMock(return_value=_Stream())
    posts: list[dict] = []

    async def fake_post(url, json=None, headers=None, timeout=None):
        posts.append({"url": url, "envelope": json})
        resp = MagicMock()
        resp.status_code = 202
        resp.raise_for_status = MagicMock()
        return resp

    fake_http.post = AsyncMock(side_effect=fake_post)

    async with AcpClient("http://x:2468", "claude", http_client=fake_http) as c:
        c.start_stream()
        # Give the consumer a chance to read + respond
        import asyncio
        await asyncio.sleep(0.05)

    # The SSE consumer should have POSTed a response that allows the tool
    responses = [p for p in posts if p["envelope"].get("id") == 42]
    assert responses, "no response sent for permission request"
    body = responses[0]["envelope"]
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 42
    assert "result" in body
    assert body["result"]["outcome"]["outcome"] == "selected"
    assert body["result"]["outcome"]["optionId"] == "allow"


@pytest.mark.asyncio
async def test_sse_consumer_rejects_unsupported_agent_requests():
    """Anything other than permission_request gets -32601 so the agent
    can fall back instead of hanging."""
    fake_http = MagicMock()
    req = (
        'data: {"jsonrpc":"2.0","id":7,'
        '"method":"fs/read_text_file",'
        '"params":{"sessionId":"s","path":"/etc/passwd"}}'
    )

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield req

    fake_http.stream = MagicMock(return_value=_Stream())
    posts: list[dict] = []

    async def fake_post(url, json=None, headers=None, timeout=None):
        posts.append({"envelope": json})
        resp = MagicMock()
        resp.status_code = 202
        resp.raise_for_status = MagicMock()
        return resp

    fake_http.post = AsyncMock(side_effect=fake_post)

    async with AcpClient("http://x:2468", "claude", http_client=fake_http) as c:
        c.start_stream()
        import asyncio
        await asyncio.sleep(0.05)

    responses = [p for p in posts if p["envelope"].get("id") == 7]
    assert responses
    body = responses[0]["envelope"]
    assert "error" in body
    assert body["error"]["code"] == -32601


# --- HTTP-level integration: /v1/agent → call_via_sandbox -------------------


def test_v1_agent_endpoint_routes_through_sandbox(monkeypatch):
    """POST /v1/agent should reach providers.sandbox_agent.call_via_sandbox."""
    from aitelier.server import app
    from fastapi.testclient import TestClient

    captured = {}

    async def fake_call_via_sandbox(name, prompt, **kwargs):
        captured["name"] = name
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.1, "run_id": kwargs.get("run_id", ""),
            "trace_id": kwargs.get("run_id", ""),
            "content": "ok", "parsed": None,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    # The runner imports call_agent at module level — patch both surfaces.
    monkeypatch.setattr("aitelier.providers.sandbox_agent.call_via_sandbox",
                        fake_call_via_sandbox)
    monkeypatch.setattr("aitelier.providers.agent.call_agent", fake_call_via_sandbox)
    monkeypatch.setattr("aitelier.runner.call_agent", fake_call_via_sandbox)

    client = TestClient(app)
    resp = client.post("/v1/agent", json={
        "model": "claude-code",
        "initial_message": "what is 2+2?",
        "timeout": 10,
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["provider"] == "claude-code"
    assert captured["name"] == "claude-code"
    assert captured["prompt"] == "what is 2+2?"


# --- placeholder so the file isn't dependency-heavy without anyio ------------

def test_module_imports():
    """Smoke import to catch syntax errors even when pytest-asyncio isn't installed."""
    import aitelier.providers.sandbox_agent as mod
    assert mod.AcpClient is not None
    assert mod.call_via_sandbox is not None


# Silence ruff unused import in some Python configs
_ = json
