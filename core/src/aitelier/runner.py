"""Run-id utility — shared between endpoints and the schedule handler.

Inference dispatch (LLM via LiteLLM, agent via Sandbox Agent) lives in the
endpoint helpers in `server.py`. This module is just the run-id helper that
multiple call sites share.
"""

from __future__ import annotations

import secrets


def make_run_id() -> str:
    """A run id IS a trace id: 128 bits of entropy as 32 lowercase hex chars
    — a valid W3C/OpenTelemetry trace id, so every run is directly
    addressable in any OTLP backend and `trace_id == run_id` stays true.

    No timestamp or task name is baked into the id; that's an anti-pattern
    (ids carrying semantic payload). Chronology comes from `started_at`,
    classification from `kind`/`agent_id`.
    """
    return secrets.token_hex(16)
