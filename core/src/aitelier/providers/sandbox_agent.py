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
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from aitelier.config import get_config
from aitelier.errors import classify_error

logger = logging.getLogger("aitelier.sandbox_agent")

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
        except Exception as exc:
            # Stream may close when the session ends — common and benign.
            # Log at debug for diagnosability without polluting normal output.
            logger.debug("SSE consumer ended: %s: %s", type(exc).__name__, exc)

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
        except Exception as exc:
            # Best-effort. If the POST fails, the agent will eventually
            # time out on its end.
            logger.debug(
                "agent-response POST failed: %s: %s",
                type(exc).__name__, exc,
            )

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
# Pre-flight validation — surface common remote-SA misconfigurations early
# ---------------------------------------------------------------------------


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _is_local_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def _scrub_sandbox_url(message: str, base_url: str | None) -> str:
    """Replace the literal sandbox base URL with `<sandbox>` in any
    consumer-visible message. The URL is internal topology; surfacing
    it (in error envelopes, event payloads) leaks host shape to remote
    callers in hosted mode and clutters consumer dashboards. The full
    URL stays in server logs and the runs row for operator debugging.
    """
    if not base_url:
        return message
    return message.replace(base_url, "<sandbox>")


def _warn_remote_misconfig(
    sa_base_url: str,
    workspace: str | None,
    mcp_servers: list[dict] | None,
) -> None:
    """Emit warnings when the SA is remote/containerized but the request
    references host-local paths or 127.0.0.1 URLs that won't resolve there.

    Triggered when sandbox_agent.base_url is *not* local. Warnings only —
    we never reject the request, since the user may have set up
    `host.docker.internal` aliases or tunneled the loopback themselves.
    """
    if _is_local_url(sa_base_url):
        return

    for s in mcp_servers or []:
        if s.get("transport", "http") != "http":
            continue
        url = s.get("url") or ""
        if url and _is_local_url(url):
            logger.warning(
                "MCP server %r url=%s points at loopback but Sandbox Agent is "
                "remote (%s); the agent won't be able to reach it. Use "
                "host.docker.internal, a tunneled public URL, or a stdio "
                "transport instead.",
                s.get("name") or "<unnamed>", url, sa_base_url,
            )

    if workspace and workspace.startswith("/") and not workspace.startswith("/workspace"):
        logger.warning(
            "workspace=%s looks like a host path but Sandbox Agent is remote "
            "(%s); the directory likely doesn't exist inside the sandbox. "
            "Use a path under /workspace (the SA default) or seed files via "
            "the `prepare.files` option on /v1/agent.",
            workspace, sa_base_url,
        )


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


async def _persist_sandbox_server_id(
    run_id: str, sandbox_url: str, server_id: str,
) -> None:
    """Stamp the run row with the SA endpoint + server_id so recovery can
    locate the session after an aitelier restart, and so dashboards can
    distinguish local vs remote sandboxes. Best-effort: never blocks the run.
    """
    if not run_id:
        return
    try:
        from aitelier.storage import get_store
        store = await get_store()
        await store.update_run_sandbox(
            run_id,
            sandbox_url=sandbox_url,
            sandbox_server_id=server_id,
            sandbox_backend="local" if _is_local_url(sandbox_url) else "remote",
        )
    except Exception as exc:
        logger.debug(
            "persist sandbox server_id failed for run %s: %s: %s",
            run_id, type(exc).__name__, exc,
        )


