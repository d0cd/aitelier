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


def test_error_result_includes_symbolic_sandbox_and_elapsed():
    """Error payloads surface `sandbox=local|remote` rather than the
    literal sandbox URL — the URL is internal topology and shouldn't
    leak to remote callers / consumer dashboards. The full URL stays
    in the runs row for operator debugging."""
    from aitelier.providers.sandbox_agent import _error_result
    r = _error_result("claude", "r1", Exception(""), 60.5,
                       base_url="http://localhost:2468")
    assert "http://localhost:2468" not in r["error_msg"]
    assert "sandbox=local" in r["error_msg"]
    assert "elapsed=60.5s" in r["error_msg"]
    # Even when str(exc) is empty, error_msg is non-empty
    assert r["error_msg"]


def test_error_result_scrubs_url_embedded_in_exception_text():
    """When the underlying httpx exception text already contains the URL
    (e.g. 'Client error 400 for url http://127.0.0.1:2468/...'),
    `_error_result` replaces it with `<sandbox>` so consumers don't see
    the literal URL even via the wrapped exception message."""
    from aitelier.providers.sandbox_agent import _error_result
    exc = Exception(
        "Client error '400 Bad Request' for url "
        "'http://127.0.0.1:2468/v1/acp/abc?agent=x'",
    )
    r = _error_result("claude", "r1", exc, 1.0,
                       base_url="http://127.0.0.1:2468")
    assert "127.0.0.1:2468" not in r["error_msg"]
    assert "<sandbox>" in r["error_msg"]


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


def test_aggregate_result_uses_turn_content_string():
    """turn_result.content as plain string → result.content."""
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed", "content": "Hello world"},
        elapsed=1.5,
        response_format=None,
    )
    assert result["status"] == "ok"
    assert result["content"] == "Hello world"
    assert result["finish_reason"] == "completed"
    assert result["provider"] == "claude-code"
    assert result["run_id"] == "r1"


def test_notification_to_event_acp_discriminator():
    """ACP notifications use `sessionUpdate` with snake_case values. The
    per-notification mapper translates them to aitelier's event shape;
    the streaming caller aggregates the events into content/tool_calls.
    """
    from aitelier.providers.sandbox_agent import _notification_to_event
    note = {"method": "session/update", "params": {"update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Hello"}}}}
    ev = _notification_to_event(note)
    assert ev == {"type": "delta", "content": "Hello"}

    note = {"method": "session/update", "params": {"update": {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": "thinking..."}}}}
    ev = _notification_to_event(note)
    assert ev == {"type": "thought", "content": "thinking..."}

    note = {"method": "session/update", "params": {"update": {
        "sessionUpdate": "tool_call", "name": "Read",
        "rawInput": {"path": "/tmp/x"}}}}
    ev = _notification_to_event(note)
    assert ev["type"] == "tool_call"
    assert ev["tool"] == "Read"
    assert ev["input"] == {"path": "/tmp/x"}


def test_notification_to_event_camelcase_toolcall():
    """Older sandbox-agent versions emitted `type: toolCall`; mapper must
    accept both that and the spec-compliant `sessionUpdate: tool_call`."""
    from aitelier.providers.sandbox_agent import _notification_to_event
    note = {"method": "session/update", "params": {"update": {
        "type": "toolCall",
        "name": "Read",
        "arguments": {"path": "/tmp/foo"},
    }}}
    ev = _notification_to_event(note)
    assert ev["tool"] == "Read"
    assert ev["input"] == {"path": "/tmp/foo"}


def test_aggregate_result_extracts_usage_when_agent_surfaces_it():
    """Agent backends that surface token usage should populate result.usage."""
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={
            "stopReason": "completed", "content": "hi",
            "usage": {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165},
        },
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
        elapsed=0.1,
        response_format=None,
    )
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 30
    assert result["usage"]["total_tokens"] == 130  # auto-summed


def test_aggregate_result_falls_back_to_turn_result_content_list():
    """turn_result.content as list of text blocks → joined string."""
    result = _aggregate_result(
        agent="codex", run_id="r2",
        turn_result={
            "stopReason": "completed",
            "content": [{"type": "text", "text": "Done."}],
        },
        elapsed=0.1,
        response_format=None,
    )
    assert result["content"] == "Done."


def test_aggregate_result_parses_json_schema_output():
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed",
                      "content": '{"answer": "42"}'},
        elapsed=0.2,
        response_format={"type": "json_schema", "schema": {}},
    )
    assert result["parsed"] == {"answer": "42"}


