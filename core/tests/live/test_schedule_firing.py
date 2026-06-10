"""Live tests for the schedule tick loop firing actual runs.

Schedule CRUD shape is covered in test_runs_and_schedules.py. Here we
verify the *firing* contract: a schedule's task body becomes a real run
within ~tick interval of `at_iso`, with the schedule id stamped into
the run's correlation_id (`sched-{schedule_id}-...`).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta


def _assert_local(litellm_models: list[str]) -> None:
    assert "local" in litellm_models, (
        f"`local` (Ollama) must be advertised by /v1/discovery for this test. "
        f"Curated models: {sorted(m for m in litellm_models if '/' not in m)}"
    )


def test_one_shot_schedule_fires_and_creates_run(http, litellm_models):
    """A one-shot schedule with `at_iso` ~3s in the future creates a run
    within ~10s, with the schedule id reflected in the run's correlation_id."""
    _assert_local(litellm_models)
    fire_at = datetime.now(UTC) + timedelta(seconds=3)
    name = f"live-fire-{uuid.uuid4().hex[:6]}"
    created = http.post("/v1/schedules", json={
        "name": name,
        "task": {
            "model": "local",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10, "temperature": 0,
        },
        "at_iso": fire_at.isoformat().replace("+00:00", "Z"),
    }).json()
    sid = created["id"]
    cid_prefix = f"sched-{sid}-"

    try:
        # Schedule tick loop runs every few seconds; give it generous time.
        deadline = time.monotonic() + 30
        run = None
        while time.monotonic() < deadline:
            recent = http.get("/v1/runs", params={"limit": 100}).json()
            run = next(
                (r for r in recent
                 if (r.get("correlation_id") or "").startswith(cid_prefix)),
                None,
            )
            if run is not None:
                break
            time.sleep(0.5)
        assert run is not None, (
            f"schedule {sid} ({name}) never produced a run within 30s. "
            f"at_iso={fire_at.isoformat()}. "
            f"Tick loop running? recent runs: "
            f"{[(r.get('run_id'), r.get('correlation_id')) for r in recent[:5]]}"
        )
        assert run["kind"] == "complete"
        # Wait for the run to settle (the firing race can return it in
        # `running` state). Up to 30s.
        run_id = run["run_id"]
        terminal_deadline = time.monotonic() + 30
        while time.monotonic() < terminal_deadline:
            r = http.get(f"/v1/runs/{run_id}").json()
            if r["state"] in ("completed", "failed"):
                run = r
                break
            time.sleep(0.5)
        assert run["state"] in ("completed", "failed"), run
    finally:
        # One-shot schedules auto-remove after firing; the DELETE is a
        # safety net in case the test failed before the fire.
        http.delete(f"/v1/schedules/{sid}")


def test_one_shot_schedule_next_run_at_clears_after_firing(http, litellm_models):
    """After a one-shot schedule fires, its `next_run_at` must be null so
    the tick loop doesn't re-fire it. The row remains (operators can
    inspect last_run_at); just the next-fire slot is cleared."""
    _assert_local(litellm_models)
    fire_at = datetime.now(UTC) + timedelta(seconds=3)
    name = f"live-once-{uuid.uuid4().hex[:6]}"
    created = http.post("/v1/schedules", json={
        "name": name,
        "task": {
            "model": "local",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10,
        },
        "at_iso": fire_at.isoformat().replace("+00:00", "Z"),
    }).json()
    sid = created["id"]
    cid_prefix = f"sched-{sid}-"

    try:
        # Wait for the run to appear so we know it fired.
        deadline = time.monotonic() + 30
        fired = False
        while time.monotonic() < deadline:
            recent = http.get("/v1/runs", params={"limit": 100}).json()
            if any((r.get("correlation_id") or "").startswith(cid_prefix)
                   for r in recent):
                fired = True
                break
            time.sleep(0.5)
        assert fired, f"schedule {sid} never fired"

        # next_run_at must be cleared so the tick loop won't re-fire it.
        # The clear can lag a tick (~10s); poll briefly.
        clear_deadline = time.monotonic() + 15
        while time.monotonic() < clear_deadline:
            row = http.get(f"/v1/schedules/{sid}").json()
            if row.get("next_run_at") is None:
                assert row.get("last_run_at") is not None, row
                return
            time.sleep(0.5)
        final = http.get(f"/v1/schedules/{sid}").json()
        assert final.get("next_run_at") is None, (
            f"one-shot schedule {sid} still has next_run_at after firing: "
            f"{final.get('next_run_at')}"
        )
    finally:
        http.delete(f"/v1/schedules/{sid}")
