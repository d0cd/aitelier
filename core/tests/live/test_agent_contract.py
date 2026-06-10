"""Live contract tests for the SA-backed agent path.

Every test that exercises Sandbox Agent lives here. Each test takes an
`agent_backend` parameter which is parameterized at collection time
(see `pytest_generate_tests` in conftest.py). Run against just `claude`
by default (`--agent-matrix=curated`) or every backend SA advertises
(`--agent-matrix=full`).

Conventions:
- The agent path's response shape (sync + stream) must hold for every
  backend.
- prepare.files / artifacts.fetch / idempotency / cancellation are
  aitelier-side contracts — the backend choice should be transparent.
- Anything that depends on a specific backend's CLI behavior (e.g.,
  "claude actually writes file X via its Write tool") goes in
  test_agent_config.py instead, where it's gated explicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path



# ---------- helpers ----------


def _agent_body(agent: str, *, content: str = "Reply with exactly: ack",
                inner_model: str | None = None,
                aitelier_opts: dict | None = None) -> dict:
    model = f"agent:{agent}" + (f"/{inner_model}" if inner_model else "")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    if aitelier_opts:
        body["aitelier"] = aitelier_opts
    return body


# ---------- /v1/chat/completions agent path (sync) ----------


def test_agent_sync_returns_chat_completion(http, trace_tag, agent_backend):
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent_backend,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["aitelier_run_id"]


def test_agent_rejects_openai_tools(http, agent_backend):
    """The agent path must hard-reject `tools` — silent drops are a footgun."""
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent_backend),
        "tools": [{"type": "function",
                   "function": {"name": "fake", "parameters": {}}}],
    })
    assert r.status_code == 400, r.text
    assert "tools" in r.json()["detail"]


# The mirror test — `aitelier.*` rejected on the LLM path — lives in
# test_complete_and_embed.py since it's about the LLM path's validation,
# not an agent behavior. Kept there to avoid the parametrize tax.


def test_agent_run_persists_agent_id_and_inner_model(
    http, trace_tag, agent_backend,
):
    """The run row records agent_id=<backend> and model=<inner LLM>
    distinctly, not collapsed."""
    inner = "local"
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent_backend, inner_model=inner,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["aitelier_run_id"]

    runs = http.get("/v1/runs", params={"trace_tag": trace_tag, "limit": 10}).json()
    mine = [x for x in runs if x["run_id"] == run_id]
    assert mine, f"could not find run {run_id} via /v1/runs"
    row = mine[0]
    assert row["agent_id"] == agent_backend
    assert row["model"] == inner


# ---------- /v1/chat/completions agent path (stream) ----------


def test_agent_stream_emits_openai_chunks(http, trace_tag, agent_backend):
    """Agent stream maps ACP deltas to OpenAI chunks. At minimum we should
    see one chunk with assistant role + final chunk with finish_reason."""
    chunks = []
    with http.stream("POST", "/v1/chat/completions", json={
        **_agent_body(agent_backend, content="Say one word: hello",
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
        "stream": True,
    }) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunks.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    assert chunks, "expected at least one OpenAI chunk"
    finishes = [c["choices"][0].get("finish_reason") for c in chunks
                if c.get("choices")]
    has_terminal = "stop" in finishes or any("error" in c for c in chunks)
    assert has_terminal, f"no terminal chunk; got {chunks!r}"


# ---------- /v1/runs/{id}/events ----------


def test_agent_run_events_recorded(http, trace_tag, agent_backend):
    """Agent runs emit a `start` event (plus tool_call/tool_result if any)."""
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent_backend,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["aitelier_run_id"]
    events = http.get(f"/v1/runs/{run_id}/events").json()
    kinds = [e["kind"] for e in events]
    assert "start" in kinds, kinds


def test_agent_run_events_stream_emits_sse(http, trace_tag, agent_backend):
    """SSE stream of `/v1/runs/{id}/events` — wire format (data: lines,
    optional [DONE] sentinel), prefix matches the snapshot endpoint."""
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent_backend,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["aitelier_run_id"]

    expected = http.get(f"/v1/runs/{run_id}/events").json()
    assert expected, "agent run has no events; cannot exercise stream"

    streamed: list[dict] = []
    with http.stream("GET", f"/v1/runs/{run_id}/events/stream",
                     timeout=10) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            streamed.append(json.loads(payload))
            if len(streamed) >= len(expected):
                break
    streamed_seqs = [e.get("seq") for e in streamed]
    expected_seqs = [e["seq"] for e in expected]
    assert streamed_seqs[:len(expected_seqs)] == expected_seqs, (
        f"stream prefix mismatch:\n streamed={streamed_seqs}\n expected={expected_seqs}"
    )


# ---------- prepare → agent → artifacts loop ----------


def test_agent_prepare_files_round_trip(http, trace_tag, agent_backend):
    """`aitelier.prepare.files` ships a file into the sandbox and
    `aitelier.artifacts.fetch` reads it back. Exercises SA's actual
    `/v1/fs/file` wire shape (path-as-query-param, not body)."""
    # Path must be (a) writable on the SA side, (b) non-symlinked on
    # the aitelier side so the symlink-component guard accepts it.
    # AITELIER_LIVE_TMPDIR pins whichever path is correct for the target
    # deployment.
    tmpdir_str = os.environ.get("AITELIER_LIVE_TMPDIR")
    tmpdir = Path(tmpdir_str) if tmpdir_str else Path(tempfile.gettempdir()).resolve()
    fname = str(tmpdir / f"aitelier-live-{trace_tag}.txt")
    content = f"live-test-{trace_tag}"

    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent_backend, content="ok",
                      aitelier_opts={
                          "max_turns": 1, "trace_tag": trace_tag,
                          "prepare": {"files": [{"path": fname, "content": content}]},
                          "artifacts": {"fetch": [fname]},
                      }),
        "timeout": 120,
    })

    assert r.status_code != 400, (
        f"aitelier rejected the request at validation: {r.text}. "
        f"Likely a path-validation false positive on the test setup."
    )
    body = r.json()
    if r.status_code == 500:
        err_type = (body.get("error") or {}).get("type", "")
        assert err_type != "PrepareFailed", (
            f"prepare failed — the bug we guard: {body}"
        )
    assert r.status_code == 200, (
        f"agent run didn't complete cleanly; status={r.status_code}, "
        f"body={body}"
    )
    artifacts = body.get("aitelier_artifacts") or {}
    assert fname in artifacts, (
        f"expected `aitelier_artifacts[{fname!r}]` in response; got keys: "
        f"{list(artifacts)}"
    )
    fetched = artifacts[fname]
    fetched_text = (
        fetched.get("content") if isinstance(fetched, dict) else fetched
    )
    assert content in str(fetched_text), (
        f"round-trip mismatch — wrote {content!r}, read {fetched_text!r}"
    )


# ---------- Idempotency-Key on the agent path ----------


def test_agent_idempotency_same_key_returns_cached(
    http, trace_tag, agent_backend,
):
    """Second POST with the same Idempotency-Key + body returns the same
    body byte-for-byte. The work runs exactly once."""
    key = str(uuid.uuid4())
    body = {
        **_agent_body(agent_backend,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    }
    r1 = http.post("/v1/chat/completions", headers={"Idempotency-Key": key},
                   json=body)
    r2 = http.post("/v1/chat/completions", headers={"Idempotency-Key": key},
                   json=body)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["aitelier_run_id"] == r2.json()["aitelier_run_id"]
    assert r1.json() == r2.json()


def test_agent_idempotency_different_body_returns_422(http, agent_backend):
    """Reusing a key with a different body is almost always a consumer bug;
    the server should refuse loudly rather than treat as a new request."""
    key = str(uuid.uuid4())
    r1 = http.post(
        "/v1/chat/completions",
        headers={"Idempotency-Key": key},
        json={
            **_agent_body(agent_backend, content="first call",
                          aitelier_opts={"max_turns": 1}),
            "timeout": 120,
        },
    )
    r2 = http.post(
        "/v1/chat/completions",
        headers={"Idempotency-Key": key},
        json={
            **_agent_body(agent_backend, content="DIFFERENT",
                          aitelier_opts={"max_turns": 1}),
            "timeout": 120,
        },
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 422
    assert "Idempotency-Key" in r2.json()["detail"]


def test_agent_idempotency_distinct_keys_produce_distinct_runs(
    http, trace_tag, agent_backend,
):
    body = {
        **_agent_body(agent_backend,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    }
    r1 = http.post("/v1/chat/completions",
                   headers={"Idempotency-Key": str(uuid.uuid4())},
                   json=body)
    r2 = http.post("/v1/chat/completions",
                   headers={"Idempotency-Key": str(uuid.uuid4())},
                   json=body)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["aitelier_run_id"] != r2.json()["aitelier_run_id"]


# ---------- cancellation (agent-path) ----------


def test_agent_cancel_active_run_returns_cancelled(
    http, trace_tag, agent_backend,
):
    """Start an async agent run, observe it active, cancel it, verify the
    cancel acks and the run transitions to a terminal state."""
    r = http.post("/v1/runs", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "a" * 1000}],
        "timeout": 60,
        "aitelier": {"max_turns": 5, "trace_tag": trace_tag},
    })
    r.raise_for_status()
    run_id = r.json()["run_id"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        active = http.get("/v1/runs/active").json()["active"]
        if run_id in active:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(
            f"run {run_id} never appeared in /v1/runs/active within 10s. "
            f"Either the backend is too fast to observe (test needs a "
            f"deterministic-slow backend) or the active registry has a bug."
        )

    cancel = http.post(f"/v1/runs/{run_id}/cancel")
    assert cancel.status_code == 200, (
        f"cancel returned HTTP {cancel.status_code}: {cancel.text}. "
        f"If 404, the run finalized between active-check and cancel — "
        f"that's a race in the test design, not a cancellation bug, but "
        f"it still needs fixing."
    )
    assert cancel.json() == {"run_id": run_id, "cancelled": True}

    # Run must reach a terminal state. Real backends may take 30s+ to
    # unwind a subprocess after CancelledError.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        runs = http.get("/v1/runs", params={"trace_tag": trace_tag}).json()
        mine = next((r for r in runs if r["run_id"] == run_id), None)
        if mine and mine["state"] in ("cancelled", "completed", "failed"):
            return
        time.sleep(0.5)
    raise AssertionError(
        f"run {run_id} never reached a terminal state after cancel"
    )
