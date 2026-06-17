"""MCP server exposing aitelier's control plane as tools.

Loaded by an inner agent (via `aitelier.mcp_servers[]` on the parent
request) so the agent can dispatch subagents through aitelier without
hand-rolling HTTP calls. The tools cover the orchestration-relevant
slice of the aitelier-client surface: submit / fetch / wait / cancel
runs, list runs + events, score runs, query traces + aggregates, list
active runs, and discovery. Inference itself stays on the OpenAI-shape
HTTP path (not exposed here); schedules/webhooks are operator surfaces
an inner agent doesn't drive, so they're intentionally omitted.

No business logic lives here. Each tool is a thin wrapper that the
inner agent's tool-call machinery invokes; the wrapper makes one
aitelier HTTP call and returns the response. Aitelier's existing
auth / SSRF guard / idempotency / observability all apply since
we're just a consumer of its HTTP API.

Transport: defaults to stdio (the convention for agent-loaded MCP
servers). The `AITELIER_MCP_TRANSPORT` env var can switch to
`streamable-http` for out-of-process testing.

Env:
  AITELIER_URL           Base URL of the aitelier service (default:
                         http://localhost:7777).
  AITELIER_API_KEY       Bearer token if aitelier is in hosted mode.
  AITELIER_MCP_TRANSPORT One of: stdio (default), streamable-http.
"""

from __future__ import annotations

import os
from typing import Any

from aitelier_client import Aitelier
from mcp.server.fastmcp import FastMCP


