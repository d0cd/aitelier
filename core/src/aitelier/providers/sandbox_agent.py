"""Sandbox Agent (Rivet) provider — runs coding agents via ACP.

Replaces the direct claude-code / codex subprocess paths. All agent calls go
through the Sandbox Agent HTTP server, which speaks ACP (Agent Client Protocol)
to the underlying agent. Sandboxing, env scoping, and event normalization are
the Sandbox Agent's responsibility.

Transport (Sandbox Agent's HTTP wrapping of ACP):
  - POST /v1/acp/{server_id}?agent=<id>  — send JSON-RPC envelope
      • 200 + AcpEnvelope for requests with a result
      • 202 (no body) for notifications
  - GET  /v1/acp/{server_id}             — SSE stream of envelopes
      Notifications (session/update) arrive on the stream during long-running
      prompts; request responses come back synchronously on the POST.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from typing import Any

import httpx

from aitelier.config import get_config
from aitelier.errors import classify_error
from aitelier.observability import end_agent_trace, trace_agent_call

# ACP protocol version we advertise on initialize. Sandbox Agent currently
# tracks Zed's ACP spec; bump when the upstream stabilizes 1.0.
_ACP_PROTOCOL_VERSION = 1


class AcpError(Exception):
    """JSON-RPC error returned by the agent."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AcpClient:
    """One ACP server-session bound to a single agent for the lifetime of a call.

    Owns:
      - a unique `server_id` (Sandbox Agent multiplexes ACP servers by this key)
      - the agent id (claude-code, codex, ...)
      - a background SSE consumer that surfaces `session/update` notifications
      - request-id correlation (not currently used — POSTs are synchronous —
        but kept for completeness when Sandbox Agent moves to async responses)
    """

    def __init__(self, base_url: str, agent: str, *,
                 token: str | None = None,
                 timeout: float = 600.0,
                 http_client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.token = token
        self.timeout = timeout
        self.server_id = str(uuid.uuid4())
        self._ids = itertools.count(1)
        self._notifications: asyncio.Queue[dict] = asyncio.Queue()
        self._sse_task: asyncio.Task | None = None
        self._http = http_client
        self._owns_http = http_client is None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _url(self, *, with_agent: bool = False) -> str:
        url = f"{self.base_url}/v1/acp/{self.server_id}"
        if with_agent:
            url += f"?agent={self.agent}"
        return url

    async def __aenter__(self) -> AcpClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10),
            )
        return self

    async def __aexit__(self, *exc):
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def call(self, method: str, params: dict | None = None,
                   *, first: bool = False) -> Any:
        """Send a JSON-RPC request envelope. Returns the result payload.

        `first=True` adds ?agent=<id> on the URL (required for the first POST
        to a given server_id).
        """
        envelope = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or {},
        }
        url = self._url(with_agent=first)
        resp = await self._http.post(url, json=envelope, headers=self._headers())
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise AcpError(err.get("code", -1), err.get("message", ""), err.get("data"))
        return body.get("result")

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Fire-and-forget JSON-RPC notification (no response expected)."""
        envelope = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        resp = await self._http.post(self._url(), json=envelope, headers=self._headers())
        # 202 Accepted is the documented success
        if resp.status_code not in (200, 202):
            resp.raise_for_status()

    async def _consume_sse(self) -> None:
        """Background task: parse SSE envelopes.

        Three classes of envelopes can arrive:
          - Notifications (method, no id)        → enqueue for the caller
          - Agent → client requests (method+id)  → respond synchronously;
            without responding, agents block waiting for the answer.
            This is how permission requests, fs reads, and terminal calls
            reach us per the ACP spec.
          - Responses to our outgoing requests   → not used today
            (synchronous POST returns them) but handled defensively.
        """
        assert self._http is not None
        try:
            async with self._http.stream("GET", self._url(),
                                         headers=self._headers()) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        env = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    has_id = "id" in env
                    has_method = "method" in env
                    if has_method and not has_id:
                        await self._notifications.put(env)
                    elif has_method and has_id:
                        # Agent is asking us for something. Auto-handle so
                        # the agent doesn't hang.
                        await self._respond_to_agent_request(env)
                    # else: response to a prior request from us — ignored
                    # for now; POST returns sync responses.
        except asyncio.CancelledError:
            raise
        except Exception:
            # Stream may close when the session ends — that's fine.
            pass

    async def _respond_to_agent_request(self, env: dict) -> None:
        """Build a JSON-RPC response for an agent → client request.

        Auto-approves permission asks (allow), rejects fs/terminal/etc. with
        method-not-found. Without responding, agents that advertise
        `permissions: true` (claude, codex, ...) hang on the first tool call.
        """
        req_id = env.get("id")
        method = env.get("method", "")
        params = env.get("params") or {}

        result: Any = None
        error: dict | None = None

        if method == "session/request_permission":
            # ACP outcome shape: {outcome: {outcome: "selected"|"cancelled",
            #                                optionId: "allow"|<denied-id>}}
            options = params.get("options") or []
            allow_id = "allow"
            for opt in options:
                if isinstance(opt, dict):
                    kind = opt.get("kind") or opt.get("optionId") or ""
                    if "allow" in str(kind).lower():
                        allow_id = opt.get("optionId", "allow")
                        break
            result = {"outcome": {"outcome": "selected", "optionId": allow_id}}
        else:
            # Anything else (fs.*, terminal/*, session/*) — politely refuse.
            error = {
                "code": -32601,
                "message": f"client does not implement {method}",
            }

        response: dict = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result

        try:
            assert self._http is not None
            await self._http.post(self._url(), json=response,
                                   headers=self._headers(),
                                   timeout=10.0)
        except Exception:
            # Best-effort. If the POST fails, the agent will eventually
            # time out on its end — same outcome as before this fix.
            pass

    def start_stream(self) -> None:
        """Begin background SSE consumption. Call after first POST has bound
        the server to its agent."""
        if self._sse_task is None:
            self._sse_task = asyncio.create_task(self._consume_sse())

    async def drain_notifications(self) -> list[dict]:
        """Return all currently-buffered notifications without blocking."""
        out: list[dict] = []
        while not self._notifications.empty():
            out.append(self._notifications.get_nowait())
        return out

    async def next_notification(self, timeout: float = 0.1) -> dict | None:
        """Block up to `timeout` seconds for the next notification."""
        try:
            return await asyncio.wait_for(self._notifications.get(), timeout=timeout)
        except TimeoutError:
            return None


# ---------------------------------------------------------------------------
# High-level entry point used by providers/agent.py
# ---------------------------------------------------------------------------


async def call_via_sandbox(
    name: str,
    prompt: str,
    *,
    workspace: str | None = None,
    workspace_mode: str = "copy",  # noqa: ARG001 — Sandbox Agent owns isolation
    system_prompt: str | None = None,
    mcp_servers: list[dict] | None = None,
    tool_allowlist: list[str] | None = None,  # noqa: ARG001 — TODO: wire to set_config_option
    response_format: dict | None = None,
    max_turns: int | None = None,  # noqa: ARG001 — TODO: wire to set_config_option
    timeout: int = 600,
    run_dir: Any = None,  # noqa: ARG001 — kept for signature parity
    run_id: str = "",
    trace_tag: str | None = None,  # noqa: ARG001
) -> dict:
    """Run an agent via Sandbox Agent. Returns aitelier's standard result dict.

    Parameter mapping (M4 in progress — some params not yet plumbed):
      system_prompt    → initialize.clientCapabilities or session config
      mcp_servers      → session/new mcpServers
      tool_allowlist   → session/set_config_option (agent-specific)
      response_format  → session/prompt content block (json_schema)
      max_turns        → session/set_config_option (agent-specific)
      workspace        → session/new working directory
      workspace_mode   → handled inside Sandbox Agent (we don't manage isolation)
    """
    cfg = get_config().sandbox_agent
    start = time.monotonic()

    # Open a Langfuse generation if observability is configured.
    # No-op when LANGFUSE_PUBLIC_KEY is unset.
    _trace, _gen = trace_agent_call(
        task_name=trace_tag or "agent",
        agent_name=name,
        prompt=prompt,
        run_id=run_id,
    )

    try:
        async with AcpClient(cfg.base_url, name, token=cfg.token,
                             timeout=timeout) as client:
            result = await asyncio.wait_for(
                _run_one_turn(
                    client,
                    prompt=prompt,
                    workspace=workspace,
                    system_prompt=system_prompt,
                    mcp_servers=mcp_servers,
                    response_format=response_format,
                    run_id=run_id,
                    start=start,
                ),
                timeout=timeout,
            )
    except TimeoutError:
        result = _timeout_result(name, run_id, time.monotonic() - start)
    except Exception as exc:
        result = _error_result(name, run_id, exc, time.monotonic() - start,
                               base_url=cfg.base_url)

    end_agent_trace(_gen, result.get("content") or "", None, run_id)
    return result


async def _run_one_turn(
    client: AcpClient,
    *,
    prompt: str,
    workspace: str | None,
    system_prompt: str | None,
    mcp_servers: list[dict] | None,
    response_format: dict | None,
    run_id: str,
    start: float,
) -> dict:
    # 1. Handshake
    await client.call("initialize", {
        "protocolVersion": _ACP_PROTOCOL_VERSION,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {"name": "aitelier", "version": "0.1.0"},
    }, first=True)

    # 2. New session
    session_params: dict = {
        "cwd": workspace or ".",
        "mcpServers": _adapt_mcp_servers(mcp_servers),
    }
    session_resp = await client.call("session/new", session_params)
    session_id = session_resp["sessionId"] if isinstance(session_resp, dict) else session_resp

    # 3. Start SSE consumer for notifications during the prompt
    client.start_stream()

    # 4. Optional: set system prompt via config option (agent-specific).
    if system_prompt:
        try:
            await client.notify("session/set_config_option", {
                "sessionId": session_id,
                "option": "systemPrompt",
                "value": system_prompt,
            })
        except Exception:
            # Best-effort — not all agents support this key.
            pass

    # 5. Run the turn (POST blocks until the agent completes a turn)
    prompt_params = {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}],
    }
    if response_format and response_format.get("type") == "json_schema":
        prompt_params["responseFormat"] = response_format

    turn_result = await client.call("session/prompt", prompt_params)

    # 6. Drain any remaining notifications
    notifications = await client.drain_notifications()

    # 7. Close session
    try:
        await client.notify("session/close", {"sessionId": session_id})
    except Exception:
        pass

    elapsed = time.monotonic() - start
    return _aggregate_result(
        agent=client.agent,
        run_id=run_id,
        turn_result=turn_result,
        notifications=notifications,
        elapsed=elapsed,
        response_format=response_format,
    )


# ---------------------------------------------------------------------------
# Streaming entry point used by /v1/agent/stream
# ---------------------------------------------------------------------------


async def call_via_sandbox_stream(
    name: str,
    prompt: str,
    *,
    workspace: str | None = None,
    system_prompt: str | None = None,
    mcp_servers: list[dict] | None = None,
    response_format: dict | None = None,
    timeout: int = 600,  # noqa: ARG001 — outer endpoint enforces timeout
    run_id: str = "",
):
    """Streaming variant of call_via_sandbox.

    Yields events as they arrive from the ACP session/update notifications,
    then a terminal `done` (or `error`) event:

      {"type": "delta",       "content": "..."}
      {"type": "tool_call",   "server": "...", "tool": "...", "input": {...}}
      {"type": "tool_result", "tool": "...", "output": ..., "elapsed_ms": ...}
      {"type": "done",        ... full aggregated Result dict ...}
      {"type": "error",       "error_type": "...", "error_msg": "..."}
    """
    cfg = get_config().sandbox_agent
    start = time.monotonic()

    text_chunks: list[str] = []
    tool_calls: list[dict] = []

    _trace, _gen = trace_agent_call(
        task_name="agent",
        agent_name=name,
        prompt=prompt,
        run_id=run_id,
    )

    try:
        async with AcpClient(cfg.base_url, name, token=cfg.token,
                             timeout=timeout) as client:
            await client.call("initialize", {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                # Advertise only what aitelier actually services in the SSE
                # consumer. fs/terminal handlers would need real
                # implementations; until then say no so the agent doesn't
                # ask and hang waiting for a response.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "aitelier", "version": "0.1.0"},
            }, first=True)

            session_resp = await client.call("session/new", {
                "cwd": workspace or ".",
                "mcpServers": _adapt_mcp_servers(mcp_servers),
            })
            session_id = (
                session_resp["sessionId"]
                if isinstance(session_resp, dict) else session_resp
            )

            client.start_stream()

            if system_prompt:
                try:
                    await client.notify("session/set_config_option", {
                        "sessionId": session_id,
                        "option": "systemPrompt",
                        "value": system_prompt,
                    })
                except Exception:
                    pass

            prompt_params: dict = {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            }
            if response_format and response_format.get("type") == "json_schema":
                prompt_params["responseFormat"] = response_format

            # Run session/prompt in the background; surface notifications live.
            prompt_task = asyncio.create_task(
                client.call("session/prompt", prompt_params)
            )

            while not prompt_task.done():
                note = await client.next_notification(timeout=0.25)
                if note is None:
                    continue
                ev = _notification_to_event(note)
                if ev is None:
                    continue
                if ev["type"] == "delta":
                    text_chunks.append(ev["content"])
                elif ev["type"] == "tool_call":
                    tool_calls.append({
                        "server": ev.get("server"),
                        "tool": ev.get("tool"),
                        "input": ev.get("input"),
                    })
                yield ev

            # Final turn response
            try:
                turn_result = await prompt_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield {
                    "type": "error",
                    "error_type": classify_error(exc),
                    "error_msg": str(exc),
                }
                return

            # Drain any straggling notifications.
            for note in await client.drain_notifications():
                ev = _notification_to_event(note)
                if ev is None:
                    continue
                if ev["type"] == "delta":
                    text_chunks.append(ev["content"])
                yield ev

            try:
                await client.notify("session/close", {"sessionId": session_id})
            except Exception:
                pass

            elapsed = time.monotonic() - start
            done = _aggregate_result(
                agent=client.agent,
                run_id=run_id,
                turn_result=turn_result,
                notifications=[],  # already drained above
                elapsed=elapsed,
                response_format=response_format,
            )
            if text_chunks:
                done["content"] = "".join(text_chunks)
            if tool_calls:
                done["tool_calls"] = tool_calls
            done["type"] = "done"
            end_agent_trace(_gen, done.get("content") or "", None, run_id)
            yield done

    except asyncio.CancelledError:
        end_agent_trace(_gen, "".join(text_chunks), None, run_id)
        raise
    except Exception as exc:
        end_agent_trace(_gen, str(exc), None, run_id)
        yield {
            "type": "error",
            "error_type": classify_error(exc),
            "error_msg": str(exc),
        }


# ---------------------------------------------------------------------------
# Adapters / aggregation
# ---------------------------------------------------------------------------


def _notification_to_event(note: dict) -> dict | None:
    """Map an ACP session/update notification to an aitelier streaming event.

    Returns None when the notification doesn't carry a payload we surface.
    """
    params = note.get("params") or {}
    update = params.get("update") or params
    kind = update.get("type") or update.get("kind")

    if kind in ("messageChunk", "agentMessageChunk", "text"):
        content = update.get("content") or update.get("text")
        if isinstance(content, str):
            return {"type": "delta", "content": content}
        if isinstance(content, dict):
            text = content.get("text")
            if text:
                return {"type": "delta", "content": text}
        return None

    if kind in ("toolCall", "tool_call"):
        return {
            "type": "tool_call",
            "server": update.get("server") or update.get("serverName"),
            "tool": update.get("name") or update.get("toolName"),
            "input": update.get("arguments") or update.get("input"),
        }

    if kind in ("toolResult", "tool_result"):
        return {
            "type": "tool_result",
            "tool": update.get("name") or update.get("toolName"),
            "output": update.get("result") or update.get("output"),
            "elapsed_ms": update.get("elapsed_ms") or update.get("elapsedMs"),
        }

    return None


def _adapt_mcp_servers(servers: list[dict] | None) -> list[dict]:
    """Convert aitelier's MCP server shape to ACP's wire schema.

    aitelier's public API uses `transport: "http" | "stdio"`. ACP's
    schema/schema.json (McpServerHttp / McpServerStdio) requires:
      - `type` as the discriminator (literal const, not `transport`)
      - `headers: [{name, value}]` required on http (empty list is valid)
      - `env: [{name, value}]` required on stdio (empty list is valid)
    """
    if not servers:
        return []
    out: list[dict] = []
    for s in servers:
        transport = s.get("transport", "http")
        if transport == "http":
            out.append({
                "type": "http",
                "name": s["name"],
                "url": s.get("url", ""),
                "headers": s.get("headers", []),
            })
        elif transport == "stdio":
            out.append({
                "type": "stdio",
                "name": s["name"],
                "command": s.get("command", ""),
                "args": s.get("args", []),
                "env": s.get("env", []),
            })
    return out


def _aggregate_result(
    *,
    agent: str,
    run_id: str,
    turn_result: dict | None,
    notifications: list[dict],
    elapsed: float,
    response_format: dict | None,
) -> dict:
    """Convert ACP response + notifications → aitelier's standard result dict."""
    text_chunks: list[str] = []
    tool_calls: list[dict] = []

    for note in notifications:
        ev = _notification_to_event(note)
        if ev is None:
            continue
        if ev["type"] == "delta":
            text_chunks.append(ev["content"])
        elif ev["type"] == "tool_call":
            tool_calls.append({
                "server": ev.get("server"),
                "tool": ev.get("tool"),
                "input": ev.get("input"),
            })

    content = "".join(text_chunks)
    # Final stop_reason from the turn response if present
    finish_reason = "completed"
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if isinstance(turn_result, dict):
        finish_reason = (
            turn_result.get("stopReason")
            or turn_result.get("finish_reason")
            or finish_reason
        )
        # Some agents return the full text in turn_result.content; prefer it if chunks were empty.
        if not content:
            tr_content = turn_result.get("content")
            if isinstance(tr_content, str):
                content = tr_content
            elif isinstance(tr_content, list):
                content = "".join(
                    c.get("text", "") for c in tr_content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
        # Best-effort usage extraction. Different agent backends surface
        # tokens under different keys; we accept either snake or camel case
        # and the OpenAI-flavored {prompt,completion} or our own
        # {input,output} convention.
        raw_usage = turn_result.get("usage")
        if isinstance(raw_usage, dict):
            in_tok = (
                raw_usage.get("input_tokens")
                or raw_usage.get("inputTokens")
                or raw_usage.get("prompt_tokens")
                or raw_usage.get("promptTokens")
                or 0
            )
            out_tok = (
                raw_usage.get("output_tokens")
                or raw_usage.get("outputTokens")
                or raw_usage.get("completion_tokens")
                or raw_usage.get("completionTokens")
                or 0
            )
            tot_tok = (
                raw_usage.get("total_tokens")
                or raw_usage.get("totalTokens")
                or (in_tok + out_tok)
            )
            usage = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": tot_tok,
            }

    parsed = None
    if response_format and response_format.get("type") == "json_schema" and content:
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "kind": "agent",
        "provider": agent,
        "status": "ok",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": content,
        "parsed": parsed,
        "usage": usage,
        "finish_reason": finish_reason,
        "tool_calls": tool_calls,
        "cost_usd": None,  # See docs/INTEGRATION.md → "Cost tracking" for why.
        "error_type": None,
        "error_msg": None,
    }


def _timeout_result(agent: str, run_id: str, elapsed: float) -> dict:
    return {
        "kind": "agent",
        "provider": agent,
        "status": "error",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "timeout",
        "tool_calls": [],
        "cost_usd": None,
        "error_type": "Timeout",
        "error_msg": f"Agent exceeded timeout after {elapsed:.1f}s",
    }


def _error_result(
    agent: str, run_id: str, exc: Exception, elapsed: float,
    *, base_url: str | None = None,
) -> dict:
    """Build an error result dict with a descriptive message.

    Some httpx exceptions (notably ReadTimeout) have an empty str(exc).
    We always include the URL + elapsed time in the message so consumers
    can tell *what* timed out without digging through logs.
    """
    msg = str(exc) or type(exc).__name__
    parts = [msg]
    if base_url:
        parts.append(f"url={base_url}")
    parts.append(f"elapsed={elapsed:.1f}s")
    return {
        "kind": "agent",
        "provider": agent,
        "status": "error",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "error",
        "tool_calls": [],
        "cost_usd": None,
        "error_type": classify_error(exc),
        "error_msg": " | ".join(parts),
    }
