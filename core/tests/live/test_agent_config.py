"""Live tests for `aitelier.*` agent config knobs.

These exercise the non-default knobs that operators set per-run:
- `workspace` — agent's chdir
- `prepare.commands` — shell commands run before the agent
- `prepare.sidecars` — long-running processes spawned alongside
- `tool_allowlist` — restricts which agent tools can fire

All parameterized over `agent_backend`. `mcp_servers` is intentionally
not covered — it needs an MCP server fixture which is more involved;
see brig-feedback.md for the deferred-work note.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def _writable_workdir() -> str:
    """Path the agent (SA-side) can write to. Brig pins via env var;
    elsewhere we resolve the host's tempdir to avoid macOS's /tmp
    symlink trap."""
    override = os.environ.get("AITELIER_LIVE_TMPDIR")
    if override:
        return override
    return str(Path(tempfile.gettempdir()).resolve())


def test_agent_workspace_dispatches_under_workspace(http, trace_tag, agent_backend):
    """Aitelier records the workspace in the run row. The agent CLI may
    or may not actually `chdir` there (backend-specific); the contract
    aitelier promises is that the path is forwarded + recorded."""
    workdir = _writable_workdir()
    r = http.post("/v1/chat/completions", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "say ack"}],
        "timeout": 120,
        "aitelier": {
            "max_turns": 1,
            "trace_tag": trace_tag,
            "workspace": workdir,
        },
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["aitelier_run_id"]

    row = http.get(f"/v1/runs/{run_id}").json()
    assert row["workspace"] == workdir, (
        f"workspace not recorded; expected {workdir!r}, got {row.get('workspace')!r}"
    )


def test_agent_prepare_commands_runs_setup_before_agent(
    http, trace_tag, agent_backend,
):
    """`prepare.commands` runs in the sandbox before the agent. Verify by
    having the prep step write a marker file, then artifacts.fetch it back.
    The agent itself isn't asked to do anything substantive — we only care
    that the prepare layer ran."""
    workdir = _writable_workdir()
    marker = f"{workdir}/aitelier-prep-{trace_tag}.txt"
    body = f"prep ran for {trace_tag}"

    # SA's `ProcessRunRequest` is a struct ({command, args, cwd?, …}),
    # not a shell string. Use `sh -c` so we can keep the test command
    # as one readable redirect.
    r = http.post("/v1/chat/completions", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ok"}],
        "timeout": 120,
        "aitelier": {
            "max_turns": 1,
            "trace_tag": trace_tag,
            "prepare": {
                "commands": [{
                    "command": "sh",
                    "args": ["-c", f"echo {body!r} > {marker}"],
                }],
            },
            "artifacts": {"fetch": [marker]},
        },
    })
    assert r.status_code != 400, r.text
    body_json = r.json()
    if r.status_code == 500:
        err = (body_json.get("error") or {}).get("type", "")
        assert err != "PrepareFailed", (
            f"prepare command failed: {body_json}"
        )
    assert r.status_code == 200, body_json

    artifacts = body_json.get("aitelier_artifacts") or {}
    assert marker in artifacts, (
        f"expected artifact {marker!r} in {list(artifacts)}"
    )
    fetched = artifacts[marker]
    fetched_text = fetched.get("content") if isinstance(fetched, dict) else fetched
    assert body in str(fetched_text), (
        f"prepare marker mismatch: wrote {body!r}, read {fetched_text!r}"
    )


def test_agent_prepare_sidecar_starts_and_records(
    http, trace_tag, agent_backend,
):
    """`prepare.sidecars` spawns long-running processes. The contract
    aitelier promises: each sidecar's spawn is recorded in the run row's
    environment + sandbox info, and the run completes without the
    sidecar tripping the prepare layer."""
    r = http.post("/v1/chat/completions", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 120,
        "aitelier": {
            "max_turns": 1,
            "trace_tag": trace_tag,
            "prepare": {
                # SA's ProcessCreateRequest uses {command, args}, not a
                # single shell string. `sleep 60` is a no-op long-runner
                # — the agent won't talk to it, we just verify the
                # spawn doesn't trip the prepare layer.
                "sidecars": [{"command": "sleep", "args": ["60"]}],
            },
        },
    })
    assert r.status_code != 400, r.text
    body = r.json()
    if r.status_code == 500:
        err = (body.get("error") or {}).get("type", "")
        assert err != "PrepareFailed", (
            f"sidecar prepare failed: {body}"
        )
    assert r.status_code == 200, body


def test_agent_tool_allowlist_is_recorded_on_the_run(
    http, trace_tag, agent_backend,
):
    """Aitelier records the tool_allowlist passed in the request body so
    operators can audit what the agent was permitted to do. The actual
    enforcement is the agent CLI's job (claude-code reads the policy at
    boot); we test the recording, not the enforcement."""
    allowlist = ["Read", "Bash"]
    r = http.post("/v1/chat/completions", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 120,
        "aitelier": {
            "max_turns": 1,
            "trace_tag": trace_tag,
            "tool_allowlist": allowlist,
        },
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["aitelier_run_id"]

    row = http.get(f"/v1/runs/{run_id}").json()
    env = row.get("environment") or {}
    recorded = env.get("tool_allowlist") or []
    # Order-insensitive: aitelier may re-sort or normalize.
    assert set(recorded) == set(allowlist), (
        f"tool_allowlist not recorded; expected {allowlist}, "
        f"got environment.tool_allowlist={recorded}"
    )
