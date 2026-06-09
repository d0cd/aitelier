"""Run-id utility — shared between endpoints and the schedule handler.

Inference dispatch (LLM via LiteLLM, agent via Sandbox Agent) lives in the
endpoint helpers in `server.py`. This module is just the run-id helper that
multiple call sites share.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def make_run_id(task_name: str) -> str:
    # Microsecond timestamp + 4 hex chars of entropy. Microseconds alone
    # collide under tight async fan-outs (same wall-clock μs is plausible
    # inside one event-loop tick); the suffix makes the PK collision-proof
    # without giving up the chronological sort the timestamp provides.
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")
    return f"{ts}_{task_name}_{secrets.token_hex(2)}"
