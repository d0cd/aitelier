"""Plain dataclasses used by the storage layer.

These are *not* the wire types — those live in `schemas/v1/` and the SDKs.
These are the in-process representation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


def _max_metadata_bytes() -> int:
    """Read [storage] max_metadata_bytes from config at call time.

    Lazy lookup (rather than module-level constant) so we don't read
    config at import — keeps the import graph clean and lets tests
    override config without import-order tricks.
    """
    from aitelier.config import get_config
    return get_config().storage.max_metadata_bytes

RunState = Literal["pending", "running", "completed", "failed", "cancelled", "orphaned"]


# Valid `group_by` values for aggregate_runs. Both PostgresStore (which maps
# them to SQL expressions) and InMemoryStore (which maps them to attribute
# lookups) reference this single set so the two impls can't drift.
AGGREGATE_GROUP_KEYS = frozenset({
    "trace_tag", "kind", "model", "agent_id",
    "status", "error_type", "day",
})
"""Valid states for a run. State machine enforced by `update_run_state`."""

RunKind = Literal["complete", "embed", "agent"]
"""Wire-format kinds for the three primitives."""

RunEventKind = Literal[
    "start", "delta", "tool_call", "tool_result",
    "finish", "error", "cancelled", "orphaned",
    # Open extension point — backends may emit additional kinds. Listed
    # values are the ones aitelier itself emits via _RunEventEmitter.
]
"""Kinds emitted by the run-event timeline. Open-ended for forward-compat."""

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
    parent_run_id: str | None = None
    """Optional pointer to a parent run for multi-agent workflows.

    Pure pass-through — aitelier records the value and lets consumers
    query `/v1/runs?parent_run_id=X` to reconstruct hierarchies, but
    imposes no semantics: no FK, no cycle check, no cascade. The
    consumer (or the orchestrator above aitelier) owns the meaning."""
    sandbox_backend: str | None = None     # local | remote
    sandbox_url: str | None = None
    sandbox_server_id: str | None = None
    workspace: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    system_prompt_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defense against persisting unboundedly large metadata blobs.
        # JSON-encoded size is what hits Postgres; `default=str` lets
        # callers include datetimes/UUIDs without first serializing them.
        if self.metadata:
            size = len(json.dumps(self.metadata, default=str))
            limit = _max_metadata_bytes()
            if size > limit:
                raise ValueError(
                    f"metadata too large: {size} bytes "
                    f"(limit {limit}). Trim before passing."
                )


@dataclass
class Run:
    """A run row as returned from the store.

    `state` and `status` are intentionally separate and BOTH meaningful:

    - `state` (RunState literal) is the lifecycle position:
      pending → running → {completed | failed | cancelled | orphaned}.
      Used by the operational surface (/v1/runs/active, /v1/runs/{id}/wait)
      and by the on-startup orphan reconciliation that flips any
      pending/running rows from a previous process to `orphaned`.

    - `status` (free-form: "ok" | "error" | "cancelled" | None) is the
      outcome category surfaced in TraceRecord.status. It diverges
      from `state` only on user-initiated cancellation:
        state="cancelled", status="cancelled"   (user cancelled)
        state="failed",    status="error"        (provider/internal error)
        state="completed", status="ok"           (success)
      During pending/running, `status` is None.

    Consumers filtering for genuine failures should query `status="error"`;
    consumers wanting user-initiated stops should query `status="cancelled"`.
    `state` is the right filter for "still in flight" (in {pending, running}).
    """
    run_id: str
    state: RunState
    """Lifecycle position. See class docstring for the relationship to `status`."""
    kind: str
    started_at: datetime
    ended_at: datetime | None = None
    agent_id: str | None = None
    model: str | None = None
    trace_tag: str | None = None
    correlation_id: str | None = None
    parent_run_id: str | None = None
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
    """Outcome category. None during pending/running; set at terminal state.
    See class docstring for the relationship to `state`."""
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
    parent_run_id: str | None = None
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
class IdempotencyRecord:
    """Cached response for a previously-seen Idempotency-Key.

    The SDK auto-attaches the same key on retries; the server returns the
    cached response (instead of re-executing) when the body matches. A
    different body under the same key signals consumer error (HTTP 422).
    """
    key: str
    body_hash: str
    endpoint: str
    status_code: int
    response: dict[str, Any]
    expires_at: datetime
    run_id: str | None = None
    created_at: datetime | None = None


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
