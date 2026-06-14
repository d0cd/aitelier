"""ACP-over-HTTP transport — the AcpClient and supporting helpers.

Sandbox Agent wraps the Agent Client Protocol over HTTP:
  - POST /v1/acp/{server_id}?agent=<id>  — send JSON-RPC envelope
      • 200 + AcpEnvelope for requests with a result
      • 202 (no body) for notifications
  - GET  /v1/acp/{server_id}             — SSE stream of envelopes
      Notifications (session/update) arrive on the stream during
      long-running prompts; request responses come back synchronously
      on the POST.

This module owns the wire layer — building/parsing JSON-RPC envelopes,
auto-responding to agent→client requests, scrubbing URLs out of error
messages, and the local/remote SA preflight warnings. Higher-level
session orchestration (open/close session, prompt drive, event
translation) lives in `providers/sandbox_agent.py`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("aitelier.sandbox_agent")

# ACP protocol version we advertise on initialize. Sandbox Agent currently
# tracks Zed's ACP spec; bump when the upstream stabilizes 1.0.
ACP_PROTOCOL_VERSION = 1


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
# URL helpers + pre-flight warnings
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
            "the `aitelier.prepare.files` option on /v1/chat/completions.",
            workspace, sa_base_url,
        )


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