def create_server(client: Aitelier | None = None) -> FastMCP:
    """Build the MCP server. `client` lets tests inject a fake
    Aitelier client; production callers pass nothing and we read the
    base URL + api key from env."""
    if client is None:
        base_url = os.environ.get("AITELIER_URL", "http://localhost:7777")
        api_key = os.environ.get("AITELIER_API_KEY")
        client = Aitelier(base_url=base_url, api_key=api_key)

    server = FastMCP("aitelier-mcp")

    @server.tool()
    async def submit_run(
        model: str,
        messages: list[dict[str, Any]],
        parent_run_id: str | None = None,
        trace_tag: str | None = None,
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        timeout: int | None = None,
        workspace: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        tool_allowlist: list[str] | None = None,
        max_turns: int | None = None,
        reasoning_effort: str | None = None,
        approval_mode: str | None = None,
        prepare: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        examples: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit an async agent run via aitelier's `POST /v1/runs`.

        Returns immediately with `{run_id, status: "accepted"}`. The
        final ChatCompletion (or error) lands on `webhook_url` when
        the run finishes; consumers without a webhook can poll
        `get_run` / `wait_for_run` / `list_run_events`.

        `model` must start with `agent:<backend>` (e.g. `agent:claude`).
        `parent_run_id` records this submission as a child of the
        provided run id — recover the subtree later via `list_runs`.

        `prepare` / `artifacts` drive the one-call agent workflow
        (install → commands → file seed → sidecars → run → artifact
        fetch); `reasoning_effort` / `approval_mode` / `examples` tune the
        agent itself. `tool_allowlist` / `max_turns` are claude-only.
        `timeout` (seconds) sets the server-side run limit and
        `correlation_id` propagates a trace id.
        """
        aitelier_block: dict[str, Any] = {}
        for key, value in (
            ("parent_run_id", parent_run_id),
            ("trace_tag", trace_tag),
            ("workspace", workspace),
            ("mcp_servers", mcp_servers),
            ("tool_allowlist", tool_allowlist),
            ("max_turns", max_turns),
            ("reasoning_effort", reasoning_effort),
            ("approval_mode", approval_mode),
            ("prepare", prepare),
            ("artifacts", artifacts),
            ("examples", examples),
        ):
            if value is not None:
                aitelier_block[key] = value
        return await client.submit_run(
            model=model, messages=messages,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            timeout=timeout,
            aitelier_opts=aitelier_block or None,
        )

    @server.tool()
    async def get_run(run_id: str) -> dict[str, Any]:
        """Fetch a single run's state. Returns the Run row including
        `state`, `status`, `input_tokens`, `output_tokens`, `total_tokens`,
        `parent_run_id`, `trace_tag`, `correlation_id`, `error_type`,
        `error_msg`."""
        run = await client.get_run(run_id)
        return _as_dict(run)

    @server.tool()
    async def list_runs(
        parent_run_id: str | None = None,
        trace_tag: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List runs filtered by parent_run_id / trace_tag / state.

        The intended use for multi-agent workflows: pass the orchestrator's
        own run_id as `parent_run_id` to recover the list of children
        (and their states, results, errors) without needing to track
        them yourself.
        """
        runs = await client.list_runs(
            parent_run_id=parent_run_id, trace_tag=trace_tag,
            state=state, limit=limit,
        )
        return [_as_dict(r) for r in runs]

    @server.tool()
    async def list_run_events(run_id: str) -> list[dict[str, Any]]:
        """Return the durable event log for a run — start, deltas,
        tool_calls, tool_results, finish, error. Use to inspect a
        child's behavior post-hoc, especially when the child failed."""
        events = await client.list_run_events(run_id)
        return [_as_dict(e) for e in events]

    @server.tool()
    async def cancel_run(run_id: str) -> dict[str, Any]:
        """Cancel an in-flight run. Returns `{run_id, cancelled: bool}`.
        Already-terminal runs accept the request but `cancelled` will
        be False — safe to call optimistically."""
        ack = await client.cancel_run(run_id)
        return _as_dict(ack)

    @server.tool()
    async def wait_for_run(
        run_id: str, timeout: float = 60.0, poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        """Block until `run_id` reaches a terminal state, then return the
        Run. Lets an orchestrator dispatch a child and await its result
        without a webhook receiver. Raises on 408 (still running at
        timeout — call again) or 404 (unknown run)."""
        run = await client.wait_for_run(
            run_id, timeout=timeout, poll_interval=poll_interval,
        )
        return _as_dict(run)

    @server.tool()
    async def add_run_score(
        run_id: str, name: str, value: float, evaluator: str,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a grader's score against a run. Lets an orchestrator
        grade its children's outputs and persist the verdict durably."""
        score = await client.add_run_score(
            run_id, name=name, value=value, evaluator=evaluator,
            comment=comment, metadata=metadata,
        )
        return _as_dict(score)

    @server.tool()
    async def list_run_scores(run_id: str) -> list[dict[str, Any]]:
        """All scores written against `run_id`, oldest first."""
        scores = await client.list_run_scores(run_id)
        return [_as_dict(s) for s in scores]

    @server.tool()
    async def recent_traces(
        trace_tag: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query recent runs as trace summaries (counts, tokens, cost,
        status). Use to review a workflow's children by `trace_tag`."""
        traces = await client.recent_traces(
            trace_tag=trace_tag, status=status, since=since, limit=limit,
        )
        return [_as_dict(t) for t in traces]

    @server.tool()
    async def aggregate_traces(
        group_by: str = "trace_tag",
        since: str | None = None,
        until: str | None = None,
        trace_tag: str | None = None,
    ) -> dict[str, Any]:
        """Roll up run stats by trace_tag / kind / model / agent_id /
        status / error_type / day — token + cost + error totals."""
        agg = await client.aggregate_traces(
            group_by=group_by, since=since, until=until, trace_tag=trace_tag,
        )
        return _as_dict(agg)

    @server.tool()
    async def list_active_runs() -> dict[str, Any]:
        """List run_ids currently in-flight in the aitelier process."""
        return _as_dict(await client.list_active_runs())

    @server.tool()
    async def discovery() -> dict[str, Any]:
        """aitelier's live capability + endpoint inventory + dependency
        health. The runtime source of truth for what's reachable."""
        return _as_dict(await client.discovery())

    @server.tool()
    async def get_my_run_id() -> dict[str, str | None]:
        """Return the calling agent's own aitelier run_id, or None.

        aitelier injects `AITELIER_RUN_ID` into the env of every stdio
        MCP server it spawns. Call this first and pass the returned
        value as `parent_run_id` on every `submit_run` so the children
        can be recovered via `list_runs(parent_run_id=...)` and the
        whole tree shows up under one workflow.
        """
        return {"run_id": os.environ.get("AITELIER_RUN_ID")}

    return server


def _as_dict(obj: Any) -> dict[str, Any]:
    """Convert SDK dataclass / pydantic responses to plain dicts so
    MCP's JSON serializer doesn't need adapters."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return {"value": obj}


def main() -> None:
    """Entry point for `aitelier-mcp` CLI script. stdio by default."""
    transport = os.environ.get("AITELIER_MCP_TRANSPORT", "stdio")
    server = create_server()
    server.run(transport=transport)


if __name__ == "__main__":
    main()
