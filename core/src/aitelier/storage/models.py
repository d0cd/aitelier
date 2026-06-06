"""Plain dataclasses used by the storage layer.

These are *not* the wire types — those live in `schemas/v1/` and the SDKs.
These are the in-process representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

RunState = Literal["pending", "running", "completed", "failed", "cancelled", "orphaned"]
"""Valid states for a run. State machine enforced by `update_run_state`."""

_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "orphaned"})
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending":   frozenset({"running", "failed", "cancelled"}),
    "running":   frozenset({"completed", "failed", "cancelled", "orphaned"}),
    "completed": frozenset(),
    "failed":    frozenset(),
    "cancelled": frozenset(),
    "orphaned":  frozenset(),
}


def is_terminal(state: RunState) -> bool:
    return state in _TERMINAL_STATES


def can_transition(from_state: RunState, to_state: RunState) -> bool:
    return to_state in _VALID_TRANSITIONS.get(from_state, frozenset())


@dataclass
class RunSpec:
    """Inputs needed to create a run row at start-of-run."""
    run_id: str
    kind: str             # complete | embed | agent
    agent_id: str | None = None
    model: str | None = None
    trace_tag: str | None = None
    correlation_id: str | None = None
    sandbox_backend: str | None = None     # local | remote
    sandbox_url: str | None = None
    sandbox_server_id: str | None = None
    workspace: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    system_prompt_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Run:
    """A run row as returned from the store."""
    run_id: str
    state: RunState
    kind: str
    started_at: datetime
    ended_at: datetime | None = None
    agent_id: str | None = None
    model: str | None = None
    trace_tag: str | None = None
    correlation_id: str | None = None
    sandbox_backend: str | None = None
    sandbox_url: str | None = None
    sandbox_server_id: str | None = None
    workspace: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    finish_reason: str | None = None
    tool_call_count: int = 0
    system_prompt_hash: str | None = None
    status: str | None = None
    error_type: str | None = None
    error_msg: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunFilter:
    state: RunState | None = None
    kind: str | None = None
    agent_id: str | None = None
    trace_tag: str | None = None
    correlation_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50


@dataclass
class RunEvent:
    """One row in the append-only event timeline."""
    run_id: str
    seq: int
    # start | delta | tool_call | tool_result | finish | error | cancelled | orphaned
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime | None = None
    event_id: int | None = None  # set by store on insert


@dataclass
class Schedule:
    id: str
    name: str
    task: dict[str, Any]
    interval_seconds: int | None
    at_iso: datetime | None
    webhook_url: str | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime


@dataclass
class WebhookDelivery:
    id: int
    url: str
    payload: dict[str, Any]
    state: Literal["pending", "delivered", "failed"]
    attempts: int
    last_status_code: int | None
    last_error: str | None
    last_attempt_at: datetime | None
    next_attempt_at: datetime | None
    created_at: datetime
    run_id: str | None = None
    schedule_id: str | None = None
