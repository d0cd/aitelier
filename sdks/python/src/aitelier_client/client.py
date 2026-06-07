"""Async HTTP client for the aitelier service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from aitelier_client._generated.models import (
    ActiveRuns,
    CancelAck,
    Discovery,
    Result,
    TaskSpec,
    TraceRecord,
)


class Aitelier:
    """Client for the aitelier HTTP service.

    Usage:
        async with Aitelier() as client:
            result = await client.complete(model="claude-sonnet", messages=[...])
    """

    def __init__(
        self,
        base_url: str = "http://localhost:7777",
        timeout: float = 600,
        *,
        default_correlation_id: str | None = None,
        api_key: str | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._default_cid = default_correlation_id
        self._api_key = api_key

    def _cid_header(self, correlation_id: str | None) -> dict[str, str]:
        """Per-request override for correlation_id. Auth header is set once
        on the underlying client so every request carries it."""
        cid = correlation_id or self._default_cid
        return {"X-Correlation-Id": cid} if cid else {}

    def _default_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def __aenter__(self) -> Aitelier:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout, connect=10),
            headers=self._default_headers(),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if not self._client:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout, connect=10),
                headers=self._default_headers(),
            )
        return self._client

    # --- Primitives (deepread contract) ---

    async def complete(
        self,
        model: str,
        messages: list[dict],
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        timeout: int | None = None,
        trace_tag: str | None = None,
        correlation_id: str | None = None,
    ) -> Result:
        """Single-shot chat completion."""
        client = self._ensure_client()
        body: dict[str, Any] = {"model": model, "messages": messages}
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if timeout is not None:
            body["timeout"] = timeout
        if trace_tag is not None:
            body["trace_tag"] = trace_tag

        resp = await client.post("/v1/complete", json=body,
                                  headers=self._cid_header(correlation_id))
        resp.raise_for_status()
        return Result(**resp.json())

    async def complete_stream(
        self,
        model: str,
        messages: list[dict],
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        timeout: int | None = None,
        trace_tag: str | None = None,
        correlation_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Streaming chat completion. Yields events with keys `type` and `data`.

        Event types:
          complete.delta  — incremental content chunk
          complete.done   — final aggregated result
          complete.error  — terminal error
        """
        from httpx_sse import aconnect_sse

        client = self._ensure_client()
        body: dict[str, Any] = {"model": model, "messages": messages}
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if timeout is not None:
            body["timeout"] = timeout
        if trace_tag is not None:
            body["trace_tag"] = trace_tag

        import json
        async with aconnect_sse(
            client, "POST", "/v1/complete/stream",
            json=body, headers=self._cid_header(correlation_id),
        ) as event_source:
            async for sse in event_source.aiter_sse():
                yield {"type": sse.event, "data": json.loads(sse.data)}

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        timeout: int | None = None,
        correlation_id: str | None = None,
    ) -> Result:
        """Batch embedding."""
        client = self._ensure_client()
        body: dict[str, Any] = {"texts": texts}
        if model is not None:
            body["model"] = model
        if timeout is not None:
            body["timeout"] = timeout

        resp = await client.post("/v1/embed", json=body,
                                  headers=self._cid_header(correlation_id))
        resp.raise_for_status()
        return Result(**resp.json())

    async def run_agent(
        self,
        model: str,
        *,
        agent_model: str | None = None,
        system_prompt: str | None = None,
        initial_message: str | None = None,
        examples: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        tool_allowlist: list[str] | None = None,
        response_format: dict | None = None,
        max_turns: int | None = None,
        timeout: int | None = None,
        workspace: str | None = None,
        workspace_mode: str = "copy",
        trace_tag: str | None = None,
        metadata: dict | None = None,
        correlation_id: str | None = None,
        prepare: dict | None = None,
        artifacts: dict | None = None,
    ) -> Result:
        """Run an agent with MCP tool loop.

        `examples` is a list of {"user": ..., "assistant": ...} pairs that
        the server folds into the system prompt under an `## Examples` heading.

        `agent_model` overrides the LLM the agent uses internally (e.g.
        claude-opus-4-7); leave None to use the backend's default.

        `prepare` runs setup inside the sandbox before the agent starts:
            {
              "install_agents": ["claude"],
              "commands":       [{"cmd": "apt-get", "args": ["install","jq"]}],
              "files":          [{"path": "/workspace/in.txt", "content": "..."}],
              "sidecars":       [{"name": "mockapi", "cmd": "python", "args": ["x.py"]}],
            }
        A failed install / command / file write aborts the run with
        finish_reason="prepare_failed". Sidecars are torn down when the
        agent finishes (even on error).

        `artifacts` fetches files back from the sandbox after the run:
            {"fetch": ["/workspace/out.json", "/workspace/diff.patch"]}
        Returned in result.artifacts keyed by path.
        """
        client = self._ensure_client()
        body: dict[str, Any] = {"model": model}
        if agent_model is not None:
            body["agent_model"] = agent_model
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if initial_message is not None:
            body["initial_message"] = initial_message
        if examples is not None:
            body["examples"] = examples
        if mcp_servers is not None:
            body["mcp_servers"] = mcp_servers
        if tool_allowlist is not None:
            body["tool_allowlist"] = tool_allowlist
        if response_format is not None:
            body["response_format"] = response_format
        if max_turns is not None:
            body["max_turns"] = max_turns
        if timeout is not None:
            body["timeout"] = timeout
        if workspace is not None:
            body["workspace"] = workspace
        if workspace_mode != "copy":
            body["workspace_mode"] = workspace_mode
        if trace_tag is not None:
            body["trace_tag"] = trace_tag
        if metadata is not None:
            body["metadata"] = metadata
        if prepare is not None:
            body["prepare"] = prepare
        if artifacts is not None:
            body["artifacts"] = artifacts

        resp = await client.post("/v1/agent", json=body,
                                  headers=self._cid_header(correlation_id))
        resp.raise_for_status()
        return Result(**resp.json())

    async def recent_traces(
        self,
        *,
        since: str | None = None,
        trace_tag: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[TraceRecord]:
        """Query recent traces."""
        client = self._ensure_client()
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        if trace_tag:
            params["trace_tag"] = trace_tag
        if status:
            params["status"] = status

        resp = await client.get("/v1/traces", params=params)
        resp.raise_for_status()
        return [TraceRecord(**t) for t in resp.json()]

    # --- Task runner endpoints (legacy/fan-out) ---

    async def execute(self, **kwargs: Any) -> Result:
        """Execute a task against its first preferred provider."""
        client = self._ensure_client()
        task = TaskSpec(**kwargs)
        resp = await client.post("/v1/execute", json=task.model_dump(exclude_none=True))
        resp.raise_for_status()
        return Result(**resp.json())

    async def execute_stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        """Execute a task and stream events via SSE."""
        from httpx_sse import aconnect_sse

        client = self._ensure_client()
        task = TaskSpec(**kwargs)

        async with aconnect_sse(
            client, "POST", "/v1/execute/stream", json=task.model_dump(exclude_none=True)
        ) as event_source:
            import json
            async for sse in event_source.aiter_sse():
                yield {"type": sse.event, "data": json.loads(sse.data)}

    async def fanout(
        self, providers: list[str], max_concurrent: int = 4, **kwargs: Any,
    ) -> list[Result]:
        """Fan out a task across multiple providers."""
        client = self._ensure_client()
        task = TaskSpec(**kwargs)
        body = {
            "task": task.model_dump(exclude_none=True),
            "providers": providers,
            "max_concurrent": max_concurrent,
        }
        resp = await client.post("/v1/fanout", json=body)
        resp.raise_for_status()
        return [Result(**r) for r in resp.json()]

    async def get_run(self, run_id: str) -> dict:
        client = self._ensure_client()
        resp = await client.get(f"/v1/runs/{run_id}")
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> dict:
        client = self._ensure_client()
        resp = await client.get("/v1/health")
        resp.raise_for_status()
        return resp.json()

    async def aggregate_traces(
        self,
        *,
        group_by: str = "trace_tag",
        since: str | None = None,
        until: str | None = None,
        trace_tag: str | None = None,
    ) -> dict:
        """Roll up trace stats by trace_tag / kind / model / status / error_type / day."""
        client = self._ensure_client()
        params: dict[str, Any] = {"group_by": group_by}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if trace_tag:
            params["trace_tag"] = trace_tag
        resp = await client.get("/v1/traces/aggregates", params=params)
        resp.raise_for_status()
        return resp.json()

    # --- Cancellation ---

    async def list_active_runs(self) -> ActiveRuns:
        """Return the list of run_ids currently in-flight on the server."""
        client = self._ensure_client()
        resp = await client.get("/v1/runs/active")
        resp.raise_for_status()
        return ActiveRuns(**resp.json())

    async def cancel_run(self, run_id: str) -> CancelAck:
        """Signal cancellation for an in-flight run.

        Returns CancelAck on success. Raises httpx.HTTPStatusError(404)
        if the run isn't active.
        """
        client = self._ensure_client()
        resp = await client.post(f"/v1/runs/{run_id}/cancel")
        resp.raise_for_status()
        return CancelAck(**resp.json())

    async def run_agent_stream(
        self,
        model: str,
        *,
        agent_model: str | None = None,
        system_prompt: str | None = None,
        initial_message: str | None = None,
        examples: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        tool_allowlist: list[str] | None = None,
        response_format: dict | None = None,
        max_turns: int | None = None,
        timeout: int | None = None,
        workspace: str | None = None,
        workspace_mode: str = "copy",
        trace_tag: str | None = None,
        metadata: dict | None = None,
        correlation_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Streaming agent run. Yields events with `type` (event name) and `data`.

        Event types:
          agent.delta        — incremental text chunk
          agent.tool_call    — agent invoked an MCP tool
          agent.tool_result  — tool returned
          agent.done         — final aggregated Result
          agent.error        — terminal error
        """
        from httpx_sse import aconnect_sse

        client = self._ensure_client()
        body: dict[str, Any] = {"model": model}
        if agent_model is not None:
            body["agent_model"] = agent_model
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if initial_message is not None:
            body["initial_message"] = initial_message
        if examples is not None:
            body["examples"] = examples
        if mcp_servers is not None:
            body["mcp_servers"] = mcp_servers
        if tool_allowlist is not None:
            body["tool_allowlist"] = tool_allowlist
        if response_format is not None:
            body["response_format"] = response_format
        if max_turns is not None:
            body["max_turns"] = max_turns
        if timeout is not None:
            body["timeout"] = timeout
        if workspace is not None:
            body["workspace"] = workspace
        if workspace_mode != "copy":
            body["workspace_mode"] = workspace_mode
        if trace_tag is not None:
            body["trace_tag"] = trace_tag
        if metadata is not None:
            body["metadata"] = metadata

        import json
        async with aconnect_sse(
            client, "POST", "/v1/agent/stream",
            json=body, headers=self._cid_header(correlation_id),
        ) as event_source:
            async for sse in event_source.aiter_sse():
                yield {"type": sse.event, "data": json.loads(sse.data)}

    # --- Agent preview ---

    async def agent_preview(
        self,
        *,
        mcp_servers: list[dict] | None = None,
        tool_allowlist: list[str] | None = None,
    ) -> dict:
        """Dry-run resolve MCP servers + allowlist. Returns per-server tool lists
        plus allowlist matches/misses/unused, to catch typos before a real run.
        """
        client = self._ensure_client()
        body: dict[str, Any] = {}
        if mcp_servers is not None:
            body["mcp_servers"] = mcp_servers
        if tool_allowlist is not None:
            body["tool_allowlist"] = tool_allowlist
        resp = await client.post("/v1/agent/preview", json=body)
        resp.raise_for_status()
        return resp.json()

    # --- Model + agent discovery ---

    async def litellm_models(self) -> list[dict]:
        """All LiteLLM-known models (aliases + wildcard providers)."""
        client = self._ensure_client()
        resp = await client.get("/v1/litellm/models")
        resp.raise_for_status()
        return resp.json()

    async def sandbox_agent_info(self, agent: str) -> dict:
        """Capability info for one Sandbox Agent backend (models, perms, MCP)."""
        client = self._ensure_client()
        resp = await client.get(f"/v1/sandbox/agents/{agent}")
        resp.raise_for_status()
        return resp.json()

    # --- Discovery ---

    async def discovery(self) -> Discovery:
        """Capability + endpoint inventory + live dependency probes."""
        client = self._ensure_client()
        resp = await client.get("/v1/discovery")
        resp.raise_for_status()
        return Discovery(**resp.json())

    async def get_schema(self, name: str) -> dict:
        """Fetch a versioned JSON Schema by name (task, result, events, ...)."""
        client = self._ensure_client()
        resp = await client.get(f"/v1/schemas/{name}")
        resp.raise_for_status()
        return resp.json()
