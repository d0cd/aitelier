"""Run-id utility — shared between endpoints and the schedule handler.

Inference dispatch (LLM via LiteLLM, agent via Sandbox Agent) lives in the
endpoint helpers in `server.py`. This module is just the run-id helper that
multiple call sites share.
"""

from __future__ import annotations

from datetime import UTC, datetime


def make_run_id(task_name: str) -> str:
    # Microsecond precision avoids primary-key collisions when two requests
    # land in the same wall-clock second (likely under any non-trivial load).
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")
    return f"{ts}_{task_name}"
