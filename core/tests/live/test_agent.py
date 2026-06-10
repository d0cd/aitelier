"""Live tests for the agent path on /v1/chat/completions.

Exercises real Sandbox Agent + OpenAI-shape translation end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path


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


def test_agent_sync_returns_chat_completion(http, trace_tag, picked_agent):
    agent = picked_agent
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent, aitelier_opts={"max_turns": 1,
                                              "trace_tag": trace_tag}),
        "timeout": 120,
    })
    assert r.status_code in (200, 500, 502), r.text
    if r.status_code == 200:
        body = r.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["aitelier_run_id"]


def test_agent_rejects_openai_tools(http, picked_agent):
    """The agent path must hard-reject `tools` — silent drops are a footgun."""
    agent = picked_agent
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent),
        "tools": [{"type": "function",
                   "function": {"name": "fake", "parameters": {}}}],
    })
    assert r.status_code == 400, r.text
    assert "tools" in r.json()["detail"]


def test_agent_rejects_aitelier_namespace_on_llm_path(http, litellm_models):
    """`aitelier.*` is agent-only — must be rejected for LLM models."""
    assert "local" in litellm_models, (
        f"`local` (Ollama) must be advertised by /v1/discovery for this test. "
        f"Got: {sorted(m for m in litellm_models if '/' not in m)}"
    )
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "hi"}],
        "aitelier": {"workspace": "/tmp"},
    })
    assert r.status_code == 400, r.text
    assert "aitelier" in r.json()["detail"]


def test_agent_run_persists_agent_id_and_inner_model(http, trace_tag, picked_agent):
    """The run row should record agent_id=<backend> and model=<inner LLM>
    distinctly, not collapse them."""
    agent = picked_agent
    # `local` (Ollama) as the inner model — verifies aitelier records what
    # was requested, regardless of whether the agent CLI actually honors
    # the inner-model hint (most agent CLIs use their own provider).
    inner = "local"
    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent, inner_model=inner,
                      aitelier_opts={"max_turns": 1, "trace_tag": trace_tag}),
        "timeout": 120,
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["aitelier_run_id"]

    runs = http.get("/v1/runs", params={"trace_tag": trace_tag, "limit": 10}).json()
    mine = [x for x in runs if x["run_id"] == run_id]
    assert mine, f"could not find run {run_id} via /v1/runs"
    row = mine[0]
    assert row["agent_id"] == agent
    assert row["model"] == inner


# ---------- /v1/chat/completions agent path (stream) ----------


def test_agent_stream_emits_openai_chunks(http, trace_tag, picked_agent):
    """Agent stream maps ACP deltas to OpenAI chunks. At minimum we should
    see one chunk with assistant role + final chunk with finish_reason."""
    agent = picked_agent
    chunks = []
    with http.stream("POST", "/v1/chat/completions", json={
        **_agent_body(agent, content="Say one word: hello",
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


# ---------- prepare → agent → artifacts loop ----------


def test_agent_prepare_files_round_trip(http, trace_tag, picked_agent):
    """`aitelier.prepare.files` ships a file into the sandbox and
    `aitelier.artifacts.fetch` reads it back. Exercises SA's actual
    `/v1/fs/file` wire shape — unit tests with mocked sa_proxy passed
    for a year while the real call sent `path` in the body and got 400
    from SA's query-string deserializer.

    No skipping: a 4xx here means a real bug (wire shape, validator
    misconfigured, picked_agent gone). The only legitimate non-pass is
    a 502 if SA itself is unreachable — and that's a fixture-level
    failure (`picked_agent` would also have failed)."""
    import os
    import tempfile

    agent = picked_agent
    # Path must be (a) writable on the aitelier+SA side, (b) non-symlinked
    # so Phase I's symlink-component guard accepts it. Host/docker deploys:
    # both processes run on the host; `Path(tempfile.gettempdir()).resolve()`
    # returns `/private/tmp` on macOS — writable, non-symlinked. Brig
    # deploys: aitelier+SA run inside the cell; the host's `/private/tmp`
    # doesn't exist there, but the cell's `/tmp` is a real (non-symlinked)
    # writable tmpfs. AITELIER_LIVE_TMPDIR lets the test runner pin
    # whichever path is correct for the target deployment.
    tmpdir_str = os.environ.get("AITELIER_LIVE_TMPDIR")
    tmpdir = Path(tmpdir_str) if tmpdir_str else Path(tempfile.gettempdir()).resolve()
    fname = str(tmpdir / f"aitelier-live-{trace_tag}.txt")
    content = f"live-test-{trace_tag}"

    r = http.post("/v1/chat/completions", json={
        **_agent_body(agent, content="ok",
                      aitelier_opts={
                          "max_turns": 1, "trace_tag": trace_tag,
                          "prepare": {"files": [{"path": fname, "content": content}]},
                          "artifacts": {"fetch": [fname]},
                      }),
        "timeout": 120,
    })

    # Distinguish: prepare-layer breakage (the bug class this test
    # guards) is a HARD FAIL. Agent-dispatch breakage downstream of a
    # successful prepare also fails — we want to know if anything in
    # the round-trip broke.
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

    # We need a successful run for the artifact fetch to populate.
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
