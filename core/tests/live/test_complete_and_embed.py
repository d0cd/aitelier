"""Live tests for /v1/chat/completions (LLM path) and /v1/embeddings against
a running aitelier. Hits the real LiteLLM + model stack so the surface
documented in INTEGRATION.md is continuously verified end-to-end.

Strict mode: if a curated model isn't advertised by /v1/discovery, the
test fails — provisioning gaps must be visible, not papered over.

LLM-mode tests target `local` (Ollama via LiteLLM) so they don't need
an external provider key. Anthropic-specific behavior (e.g. the
strip-and-system-prompt fallback when response_format=json_object isn't
natively supported) is covered by unit tests against a mocked provider.
"""

from __future__ import annotations

import json

from .conftest import assert_upstream_ok


def _assert_curated_model(model: str, models: list[str]) -> None:
    """Strict gate: the curated model must be advertised. Tells the
    operator exactly what's missing instead of skipping."""
    assert model in models or any(m.endswith(f"/{model}") for m in models), (
        f"{model!r} must be advertised by /v1/discovery for this test. "
        f"Curated models in this LiteLLM config: "
        f"{sorted(m for m in models if '/' not in m)}"
    )


# ---------- /v1/chat/completions (LLM path) ----------


def test_chat_completions_returns_content(http, litellm_models):
    """Basic completion against `local` (Ollama). Verifies the OpenAI
    response shape: object/choices/message, usage, correlation. Allows
    content to be empty when a reasoning model (qwen3 etc.) routes all
    its tokens to hidden thinking — finish_reason must still be honest,
    and `test_chat_completions_local_reasoning_model_acceptance` covers
    the empty-content-with-reasoning-signal case separately."""
    _assert_curated_model("local", litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 200,
        "temperature": 0,
    })
    assert_upstream_ok(r)
    body = r.json()
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    msg = choice["message"]
    assert (
        msg.get("content")
        or msg.get("reasoning_content")
        or choice.get("finish_reason") in ("stop", "length")
    ), f"response has no content, reasoning, or terminal finish: {choice}"
    assert body["aitelier_run_id"]
    assert body["correlation_id"]


def test_chat_completions_with_local_model(http, litellm_models):
    """Contract: even reasoning models (qwen3 / o1 / thinking) return a
    valid ChatCompletion. With the Ollama bypass, thinking surfaces as
    `message.reasoning_content` and `finish_reason` is honest."""
    _assert_curated_model("local", litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Reply with just: hi"}],
        "max_tokens": 200,
        "temperature": 0,
    })
    assert_upstream_ok(r)
    body = r.json()
    assert body["object"] == "chat.completion"
    finish = body["choices"][0]["finish_reason"]
    content = body["choices"][0]["message"].get("content") or ""
    assert content or finish in ("length", "stop")


def test_chat_completions_local_reasoning_model_acceptance(http, litellm_models):
    """For `local` with completion_tokens > 0, at least one of content /
    reasoning_content / tool_calls must be non-empty. Catches the
    LiteLLM-drops-thinking regression on Ollama-routed reasoning models."""
    _assert_curated_model("local", litellm_models)
    # Tight budget forces qwen3-class models into hidden reasoning territory.
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Reply: yes."}],
        "max_tokens": 50,
        "temperature": 0,
    })
    assert_upstream_ok(r)
    body = r.json()
    completion_tokens = body["usage"]["completion_tokens"]
    msg = body["choices"][0]["message"]
    has_any = bool(
        msg.get("content")
        or msg.get("reasoning_content")
        or msg.get("tool_calls")
        or msg.get("aitelier_exit") == "empty"
    )
    # Either some payload surfaced, or aitelier flagged the empty-tokens
    # case via the documented `aitelier_exit: "empty"` signal. Anything
    # else means the regression (LiteLLM-drops-thinking) is back.
    assert has_any, (
        f"completion_tokens={completion_tokens} but no content / "
        f"reasoning_content / tool_calls / aitelier_exit signal "
        f"surfaced. choice={body['choices'][0]!r}"
    )


def test_chat_completions_json_object_returns_200(http, litellm_models):
    """`response_format: json_object` must round-trip without 422 / drop.
    Exercised against `local` (Ollama, which has native json_object via
    the bypass adapter); the Anthropic-fallback path (strip + system-
    prompt directive) is covered by unit tests against a mocked provider."""
    _assert_curated_model("local", litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user",
                       "content": "Return JSON like {\"ok\": true}"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 50,
        "temperature": 0,
    })
    assert_upstream_ok(r)


