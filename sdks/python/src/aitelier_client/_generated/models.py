"""Generated models from schemas/v1/ — do not hand-edit.

To regenerate: ./scripts/generate-types.sh

These cover the aitelier *control plane* — Run, RunEvent, Schedule, Discovery,
Traces, ActiveRuns, CancelAck. Inference shapes (ChatCompletion, Embedding,
Model) are OpenAI's; consume them via the `openai` package.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TraceRecord(BaseModel):
    """Trace summary from the trace store."""
    trace_id: str
    started_at: str
    ended_at: str | None = None
    model: str | None = None
    kind: str | None = None
    finish_reason: str | None = None
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    system_prompt_hash: str | None = None
    trace_tag: str | None = None
    status: str | None = None


class EndpointInfo(BaseModel):
    method: str
    path: str


class CapabilityInfo(BaseModel):
    available: bool
    reason: str | None = None


class LitellmDep(BaseModel):
    reachable: bool
    base_url: str
    models: list[str] | None = None
    reason: str | None = None


class SandboxAgentDep(BaseModel):
    reachable: bool
    base_url: str
    agents: list[str] | None = None
    reason: str | None = None


class Dependencies(BaseModel):
    litellm: LitellmDep
    sandbox_agent: SandboxAgentDep


class Discovery(BaseModel):
    """Capability + endpoint inventory + live dependency probes from GET /v1/discovery."""
    service: str  # always "aitelier"
    version: str
    api_version: str
    timestamp: str
    endpoints: list[EndpointInfo]
    capabilities: dict[str, CapabilityInfo]
    dependencies: Dependencies
    schemas: dict[str, str]
    known_limitations: list[str]


class CancelAck(BaseModel):
    """Response from POST /v1/runs/{run_id}/cancel."""
    run_id: str
    cancelled: bool


class ActiveRuns(BaseModel):
    """Response from GET /v1/runs/active."""
    active: list[str]


class Run(BaseModel):
    """A row from the durable runs table (GET /v1/runs, GET /v1/runs/{id})."""
    run_id: str
    state: str  # pending | running | completed | failed | cancelled | orphaned
    kind: str
    trace_id: str | None = None
    agent_id: str | None = None
    model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    trace_tag: str | None = None
    correlation_id: str | None = None
    parent_run_id: str | None = None
    sandbox_backend: str | None = None
    sandbox_url: str | None = None
    sandbox_server_id: str | None = None
    workspace: str | None = None
    environment: dict[str, Any] = {}
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    finish_reason: str | None = None
    tool_call_count: int = 0
    status: str | None = None
    error_type: str | None = None
    error_msg: str | None = None
    metadata: dict[str, Any] = {}


class RunEvent(BaseModel):
    """One row from the append-only run_events table."""
    run_id: str
    seq: int
    kind: str
    event_id: int | None = None
    ts: str | None = None
    payload: dict[str, Any] = {}


class Schedule(BaseModel):
    """A persisted schedule entry."""
    id: str
    name: str
    task: dict[str, Any]
    interval_seconds: int | None = None
    at_iso: str | None = None
    webhook_url: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    created_at: str | None = None


class TracesAggregateBucket(BaseModel):
    key: str
    count: int
    total_tokens: int
    cost_usd: float
    error_count: int


class TracesAggregateTotals(BaseModel):
    count: int
    total_tokens: int
    cost_usd: float
    error_count: int


class TracesAggregate(BaseModel):
    """Response from GET /v1/traces/aggregates."""
    group_by: str
    groups: list[TracesAggregateBucket]
    total: TracesAggregateTotals