class _RunEventEmitter:
    """Tiny helper that appends run_events to the durable store, monotonically.

    Best-effort: if the store hiccups, the run keeps going — observability
    must never block agent execution.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._seq = 0

    async def emit(self, kind: str, payload: dict | None = None) -> None:
        from aitelier.storage import RunEvent, get_store
        self._seq += 1
        try:
            store = await get_store()
            await store.append_event(RunEvent(
                run_id=self.run_id, seq=self._seq, kind=kind,
                payload=payload or {},
            ))
        except Exception as exc:
            # Event-append is observability, not load-bearing. Don't fail
            # the run if storage is degraded — surface in logs instead.
            logger.debug(
                "event emit failed for run %s kind=%s: %s: %s",
                self.run_id, kind, type(exc).__name__, exc,
            )


async def _open_acp_session(
    client: AcpClient,
    *,
    workspace: str | None,
    mcp_servers: list[dict] | None,
    system_prompt: str | None,
    agent_model: str | None,
    tool_allowlist: list[str] | None,
    max_turns: int | None,
    run_id: str = "",
) -> str:
    """Drive the ACP handshake up to a usable session_id.

    initialize → session/new → start_stream → best-effort config notifications.

    Capabilities are advertised honestly: aitelier doesn't service fs/*
    or terminal/* (the SSE consumer rejects them in `_respond_to_agent_request`),
    so we say `false`. The agent then never asks, instead of asking and
    hanging on the rejection.

    When `run_id` is non-empty, an `<aitelier_context>` block carrying
    the run_id is prepended to the system prompt so the inner agent
    can pass `parent_run_id` when dispatching subagents through aitelier.
    """
    await client.call("initialize", {
        "protocolVersion": _ACP_PROTOCOL_VERSION,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": "aitelier", "version": "0.1.0"},
    }, first=True)

    if run_id:
        ctx_block = (
            f"<aitelier_context>\n"
            f"run_id={run_id}\n"
            f"</aitelier_context>"
        )
        system_prompt = (
            f"{ctx_block}\n\n{system_prompt}" if system_prompt else ctx_block
        )

    session_resp = await client.call("session/new", {
        "cwd": workspace or ".",
        "mcpServers": _adapt_mcp_servers(mcp_servers, run_id=run_id),
    })
    session_id = (
        session_resp["sessionId"]
        if isinstance(session_resp, dict) else session_resp
    )

    client.start_stream()

    # Best-effort config options. Agent backends accept different keys
    # (claude-code: model, systemPrompt, allowedTools, maxTurns; codex:
    # similar). Failures here are silent on purpose — sandbox-agent passes
    # the option through and the backend ignores unknowns.
    for option, value in (
        ("systemPrompt", system_prompt),
        ("model", agent_model),
        ("allowedTools", tool_allowlist),
        ("maxTurns", max_turns),
    ):
        if value is None or value == "" or value == []:
            continue
        try:
            await client.notify("session/set_config_option", {
                "sessionId": session_id,
                "option": option,
                "value": value,
            })
        except Exception as exc:
            logger.debug(
                "session/set_config_option %s ignored: %s: %s",
                option, type(exc).__name__, exc,
            )

    return session_id


async def _close_acp_session(client: AcpClient, session_id: str) -> None:
    """Tear down an ACP session.

    Uses `client.call` (request/response) rather than `client.notify`
    (fire-and-forget) so the server acknowledges receipt before our HTTP
    connection closes. Without the round-trip, sandbox-agent can drop
    the close message on connection teardown and leave child agent
    processes alive indefinitely.

    Failures are swallowed and logged: this runs from cleanup paths
    where re-raising would mask the original error. A short timeout
    keeps a stuck server from hanging the cleanup.
    """
    try:
        await asyncio.wait_for(
            client.call("session/close", {"sessionId": session_id}),
            timeout=5.0,
        )
    except Exception as exc:
        logger.warning(
            "session/close failed for session %s: %s — child process "
            "may leak. Check sandbox-agent state.",
            session_id, exc,
        )


def _prompt_params(
    session_id: str, prompt: str, response_format: dict | None,
) -> dict:
    """Build the session/prompt request body."""
    params: dict = {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}],
    }
    if response_format and response_format.get("type") == "json_schema":
        params["responseFormat"] = response_format
    return params


async def call_via_sandbox(
    name: str,
    prompt: str,
    *,
    workspace: str | None = None,
    system_prompt: str | None = None,
    mcp_servers: list[dict] | None = None,
    tool_allowlist: list[str] | None = None,
    response_format: dict | None = None,
    max_turns: int | None = None,
    agent_model: str | None = None,
    timeout: int = 600,
    run_id: str = "",
) -> dict:
    """Run an agent via Sandbox Agent. Returns aitelier's standard result dict.

    Thin wrapper over call_via_sandbox_stream: consumes the event stream and
    returns the final aggregated `done` (or surfaces error/timeout) as a dict.

    Parameter routing:
      system_prompt, agent_model, tool_allowlist, max_turns → session/set_config_option
      mcp_servers      → session/new mcpServers
      response_format  → session/prompt (json_schema only)
      workspace        → session/new cwd
    """
    start = time.monotonic()
    final: dict | None = None
    last_error: dict | None = None
    try:
        async def _consume() -> None:
            nonlocal final, last_error
            async for event in call_via_sandbox_stream(
                name, prompt,
                workspace=workspace, system_prompt=system_prompt,
                mcp_servers=mcp_servers, tool_allowlist=tool_allowlist,
                response_format=response_format, max_turns=max_turns,
                agent_model=agent_model, timeout=timeout, run_id=run_id,
            ):
                etype = event.get("type")
                if etype == "done":
                    final = {k: v for k, v in event.items() if k != "type"}
                elif etype == "error":
                    last_error = event

        await asyncio.wait_for(_consume(), timeout=timeout)
    except TimeoutError:
        return _timeout_result(name, run_id, time.monotonic() - start)

    if final is not None:
        return final
    if last_error is not None:
        # Reconstruct a full Result from the streamed error event. The
        # producer already classified the exception via `classify_error`
        # before serializing it into the event — preserve that type
        # instead of re-wrapping as `RuntimeError`, which would lose the
        # taxonomy and surface raw class names to consumers.
        cfg = get_config().sandbox_agent
        exc_msg = last_error.get("error_msg") or "stream error"
        return _error_result(
            name, run_id, RuntimeError(exc_msg),
            time.monotonic() - start, base_url=cfg.base_url,
            error_type=last_error.get("error_type"),
        )
    cfg = get_config().sandbox_agent
    return _error_result(
        name, run_id, RuntimeError("stream ended without a result"),
        time.monotonic() - start, base_url=cfg.base_url,
        error_type="ProviderError",
    )


# ---------------------------------------------------------------------------
# Streaming entry point used by /v1/agent/stream and call_via_sandbox
# ---------------------------------------------------------------------------


async def _translate_note(
    note: dict, *,
    text_chunks: list[str], tool_calls: list[dict],
    emitter: _RunEventEmitter, live: bool,
) -> dict | None:
    """Map one ACP notification → an aitelier event, with side effects.

    Mutates `text_chunks` / `tool_calls` so the caller can aggregate
    them into the final `done` payload, and emits an event to
    `emitter`. Returns the event dict (for the caller to yield) or
    None when the notification doesn't map to a surfaced event.

    `live=False` is used by the post-prompt drain pass: tool_call /
    tool_result notifications during drain are not added to
    `tool_calls` (they would already have been counted live) — only
    `delta` content is accumulated so a trailing message-chunk
    completes the visible output.
    """
    ev = _notification_to_event(note)
    if ev is None:
        return None
    if ev["type"] == "delta":
        text_chunks.append(ev["content"])
    elif live and ev["type"] in ("tool_call", "tool_result"):
        tool_calls.append({k: v for k, v in ev.items() if k != "type"})
    await emitter.emit(
        ev["type"], {k: v for k, v in ev.items() if k != "type"},
    )
    return ev


async def call_via_sandbox_stream(
    name: str,
    prompt: str,
    *,
    workspace: str | None = None,
    system_prompt: str | None = None,
    mcp_servers: list[dict] | None = None,
    tool_allowlist: list[str] | None = None,
    response_format: dict | None = None,
    max_turns: int | None = None,
    agent_model: str | None = None,
    timeout: int = 600,
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

    _warn_remote_misconfig(cfg.base_url, workspace, mcp_servers)

    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    emitter = _RunEventEmitter(run_id)
    # `sandbox_url` is internal topology — keep it out of consumer-visible
    # event payloads so it can't end up in dashboards or leak to remote
    # callers in hosted mode. The full URL stays in the runs row
    # (`sandbox_url` column) for operator debugging.
    await emitter.emit("start", {
        "agent": name,
        "sandbox": "local" if _is_local_url(cfg.base_url) else "remote",
        "workspace": workspace,
    })

    try:
        async with AcpClient(cfg.base_url, name, token=cfg.token,
                             timeout=timeout) as client:
            await _persist_sandbox_server_id(run_id, cfg.base_url, client.server_id)
            session_id: str | None = None
            try:
                session_id = await _open_acp_session(
                    client,
                    workspace=workspace, mcp_servers=mcp_servers,
                    system_prompt=system_prompt, agent_model=agent_model,
                    tool_allowlist=tool_allowlist, max_turns=max_turns,
                    run_id=run_id,
                )
                prompt_task = asyncio.create_task(client.call(
                    "session/prompt",
                    _prompt_params(session_id, prompt, response_format),
                ))

                turn_result: dict | None = None
                prompt_err: dict | None = None
                try:
                    # Live phase: pump notifications until session/prompt
                    # completes. Tool events here count toward tool_calls.
                    while not prompt_task.done():
                        note = await client.next_notification(timeout=0.25)
                        if note is None:
                            continue
                        ev = await _translate_note(
                            note, text_chunks=text_chunks,
                            tool_calls=tool_calls, emitter=emitter, live=True,
                        )
                        if ev is not None:
                            yield ev

                    try:
                        turn_result = await prompt_task
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        prompt_err = {
                            "type": "error",
                            "error_type": classify_error(exc),
                            "error_msg": _scrub_sandbox_url(str(exc), cfg.base_url),
                        }

                    # Drain phase: surface trailing notifications even on
                    # prompt error, so the event stream is consistent.
                    # Tool events were already counted live; only delta
                    # content accumulates here.
                    for note in await client.drain_notifications():
                        ev = await _translate_note(
                            note, text_chunks=text_chunks,
                            tool_calls=tool_calls, emitter=emitter, live=False,
                        )
                        if ev is not None:
                            yield ev
                finally:
                    # Close session on every exit path — cancellation, prompt
                    # error, successful completion. Without this the inner
                    # agent process stays alive indefinitely.
                    await _close_acp_session(client, session_id)
                    session_id = None

                if prompt_err is not None:
                    await emitter.emit("error", prompt_err)
                    yield prompt_err
                    return

                yield await _build_done_event(
                    client=client, run_id=run_id, start=start,
                    turn_result=turn_result, response_format=response_format,
                    text_chunks=text_chunks, tool_calls=tool_calls,
                    emitter=emitter,
                )
            finally:
                # Race-window guard: if cancellation hit between
                # _close_acp_session() and `session_id = None` above,
                # session_id stays non-None and we retry the close here.
                if session_id is not None:
                    await _close_acp_session(client, session_id)

    except asyncio.CancelledError:
        await emitter.emit("cancelled", {})
        raise
    except Exception as exc:
        err = {
            "type": "error",
            "error_type": classify_error(exc),
            "error_msg": _scrub_sandbox_url(str(exc), cfg.base_url),
        }
        await emitter.emit("error", err)
        yield err


async def _build_done_event(
    *, client, run_id: str, start: float,
    turn_result: dict | None, response_format: dict | None,
    text_chunks: list[str], tool_calls: list[dict],
    emitter: _RunEventEmitter,
) -> dict:
    """Assemble the terminal `done` event from turn_result + accumulators.

    Lives in its own function because the streaming entry point's
    happy-path tail was the densest part of `call_via_sandbox_stream` —
    aggregator call + accumulator merge + finish emit. Pulling it out
    keeps the orchestration loop readable."""
    done = _aggregate_result(
        agent=client.agent,
        run_id=run_id,
        turn_result=turn_result,
        elapsed=time.monotonic() - start,
        response_format=response_format,
    )
    if text_chunks:
        done["content"] = "".join(text_chunks)
    if tool_calls:
        done["tool_calls"] = tool_calls
    done["type"] = "done"
    await emitter.emit("finish", {
        "finish_reason": done.get("finish_reason"),
        "tool_call_count": len(tool_calls),
    })
    return done


# ---------------------------------------------------------------------------
# Adapters / aggregation
# ---------------------------------------------------------------------------


def _notification_to_event(note: dict) -> dict | None:
    """Map an ACP session/update notification to an aitelier streaming event.

    The discriminator per the ACP `SessionUpdate` schema is `sessionUpdate`
    with snake_case values (`agent_message_chunk`, `tool_call`,
    `tool_call_update`, …). Older sandbox-agent versions emitted camelCase
    aliases (`messageChunk`, `toolCall`); we accept both.
    """
    params = note.get("params") or {}
    update = params.get("update") or params
    kind = (
        update.get("sessionUpdate")
        or update.get("type")
        or update.get("kind")
    )

    if kind in (
        "agent_message_chunk", "user_message_chunk",
        "agentMessageChunk", "messageChunk", "text",
    ):
        content = update.get("content") or update.get("text")
        if isinstance(content, dict):
            content = content.get("text")
        if isinstance(content, str):
            return {"type": "delta", "content": content}
        return None

    if kind == "agent_thought_chunk":
        content = update.get("content")
        if isinstance(content, dict):
            content = content.get("text")
        if isinstance(content, str):
            return {"type": "thought", "content": content}
        return None

    if kind in ("tool_call", "toolCall"):
        return {
            "type": "tool_call",
            "server": update.get("server") or update.get("serverName"),
            "tool":   update.get("name") or update.get("toolName") or update.get("title"),
            "input":  update.get("arguments") or update.get("input") or update.get("rawInput"),
        }

    if kind in ("tool_call_update", "toolCallUpdate", "toolResult", "tool_result"):
        return {
            "type":       "tool_result",
            "tool":       update.get("name") or update.get("toolName"),
            "output":     (update.get("result") or update.get("output")
                            or update.get("rawOutput") or update.get("content")),
            "elapsed_ms": update.get("elapsed_ms") or update.get("elapsedMs"),
        }

    return None


def _adapt_mcp_servers(
    servers: list[dict] | None, *, run_id: str = "",
) -> list[dict]:
    """Convert aitelier's MCP server shape to ACP's wire schema.

    aitelier's public API uses `transport: "http" | "stdio"`. ACP's
    schema/schema.json (McpServerHttp / McpServerStdio) requires:
      - `type` as the discriminator (literal const, not `transport`)
      - `headers: [{name, value}]` required on http (empty list is valid)
      - `env: [{name, value}]` required on stdio (empty list is valid)

    When `run_id` is non-empty, AITELIER_RUN_ID is injected into every
    stdio server's env (unless the caller already set it). This lets
    aitelier-mcp expose the parent's run_id to the inner agent so it
    can dispatch subagents with the correct parent_run_id without
    out-of-band coordination.
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
            env = list(s.get("env", []))
            if run_id and not any(
                e.get("name") == "AITELIER_RUN_ID" for e in env
            ):
                env.append({"name": "AITELIER_RUN_ID", "value": run_id})
            out.append({
                "type": "stdio",
                "name": s["name"],
                "command": s.get("command", ""),
                "args": s.get("args", []),
                "env": env,
            })
    return out


def _aggregate_result(
    *,
    agent: str,
    run_id: str,
    turn_result: dict | None,
    elapsed: float,
    response_format: dict | None,
) -> dict:
    """Build the final aitelier result dict from an ACP turn response.

    The streaming caller already drains notifications inline; we don't
    re-process them here. This function exists to centralise turn_result
    → result conversion (finish_reason, usage extraction, content
    fallback, json_schema parsing) so streaming and non-streaming
    paths produce identical shapes.
    """
    content = ""
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
        "tool_calls": [],
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
    error_type: str | None = None,
) -> dict:
    """Build an error result dict with a descriptive message.

    Some httpx exceptions (notably ReadTimeout) have an empty str(exc).
    We always include a sandbox locator + elapsed time so consumers can
    tell *what* timed out without digging through logs — symbolic
    `sandbox=local|remote` rather than the literal URL, since the URL is
    internal topology that shouldn't appear in error payloads visible to
    remote callers.

    `error_type` lets callers preserve a previously-classified type
    (e.g. one already derived from a streamed `error` event inside the
    producer). Without that override we'd re-classify a generic wrapper
    exception and lose the original taxonomy, leaking `RuntimeError` /
    `ReadTimeout` etc. to consumers instead of the documented vocabulary.
    """
    msg = _scrub_sandbox_url(str(exc) or type(exc).__name__, base_url)
    parts = [msg]
    if base_url:
        parts.append(
            "sandbox=" + ("local" if _is_local_url(base_url) else "remote"),
        )
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
        "error_type": error_type or classify_error(exc),
        "error_msg": " | ".join(parts),
    }
