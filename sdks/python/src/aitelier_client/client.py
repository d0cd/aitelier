"""Async HTTP client for the aitelier service.

aitelier's inference contract is OpenAI shape. The client splits cleanly:

  - Inference (`chat.completions`, `embeddings`, `models`) — get a pre-wired
    `openai.AsyncOpenAI` via `Aitelier.openai()`. Use the OpenAI SDK directly;
    retries, streaming, and tool semantics are theirs to own.
  - Control plane (`runs`, `traces`, `schedules`, `discovery`, `health`,
    `cancel`, async-run submission) — methods on `Aitelier` itself.

The OpenAI SDK is an optional dependency. Install `aitelier-client[openai]`
to use `.openai()`. Everything else works without it.
"""

from __future__ import annotations

import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from aitelier_client._generated.models import (
    ActiveRuns,
    CancelAck,
    Discovery,
    Run,
    RunEvent,
    RunScore,
    Schedule,
    TraceRecord,
    TracesAggregate,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI

_DEFAULT_BASE_URL = "http://localhost:7777"


def _discover_base_url() -> str | None:
    """Best-effort lookup of `[service] host`/`port` in ~/.config/aitelier/config.toml.

    Returns None if the file doesn't exist or doesn't declare a usable
    host+port. No env-var reads — the service config is the only source.
    """
    cfg_path = Path.home() / ".config" / "aitelier" / "config.toml"
    if not cfg_path.exists():
        return None
    try:
        data = tomllib.loads(cfg_path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return None
    svc = data.get("service") or {}
    host = svc.get("host")
    port = svc.get("port")
    if host and port:
        return f"http://{host}:{port}"
    return None


class Aitelier:
    """aitelier service client. Inference via `.openai()`; control plane direct."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or _discover_base_url()
                          or _DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._openai: AsyncOpenAI | None = None

    async def __aenter__(self) -> Aitelier:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._openai is not None:
            await self._openai.close()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout,
                headers=self._default_headers(),
            )
        return self._client

    def _default_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # --- Inference: hand the consumer a real OpenAI client -------------------

    def openai(self) -> AsyncOpenAI:
        """Return a pre-configured `openai.AsyncOpenAI` pointed at this aitelier.

        Use it for `chat.completions.create`, `embeddings.create`, `models.list`.
        Streaming, retries, structured outputs, tool-call semantics — all
        OpenAI SDK territory.

        Raises ImportError when the `openai` package isn't installed.
        Install `aitelier-client[openai]` to enable.
        """
        if self._openai is not None:
            return self._openai
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ImportError(
                "The `openai` package is required for Aitelier.openai(). "
                "Install with `pip install aitelier-client[openai]`.",
            ) from exc
        # OpenAI client adds /v1 itself; point base_url at the bare service.
        self._openai = AsyncOpenAI(
            base_url=self.base_url + "/v1",
            api_key=self.api_key or "no-auth",
        )
        return self._openai

    # --- Async agent runs (long-running, webhook-delivered) -----------------

    async def submit_run(
        self, *,
        model: str,
        messages: list[dict],
        webhook_url: str | None = None,
        aitelier_opts: dict | None = None,
        timeout: int | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        """Submit an async agent run via POST /v1/runs.

        Returns immediately with `{run_id, status: "accepted"}`. The final
        ChatCompletion (or error) is delivered to `webhook_url` when ready.
        Poll `get_run(run_id)` or `list_run_events(run_id)` otherwise.
        """
        body: dict[str, Any] = {"model": model, "messages": messages}
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if aitelier_opts is not None:
            body["aitelier"] = aitelier_opts
        if timeout is not None:
            body["timeout"] = timeout
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        resp = await self._ensure_client().post(
            "/v1/runs", json=body, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Control plane: runs + events ----------------------------------------

    async def get_run(self, run_id: str) -> Run:
        resp = await self._ensure_client().get(f"/v1/runs/{run_id}")
        resp.raise_for_status()
        return Run(**resp.json())

    async def list_runs(
        self, *,
        trace_tag: str | None = None,
        state: str | None = None,
        correlation_id: str | None = None,
        parent_run_id: str | None = None,
        limit: int = 50,
    ) -> list[Run]:
        params: dict[str, Any] = {"limit": limit}
        if trace_tag is not None:
            params["trace_tag"] = trace_tag
        if state is not None:
            params["state"] = state
        if correlation_id is not None:
            params["correlation_id"] = correlation_id
        if parent_run_id is not None:
            params["parent_run_id"] = parent_run_id
        resp = await self._ensure_client().get("/v1/runs", params=params)
        resp.raise_for_status()
        return [Run(**r) for r in resp.json()]

    async def list_run_events(self, run_id: str) -> list[RunEvent]:
        resp = await self._ensure_client().get(f"/v1/runs/{run_id}/events")
        resp.raise_for_status()
        return [RunEvent(**e) for e in resp.json()]

    async def stream_run_events(self, run_id: str) -> AsyncIterator[dict]:
        """Stream run events via SSE. Yields parsed event dicts."""
        from httpx_sse import aconnect_sse
        async with aconnect_sse(
            self._ensure_client(), "GET", f"/v1/runs/{run_id}/events/stream",
        ) as event_source:
            import json
            async for sse in event_source.aiter_sse():
                yield {"type": sse.event, "data": json.loads(sse.data)}

    async def list_active_runs(self) -> ActiveRuns:
        resp = await self._ensure_client().get("/v1/runs/active")
        resp.raise_for_status()
        return ActiveRuns(**resp.json())

    async def cancel_run(self, run_id: str) -> CancelAck:
        resp = await self._ensure_client().post(f"/v1/runs/{run_id}/cancel")
        resp.raise_for_status()
        return CancelAck(**resp.json())

    async def add_run_score(
        self, run_id: str, *,
        name: str, value: float, evaluator: str,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunScore:
        """Write a grader's score back against a run. Aitelier owns
        durable storage; the grading logic lives in the caller."""
        body: dict[str, Any] = {
            "name": name, "value": value, "evaluator": evaluator,
        }
        if comment is not None:
            body["comment"] = comment
        if metadata is not None:
            body["metadata"] = metadata
        resp = await self._ensure_client().post(
            f"/v1/runs/{run_id}/scores", json=body,
        )
        resp.raise_for_status()
        return RunScore(**resp.json())

    async def list_run_scores(self, run_id: str) -> list[RunScore]:
        resp = await self._ensure_client().get(f"/v1/runs/{run_id}/scores")
        resp.raise_for_status()
        return [RunScore(**s) for s in resp.json()["data"]]

    async def export_runs(
        self, *,
        since: str | None = None,
        until: str | None = None,
        trace_tag: str | None = None,
        kind: str | None = None,
        state: str | None = None,
        limit: int = 10000,
    ) -> AsyncIterator[Run]:
        """Stream historical runs as NDJSON — one Run per line. Use for
        backfill grading. The server returns the captured request_body
        on each Run so graders see exactly what the model saw."""
        import json

        params: dict[str, Any] = {"limit": limit}
        for k, v in (("since", since), ("until", until),
                      ("trace_tag", trace_tag), ("kind", kind), ("state", state)):
            if v is not None:
                params[k] = v
        async with self._ensure_client().stream(
            "GET", "/v1/runs/export", params=params,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield Run(**json.loads(line))

    async def wait_for_run(
        self, run_id: str, *,
        timeout: float = 60.0, poll_interval: float = 0.5,
    ) -> Run:
        """Block until `run_id` reaches a terminal state; return the Run.

        Server-side polling — convenience over rolling your own loop
        when you want submit-and-await without a webhook receiver.
        Raises `httpx.HTTPStatusError(408)` if the run is still
        pending/running when the timeout elapses (call again to keep
        waiting). Raises `httpx.HTTPStatusError(404)` if the run id
        is unknown.
        """
        resp = await self._ensure_client().post(
            f"/v1/runs/{run_id}/wait",
            params={"timeout": timeout, "poll_interval": poll_interval},
            timeout=timeout + 10.0,
        )
        resp.raise_for_status()
        return Run(**resp.json())

    # --- Control plane: traces ----------------------------------------------

    async def recent_traces(
        self, *,
        trace_tag: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[TraceRecord]:
        params: dict[str, Any] = {"limit": limit}
        if trace_tag is not None:
            params["trace_tag"] = trace_tag
        if status is not None:
            params["status"] = status
        if since is not None:
            params["since"] = since
        resp = await self._ensure_client().get("/v1/traces", params=params)
        resp.raise_for_status()
        return [TraceRecord(**t) for t in resp.json()]

    async def get_trace(self, trace_id: str) -> TraceRecord:
        resp = await self._ensure_client().get(f"/v1/traces/{trace_id}")
        resp.raise_for_status()
        return TraceRecord(**resp.json())

    async def aggregate_traces(
        self, *,
        group_by: str = "model",
        since: str | None = None,
        trace_tag: str | None = None,
        limit: int = 50,
    ) -> TracesAggregate:
        params: dict[str, Any] = {"group_by": group_by}
        if since is not None:
            params["since"] = since
        if trace_tag is not None:
            params["trace_tag"] = trace_tag
        if limit is not None:
            params["limit"] = limit
        resp = await self._ensure_client().get(
            "/v1/traces/aggregates", params=params,
        )
        resp.raise_for_status()
        return TracesAggregate(**resp.json())

    # --- Control plane: schedules -------------------------------------------

    async def list_schedules(self) -> list[Schedule]:
        resp = await self._ensure_client().get("/v1/schedules")
        resp.raise_for_status()
        return [Schedule(**s) for s in resp.json()]

    async def create_schedule(
        self, *,
        name: str,
        task: dict,
        interval_seconds: int | None = None,
        at_iso: str | None = None,
        webhook_url: str | None = None,
    ) -> Schedule:
        body: dict[str, Any] = {"name": name, "task": task}
        if interval_seconds is not None:
            body["interval_seconds"] = interval_seconds
        if at_iso is not None:
            body["at_iso"] = at_iso
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        resp = await self._ensure_client().post("/v1/schedules", json=body)
        resp.raise_for_status()
        return Schedule(**resp.json())

    async def get_schedule(self, schedule_id: str) -> Schedule:
        resp = await self._ensure_client().get(f"/v1/schedules/{schedule_id}")
        resp.raise_for_status()
        return Schedule(**resp.json())

    async def delete_schedule(self, schedule_id: str) -> dict:
        resp = await self._ensure_client().delete(
            f"/v1/schedules/{schedule_id}",
        )
        resp.raise_for_status()
        return resp.json()

    # --- Discovery / meta ----------------------------------------------------

    async def discovery(self) -> Discovery:
        resp = await self._ensure_client().get("/v1/discovery")
        resp.raise_for_status()
        return Discovery(**resp.json())

    async def health(self) -> dict:
        resp = await self._ensure_client().get("/v1/health")
        resp.raise_for_status()
        return resp.json()

    async def get_schema(self, name: str) -> dict:
        resp = await self._ensure_client().get(f"/v1/schemas/{name}")
        resp.raise_for_status()
        return resp.json()