def test_chat_completions_json_schema_on_local_uses_ollama_structured_output(
    http, litellm_models,
):
    """With the Ollama bypass, `local` supports json_schema natively via
    Ollama's `format` parameter (structured outputs). Returns 200 with
    `aitelier_parsed` populated when the model produces parseable JSON."""
    _assert_curated_model("local", litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Return JSON: x is the string 'ok'"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "X",
                "schema": {"type": "object",
                           "properties": {"x": {"type": "string"}},
                           "required": ["x"]},
                "strict": True,
            },
        },
        "max_completion_tokens": 400,
    })
    assert_upstream_ok(r)
    body = r.json()
    content = body["choices"][0]["message"].get("content") or ""
    # Either the content parses as JSON (Ollama enforced the schema) or
    # aitelier_parsed populated from our fence-stripper. Both are valid.
    if content:
        import json as _json
        try:
            parsed = _json.loads(content)
        except _json.JSONDecodeError:
            parsed = body["choices"][0]["message"].get("aitelier_parsed")
        assert parsed is not None and "x" in parsed, (
            f"expected schema-shaped JSON, got: {content!r}"
        )


# ---------- streaming ----------


def test_chat_completions_stream_emits_chunks(http, litellm_models):
    """SSE streaming wire format. Allows finish_reason to be `length` as
    well as `stop` — a reasoning model on a tight budget can legitimately
    terminate via length, and the test is about the OpenAI chunk shape,
    not the model's verbosity."""
    _assert_curated_model("local", litellm_models)
    chunks = []
    with http.stream("POST", "/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Count to three."}],
        "max_tokens": 200,
        "temperature": 0,
        "stream": True,
    }) as resp:
        assert resp.status_code == 200, (
            f"stream open failed: HTTP {resp.status_code}. See conftest's "
            f"`assert_upstream_ok` for the diagnostic table."
        )
        for line in resp.iter_lines():
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunks.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    # If a chunk reports an error envelope mid-stream, that's a real bug
    # in aitelier's streaming surface — fail rather than dressing it as
    # an environmental issue.
    error_chunks = [c["error"] for c in chunks if "error" in c]
    assert not error_chunks, (
        f"stream emitted error chunks: {error_chunks}. Streaming must "
        f"surface clean terminal chunks; mid-stream errors indicate a "
        f"real upstream or aitelier bug."
    )
    assert chunks, "expected at least one OpenAI chunk"
    finishes = [c["choices"][0].get("finish_reason") for c in chunks
                if c.get("choices")]
    assert any(f in ("stop", "length") for f in finishes), (
        f"stream never emitted a terminal finish_reason; got: {finishes}"
    )


# ---------- /v1/models ----------


def test_models_endpoint_lists_aliases(http, litellm_models):
    r = http.get("/v1/models")
    r.raise_for_status()
    data = r.json()
    assert data["object"] == "list"
    ids = {m["id"] for m in data["data"]}
    # At least one curated alias should round-trip.
    assert ids & set(litellm_models), (
        f"discovery has {litellm_models} but /v1/models returned {ids}"
    )


def test_discovery_exposes_model_response_format_capabilities(http):
    """Cross-check /v1/discovery.models[*].response_format against the
    documented capability registry in providers/llm.py:

      - claude / anthropic/*: empty (no native support; aitelier falls back
        to system-prompt directive)
      - local / ollama/*: {json_object, json_schema} (Ollama native via
        the bypass adapter)
    """
    d = http.get("/v1/discovery").json()
    models = d.get("models") or []
    assert models, "discovery should expose a models[] list"
    annotated = [m for m in models if "response_format" in m]
    assert annotated, "no model in discovery has response_format capabilities"
    by_name = {m["name"]: m for m in models}

    if "local" in by_name and "response_format" in by_name["local"]:
        # Ollama bypass DOES support structured output natively.
        rf = by_name["local"]["response_format"]
        assert "json_object" in rf, f"local should support json_object: {rf}"
        assert "json_schema" in rf, f"local should support json_schema: {rf}"

    claude_keys = [n for n in by_name if n.startswith("claude")]
    if claude_keys:
        rf = by_name[claude_keys[0]].get("response_format", [])
        # Anthropic doesn't natively support either; aitelier returns []
        # so consumers know to expect the system-prompt-directive fallback.
        assert rf == [], (
            f"claude should advertise no native response_format support; "
            f"got: {rf}"
        )


# ---------- /v1/embeddings ----------


def test_embeddings_returns_correct_dimensions(http, litellm_models):
    _assert_curated_model("nomic-embed-text", litellm_models)
    r = http.post("/v1/embeddings", json={
        "model": "nomic-embed-text",
        "input": ["one", "two"],
    })
    r.raise_for_status()
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    dims = len(body["data"][0]["embedding"])
    assert dims > 0
    assert body["aitelier_run_id"]


# ---------- LLM-path validation ----------


def test_llm_path_rejects_aitelier_namespace(http, litellm_models):
    """`aitelier.*` is agent-only — must be rejected for LLM models.
    Mirror of the agent-path test that rejects `tools`; both guard against
    silent drops on the wrong route."""
    _assert_curated_model("local", litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "hi"}],
        "aitelier": {"workspace": "/tmp"},
    })
    assert r.status_code == 400, r.text
    assert "aitelier" in r.json()["detail"]
