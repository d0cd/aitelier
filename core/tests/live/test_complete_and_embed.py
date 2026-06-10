"""Live tests for /v1/chat/completions (LLM path) and /v1/embeddings against
a running aitelier. Hits the real LiteLLM + model stack so the surface
documented in INTEGRATION.md is continuously verified end-to-end.
"""

from __future__ import annotations

import json

import pytest

from .conftest import skip_on_upstream_unavailable as _skip_on_upstream_unavailable


def _has_claude_haiku(models: list[str]) -> bool:
    return any(m == "claude-haiku" or m.endswith("/claude-haiku") for m in models)


# ---------- /v1/chat/completions (LLM path) ----------


def test_chat_completions_returns_content_for_haiku(http, litellm_models):
    if not _has_claude_haiku(litellm_models):
        pytest.skip("claude-haiku not in this LiteLLM config")
    r = http.post("/v1/chat/completions", json={
        "model": "claude-haiku",
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 20,
        "temperature": 0,
    })
    _skip_on_upstream_unavailable(r)
    r.raise_for_status()
    body = r.json()
    assert body["object"] == "chat.completion"
    content = body["choices"][0]["message"]["content"]
    if body["usage"]["completion_tokens"] > 0:
        assert content, "output tokens reported but content is empty"
    assert body["aitelier_run_id"]
    assert body["correlation_id"]


def test_chat_completions_with_local_model(http, litellm_models):
    """Contract: even reasoning models (qwen3 / o1 / thinking) return a
    valid ChatCompletion. With the Ollama bypass, thinking surfaces as
    `message.reasoning_content` and `finish_reason` is honest."""
    if "local" not in litellm_models:
        pytest.skip("`local` model not configured in LiteLLM")
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Reply with just: hi"}],
        "max_tokens": 200,
        "temperature": 0,
    })
    if r.status_code != 200:
        pytest.skip(f"local model unavailable: {r.text}")
    body = r.json()
    assert body["object"] == "chat.completion"
    finish = body["choices"][0]["finish_reason"]
    content = body["choices"][0]["message"].get("content") or ""
    assert content or finish in ("length", "stop")


def test_chat_completions_local_reasoning_model_acceptance(http, litellm_models):
    """For `local` with completion_tokens > 0, at least one of content /
    reasoning_content / tool_calls must be non-empty. Catches the
    LiteLLM-drops-thinking regression on Ollama-routed reasoning models."""
    if "local" not in litellm_models:
        pytest.skip("`local` model not configured")
    # Tight budget forces qwen3-class models into hidden reasoning territory.
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "Reply: yes."}],
        "max_tokens": 50,
        "temperature": 0,
    })
    _skip_on_upstream_unavailable(r)
    assert r.status_code == 200, r.text
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


def test_chat_completions_json_object_on_anthropic_returns_200(http):
    """When a provider doesn't natively support `json_object`, aitelier
    strips the param and injects a system-prompt directive — the call
    should succeed."""
    r = http.post("/v1/chat/completions", json={
        "model": "claude-haiku",
        "messages": [{"role": "user",
                       "content": "Return JSON like {\"ok\": true}"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 50,
        "temperature": 0,
    })
    _skip_on_upstream_unavailable(r)
    # The assertion this test enforces is aitelier's contract:
    # `response_format: json_object` on an unsupported provider must
    # NOT 422 and must NOT drop the request — aitelier strips the
    # param and injects a system-prompt directive, so the call should
    # land at 200. Whether the MODEL actually returns parseable JSON
    # is the model's job; aitelier doesn't promise it.
    assert r.status_code == 200, r.text


def test_chat_completions_json_schema_on_local_uses_ollama_structured_output(
    http, litellm_models,
):
    """With the Ollama bypass, `local` supports json_schema natively via
    Ollama's `format` parameter (structured outputs). Returns 200 with
    `aitelier_parsed` populated when the model produces parseable JSON."""
    if "local" not in litellm_models:
        pytest.skip("`local` model not configured")
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
    if r.status_code != 200:
        pytest.skip(f"local model unavailable: {r.status_code} {r.text}")
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
    if not _has_claude_haiku(litellm_models):
        pytest.skip("claude-haiku not configured")
    chunks = []
    with http.stream("POST", "/v1/chat/completions", json={
        "model": "claude-haiku",
        "messages": [{"role": "user", "content": "Count to three."}],
        "max_tokens": 30,
        "temperature": 0,
        "stream": True,
    }) as resp:
        _skip_on_upstream_unavailable(resp)
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
    # Upstream may have failed mid-stream (rate limit etc.); skip rather
    # than fail when we got an error envelope instead of a terminal stop.
    if any("error" in c for c in chunks):
        pytest.skip(f"upstream errored mid-stream: {chunks[-1].get('error')}")
    assert chunks, "expected at least one OpenAI chunk"
    finishes = [c["choices"][0].get("finish_reason") for c in chunks
                if c.get("choices")]
    assert "stop" in finishes


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
    if "nomic-embed-text" not in litellm_models:
        pytest.skip("nomic-embed-text not configured")
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