def test_aggregate_result_handles_unparseable_json():
    result = _aggregate_result(
        agent="claude-code", run_id="r1",
        turn_result={"stopReason": "completed", "content": "not json"},
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
    """ACP requires `type` as the discriminator, not `transport`."""
    out = _adapt_mcp_servers([
        {"name": "x", "transport": "http", "url": "http://h/"},
    ])
    assert "transport" not in out[0]
    assert out[0]["type"] == "http"


def test_adapt_mcp_servers_injects_run_id_into_stdio_env():
    """AITELIER_RUN_ID injected into stdio servers when run_id is provided."""
    out = _adapt_mcp_servers(
        [{"name": "aitelier", "transport": "stdio", "command": "aitelier-mcp"}],
        run_id="run-abc-123",
    )
    assert out[0]["env"] == [
        {"name": "AITELIER_RUN_ID", "value": "run-abc-123"},
    ]


def test_adapt_mcp_servers_does_not_override_existing_run_id():
    """Caller-provided AITELIER_RUN_ID wins over the auto-injected value."""
    out = _adapt_mcp_servers(
        [{"name": "x", "transport": "stdio", "command": "c",
          "env": [{"name": "AITELIER_RUN_ID", "value": "custom"}]}],
        run_id="auto",
    )
    assert out[0]["env"] == [{"name": "AITELIER_RUN_ID", "value": "custom"}]


def test_adapt_mcp_servers_skips_injection_for_http_servers():
    """HTTP servers run out-of-process; env injection is stdio-only."""
    out = _adapt_mcp_servers(
        [{"name": "x", "transport": "http", "url": "http://h/"}],
        run_id="run-id",
    )
    assert "env" not in out[0]


def test_adapt_mcp_servers_no_injection_when_run_id_empty():
    """Backward-compat: empty run_id leaves env untouched."""
    out = _adapt_mcp_servers(
        [{"name": "x", "transport": "stdio", "command": "c"}],
    )
    assert out[0]["env"] == []


@pytest.mark.asyncio
async def test_open_acp_session_raises_classified_error_on_missing_sessionId():
    """ACP backends that respond to session/new without a sessionId must
    surface a classified ProviderError, not a bare KeyError. Regression
    test for the agent:mock backend reported by dispatcher 2026-05-18."""
    from aitelier.providers.sandbox_agent import AcpError, _open_acp_session

    class _FakeClient:
        agent = "mock"
        async def call(self, method, params, first=False):
            if method == "initialize":
                return {}
            if method == "session/new":
                return {"agentName": "mock"}  # no sessionId!
            return None
        def start_stream(self):
            pass
        async def notify(self, *a, **k):
            pass

    with pytest.raises(AcpError, match="sessionId"):
        await _open_acp_session(
            _FakeClient(),
            workspace=None, mcp_servers=None,
            system_prompt=None, agent_model=None,
            tool_allowlist=None, max_turns=None,
            run_id="r-1",
        )


# --- Remote-SA misconfiguration warnings -------------------------------------


def test_warn_remote_misconfig_silent_when_sa_is_local(caplog):
    from aitelier.providers.sandbox_agent import _warn_remote_misconfig
    with caplog.at_level("WARNING"):
        _warn_remote_misconfig(
            "http://localhost:2468",
            workspace="/Users/me/proj",
            mcp_servers=[{"name": "x", "transport": "http",
                          "url": "http://127.0.0.1:3001/mcp"}],
        )
    assert caplog.records == []


def test_warn_remote_misconfig_flags_loopback_mcp_url_when_sa_remote(caplog):
    from aitelier.providers.sandbox_agent import _warn_remote_misconfig
    with caplog.at_level("WARNING"):
        _warn_remote_misconfig(
            "https://sa.example.com",
            workspace=None,
            mcp_servers=[{"name": "my-mcp", "transport": "http",
                          "url": "http://127.0.0.1:3001/mcp"}],
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("my-mcp" in m and "loopback" in m for m in msgs)


def test_warn_remote_misconfig_flags_host_workspace_when_sa_remote(caplog):
    from aitelier.providers.sandbox_agent import _warn_remote_misconfig
    with caplog.at_level("WARNING"):
        _warn_remote_misconfig(
            "https://sa.example.com",
            workspace="/Users/me/projects/foo",
            mcp_servers=None,
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("workspace" in m and "host path" in m for m in msgs)


def test_warn_remote_misconfig_allows_workspace_prefix(caplog):
    """/workspace/* is the SA-internal default — don't warn on it."""
    from aitelier.providers.sandbox_agent import _warn_remote_misconfig
    with caplog.at_level("WARNING"):
        _warn_remote_misconfig(
            "https://sa.example.com",
            workspace="/workspace/foo",
            mcp_servers=None,
        )
    assert all("workspace" not in r.getMessage() or "host path" not in r.getMessage()
                for r in caplog.records)


def test_warn_remote_misconfig_ignores_stdio_mcp(caplog):
    """stdio MCP servers can't have loopback URLs — they run inside the sandbox."""
    from aitelier.providers.sandbox_agent import _warn_remote_misconfig
    with caplog.at_level("WARNING"):
        _warn_remote_misconfig(
            "https://sa.example.com",
            workspace=None,
            mcp_servers=[{"name": "fs", "transport": "stdio",
                          "command": "mcp-fs"}],
        )
    assert caplog.records == []


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


async def _patch_acp_aenter_with_fake_http(monkeypatch, fake_http) -> None:
    import aitelier.providers.sandbox_agent as mod

    async def patched_aenter(self):
        self._http = fake_http
        self._owns_http = False
        return self

    monkeypatch.setattr(mod.AcpClient, "__aenter__", patched_aenter)


def _fake_sse_stream(*lines: str):
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        def raise_for_status(self): pass
        async def aiter_lines(self):
            for line in lines:
                yield line
    return _S()


@pytest.mark.asyncio
async def test_call_via_sandbox_closes_session_when_prompt_fails(monkeypatch):
    """Session/close must run even when session/prompt itself errors out.
    The bug this guards against: under the prior code path, a prompt
    failure short-circuited the function (`yield err; return`) before
    reaching the close, leaving the inner agent's child process alive
    indefinitely. We accumulated 177 leaked claude SDK subprocesses
    consuming 578% combined CPU before the leak was diagnosed."""
    posts: list[dict] = []
    fake_http = MagicMock()

    async def fake_post(url, json=None, headers=None):
        posts.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        m = json.get("method")
        if m == "initialize":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"],
                "result": {"protocolVersion": 1},
            })
        elif m == "session/new":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"],
                "result": {"sessionId": "sess-leak-1"},
            })
        elif m == "session/prompt":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"],
                "error": {"code": -32603, "message": "agent crashed"},
            })
        elif m == "session/close":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"], "result": {},
            })
        else:
            resp.status_code = 202
        return resp

    fake_http.post = AsyncMock(side_effect=fake_post)
    fake_http.stream = MagicMock(return_value=_fake_sse_stream())

    await _patch_acp_aenter_with_fake_http(monkeypatch, fake_http)
    result = await call_via_sandbox(
        "claude-code", "boom", run_id="run-leak", timeout=10,
    )

    assert result["status"] == "error"
    methods = [p.get("method") for p in posts]
    assert "session/close" in methods, (
        "session/close must fire on the prompt-error path; "
        f"observed: {methods}"
    )


