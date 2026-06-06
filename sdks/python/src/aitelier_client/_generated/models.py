"""Generated models from schemas/v1/ — do not hand-edit.

To regenerate: ./scripts/generate-types.sh
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class McpServer(BaseModel):
    name: str
    transport: str  # "http" | "stdio"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None


class TaskSpec(BaseModel):
    """Specification for an aitelier task."""
    name: str
    kind: str  # "complete" | "embed" | "agent"
    model: str | None = None
    system_prompt: str | None = None
    messages: list[Message] | None = None
    prompt: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict | None = None
    texts: list[str] | None = None
    mcp_servers: list[McpServer] | None = None
    tool_allowlist: list[str] | None = None
    max_turns: int | None = None
    workspace: str | None = None
    workspace_mode: str = "copy"
    preferred_providers: list[str] | None = None
    timeout: int | None = None
    trace_tag: str | None = None
    metadata: dict[str, Any] | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ToolCall(BaseModel):
    server: str | None = None
    tool: str | None = None
    input: Any = None
    output: Any = None
    elapsed_ms: float | None = None


class Result(BaseModel):
    """Result of an aitelier operation."""
    kind: str
    provider: str
    status: str
    duration_s: float
    run_id: str
    trace_id: str | None = None
    content: str | None = None
    parsed: Any = None
    usage: Usage | None = None
    finish_reason: str | None = None
    cost_usd: float | None = None
    embeddings: list[list[float]] | None = None
    dimensions: int | None = None
    tool_calls: list[ToolCall] | None = None
    session_id: str | None = None
    files_changed: list[str] | None = None
    error_type: str | None = None
    error_msg: str | None = None

    # Legacy compat
    text: str | None = None


class Event(BaseModel):
    """Streaming event from a task execution."""
    type: str
    timestamp: str
    run_id: str | None = None
    provider: str | None = None
    data: dict[str, Any] | None = None


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


class CompleteStreamDelta(BaseModel):
    type: str  # always "delta"
    content: str
    correlation_id: str


class CompleteStreamDone(BaseModel):
    type: str  # always "done"
    content: str
    usage: Usage
    finish_reason: str
    cost_usd: float | None = None
    trace_id: str | None = None
    run_id: str | None = None
    correlation_id: str


class CompleteStreamError(BaseModel):
    type: str  # always "error"
    error_type: str
    error_msg: str
    correlation_id: str


# Tagged union: discriminate on `type` field.
CompleteStreamEvent = CompleteStreamDelta | CompleteStreamDone | CompleteStreamError