@pytest.mark.asyncio
async def test_call_via_sandbox_closes_session_on_cancellation(monkeypatch):
    """Same invariant on cancellation: when the consumer cancels mid-run,
    the session must still be closed to release the inner agent."""
    import asyncio
    posts: list[dict] = []
    fake_http = MagicMock()

    async def fake_post(url, json=None, headers=None):
        posts.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        m = json.get("method")
        if m == "initialize":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"],
                "result": {"protocolVersion": 1},
            })
        elif m == "session/new":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"],
                "result": {"sessionId": "sess-cancel-1"},
            })
        elif m == "session/prompt":
            # Hang the prompt long enough for the outer task to cancel us.
            await asyncio.sleep(10)
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"],
                "result": {"stopReason": "completed"},
            })
        elif m == "session/close":
            resp.json = MagicMock(return_value={
                "jsonrpc": "2.0", "id": json["id"], "result": {},
            })
        else:
            resp.status_code = 202
        return resp

    fake_http.post = AsyncMock(side_effect=fake_post)
    fake_http.stream = MagicMock(return_value=_fake_sse_stream())

    await _patch_acp_aenter_with_fake_http(monkeypatch, fake_http)

    from aitelier.providers.sandbox_agent import call_via_sandbox_stream

    async def consumer():
        async for _ in call_via_sandbox_stream(
            "claude-code", "long task",
            run_id="run-cancel", timeout=30,
        ):
            pass

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    methods = [p.get("method") for p in posts]
    assert "session/close" in methods, (
        "session/close must fire on the cancellation path; "
        f"observed: {methods}"
    )


@pytest.mark.asyncio
async def test_call_via_sandbox_returns_timeout_on_overrun(monkeypatch):
    """`call_via_sandbox` consumes the stream and enforces an overall timeout;
    a stream that never yields a terminal event must surface a Timeout result."""
    async def slow_stream(*args, **kwargs):
        import asyncio
        await asyncio.sleep(10)
        yield {"type": "done"}  # pragma: no cover — never reached

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox_stream",
        slow_stream,
    )
    result = await call_via_sandbox(
        "claude-code", "anything",
        run_id="run-t",
        timeout=1,
    )

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


# --- HTTP integration: /v1/chat/completions → call_via_sandbox -------------


def test_chat_completions_agent_route_calls_sandbox(monkeypatch):
    """`model: agent:<backend>` routes through providers.sandbox_agent."""
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
            "content": "ok",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr("aitelier.providers.sandbox_agent.call_via_sandbox",
                        fake_call_via_sandbox)

    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={
        "model": "agent:claude-code",
        "messages": [{"role": "user", "content": "what is 2+2?"}],
        "timeout": 10,
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "ok"
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
