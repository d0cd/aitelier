"""Tests for providers/llm.py — the LiteLLM-facing passthrough."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aitelier.providers.llm import (
    LLMError,
    UnsupportedResponseFormat,
    _apply_response_format_gates,
    _build_ollama_request,
    _ollama_to_chat_completion,
    _resolve_ollama_model,
    _routes_to_ollama,
    _wants_anthropic_prompt_caching,
    chat_completion,
    chat_completion_stream,
    list_models,
)

# --- Response-format gating -------------------------------------------------


def test_json_object_on_anthropic_strips_and_injects_directive():
    body = _apply_response_format_gates("claude-haiku", {
        "model": "claude-haiku",
        "messages": [{"role": "system", "content": "You are a helper."},
                     {"role": "user", "content": "go"}],
        "response_format": {"type": "json_object"},
    })
    assert "response_format" not in body
    assert "JSON only" in body["messages"][0]["content"]
    assert "You are a helper." in body["messages"][0]["content"]


def test_json_object_on_anthropic_with_no_system_prompt():
    body = _apply_response_format_gates("claude-sonnet", {
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "go"}],
        "response_format": {"type": "json_object"},
    })
    assert "response_format" not in body
    assert body["messages"][0]["role"] == "system"
    assert "JSON only" in body["messages"][0]["content"]


def test_json_object_on_openai_passes_through():
    body = _apply_response_format_gates("openai/gpt-4o", {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_object"},
    })
    assert body["response_format"] == {"type": "json_object"}


def test_json_schema_on_ollama_passes_through():
    """`local` and `ollama/*` go through the Ollama bypass which maps
    json_schema → Ollama's native `format: <schema>`. The gate should
    NOT reject it. Pass-through is honest because the bypass path
    actually honors it server-side."""
    body = _apply_response_format_gates("local", {
        "model": "local",
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_schema", "schema": {}},
    })
    assert body["response_format"] == {"type": "json_schema", "schema": {}}


def test_json_schema_on_anthropic_raises_unsupported():
    """LiteLLM's Anthropic adapter raises in get_optional_params on OpenAI-shape
    json_schema, so aitelier rejects up front with a typed 400 rather than
    letting consumers see a 502 traceback from upstream."""
    with pytest.raises(UnsupportedResponseFormat):
        _apply_response_format_gates("claude-haiku", {
            "model": "claude-haiku",
            "messages": [{"role": "user", "content": "x"}],
            "response_format": {"type": "json_schema", "schema": {}},
        })


def test_unknown_model_passes_through_response_format():
    body = _apply_response_format_gates("exotic-model-v9", {
        "model": "exotic-model-v9",
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_schema", "schema": {}},
    })
    assert body["response_format"] == {"type": "json_schema", "schema": {}}


def test_wants_anthropic_prompt_caching_detects_block_cache_control():
    """`cache_control` on a content block must be detected so the
    anthropic-beta header gets attached — without it LiteLLM strips the
    marker and cache hits = 0."""
    assert _wants_anthropic_prompt_caching({
        "model": "claude-haiku",
        "messages": [{
            "role": "system",
            "content": [{
                "type": "text", "text": "long stable prefix",
                "cache_control": {"type": "ephemeral"},
            }],
        }],
    }) is True


def test_wants_anthropic_prompt_caching_skips_non_anthropic():
    """OpenAI doesn't use cache_control; sending the Anthropic beta
    header there would be at best ignored, at worst confusing."""
    assert _wants_anthropic_prompt_caching({
        "model": "openai/gpt-4o",
        "messages": [{
            "role": "system",
            "content": [{
                "type": "text", "text": "...",
                "cache_control": {"type": "ephemeral"},
            }],
        }],
    }) is False


def test_wants_anthropic_prompt_caching_skips_when_no_marker():
    """Anthropic route without cache_control: don't attach the beta
    header — keeps request headers minimal for the common case."""
    assert _wants_anthropic_prompt_caching({
        "model": "anthropic/claude-haiku-4-5",
        "messages": [{"role": "user", "content": "hi"}],
    }) is False


@pytest.mark.asyncio
async def test_chat_completion_attaches_anthropic_beta_header(monkeypatch):
    """End-to-end: a cache_control-bearing message on a claude model
    causes the anthropic-beta header to land on the upstream request."""
    captured = {}

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={
        "id": "x", "object": "chat.completion", "model": "claude-haiku",
        "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })

    async def _post(url, **kw):
        captured["headers"] = kw.get("headers")
        return fake_resp
    fake_client.post = AsyncMock(side_effect=_post)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    await chat_completion({
        "model": "claude-haiku",
        "messages": [{
            "role": "system",
            "content": [{
                "type": "text", "text": "stable prefix",
                "cache_control": {"type": "ephemeral"},
            }],
        }, {"role": "user", "content": "hi"}],
    })
    assert captured["headers"].get("anthropic-beta") == "prompt-caching-2024-07-31"


@pytest.mark.asyncio
async def test_chat_completion_omits_beta_header_without_cache_control(monkeypatch):
    captured = {}

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={
        "id": "x", "object": "chat.completion", "model": "claude-haiku",
        "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })

    async def _post(url, **kw):
        captured["headers"] = kw.get("headers")
        return fake_resp
    fake_client.post = AsyncMock(side_effect=_post)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    await chat_completion({
        "model": "claude-haiku",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert "anthropic-beta" not in captured["headers"]


@pytest.mark.asyncio
async def test_list_models_attaches_request_caps(monkeypatch):
    """LLM-routed entries declare the full OpenAI request surface so
    consumer pickers can pre-strip fields based on the catalog (hermes
    feedback #2). response_format reflects the per-model registry."""
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={"data": [
        {"id": "claude-sonnet"},
        {"id": "openai/gpt-4o"},
    ]})
    fake_client.get = AsyncMock(return_value=fake_resp)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    models = await list_models()
    by_id = {m["id"]: m for m in models}
    claude_caps = by_id["claude-sonnet"]["aitelier_request_caps"]
    assert claude_caps["tools"] is True
    assert claude_caps["top_p"] is True
    assert claude_caps["streaming"] is True
    # Anthropic+json_schema is honestly rejected per the catalog fix.
    assert claude_caps["response_format"] == []
    gpt_caps = by_id["openai/gpt-4o"]["aitelier_request_caps"]
    assert "json_schema" in gpt_caps["response_format"]
    assert "json_object" in gpt_caps["response_format"]


# --- chat_completion passthrough -------------------------------------------


@pytest.mark.asyncio
async def test_chat_completion_returns_litellm_response_verbatim(monkeypatch):
    """chat_completion forwards the LiteLLM JSON response unchanged."""
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={
        "id": "chatcmpl-up", "object": "chat.completion",
        "model": "claude-sonnet",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    })
    fake_client.post = AsyncMock(return_value=fake_resp)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )

    resp = await chat_completion({
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp["choices"][0]["message"]["content"] == "Hello"
    assert resp["usage"]["total_tokens"] == 4


@pytest.mark.asyncio
async def test_chat_completion_maps_429_to_rate_limited(monkeypatch):
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 429
    fake_resp.text = "rate limited"
    fake_client.post = AsyncMock(return_value=fake_resp)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )

    with pytest.raises(LLMError) as exc_info:
        await chat_completion({
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "x"}],
        })
    assert exc_info.value.error_type == "RateLimited"
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_chat_completion_propagates_unsupported_response_format(monkeypatch):
    """UnsupportedResponseFormat fires on LiteLLM-routed providers without
    json_schema support. Ollama-routed models (`local`, `ollama/*`) bypass
    LiteLLM and use Ollama's native structured-output `format` field, so
    they're NOT in the unsupported-set anymore. Test against a prefix that
    stays on the LiteLLM path."""
    monkeypatch.setattr(
        "aitelier.providers.llm._RESPONSE_FORMAT_SUPPORT",
        {"unsupported-": set()},
    )
    with pytest.raises(UnsupportedResponseFormat):
        await chat_completion({
            "model": "unsupported-model",
            "messages": [{"role": "user", "content": "x"}],
            "response_format": {"type": "json_schema", "schema": {}},
        })


@pytest.mark.asyncio
async def test_chat_completion_reclassifies_upstream_preflight_response_format(
    monkeypatch,
):
    """Defense in depth: if upstream returns a 5xx whose body smells like an
    OpenAI-param preflight failure on `response_format`, surface a typed 400
    (UnsupportedResponseFormat) so consumers don't see a raw Python traceback.

    Catalog-allowed model + json_schema → would normally pass through, so the
    only protection against an upstream regression is this reclassifier.
    """
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 502
    fake_resp.text = (
        "litellm.APIConnectionError: 'json_schema'\n"
        "  File \"litellm/utils.py\", line 3974, in get_optional_params"
    )
    fake_client.post = AsyncMock(return_value=fake_resp)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    with pytest.raises(UnsupportedResponseFormat):
        await chat_completion({
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "x"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "r", "schema": {}, "strict": True},
            },
        })


@pytest.mark.asyncio
async def test_chat_completion_does_not_reclassify_unrelated_5xx(monkeypatch):
    """A 5xx without response_format on the request stays a ProviderError —
    we only reclassify when the request actually asked for structured output."""
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "internal server error"
    fake_client.post = AsyncMock(return_value=fake_resp)

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    with pytest.raises(LLMError) as exc_info:
        await chat_completion({
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "x"}],
        })
    assert exc_info.value.error_type == "ProviderError"


def _fake_stream_response(lines: list[str]):
    class _StreamCM:
        status_code = 200
        text = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def aread(self):
            return b""

        async def aiter_lines(self):
            for line in lines:
                yield line

    return _StreamCM()


def _fake_error_stream_response(status_code: int, text: str):
    class _ErrCM:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def aread(self):
            return b""

    return _ErrCM()


@pytest.mark.asyncio
async def test_chat_completion_stream_reclassifies_preflight_response_format(
    monkeypatch,
):
    """Streaming path must apply the same reclassifier as the non-stream path
    — same upstream-regression class would otherwise leak a 5xx traceback to
    SSE consumers."""
    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=_fake_error_stream_response(
        502,
        "litellm.APIConnectionError: 'json_schema'\n"
        "  File \"litellm/utils.py\", line 3974, in get_optional_params",
    ))

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )
    with pytest.raises(UnsupportedResponseFormat):
        async for _ in chat_completion_stream({
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "x"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "r", "schema": {}, "strict": True},
            },
        }):
            pass


@pytest.mark.asyncio
async def test_chat_completion_stream_yields_parsed_chunks(monkeypatch):
    """The stream helper yields already-parsed OpenAI chunk dicts."""
    lines = [
        'data: {"choices":[{"delta":{"content":"Hello"},"index":0,'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" world"},"index":0,'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
        "data: [DONE]",
    ]

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=_fake_stream_response(lines))

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )

    chunks: list[dict] = []
    async for chunk in chat_completion_stream({
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    }):
        chunks.append(chunk)

    pieces = [
        ch["choices"][0]["delta"].get("content", "")
        for ch in chunks
        if ch.get("choices")
    ]
    assert "".join(pieces) == "Hello world"
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_chat_completion_stream_skips_malformed_lines(monkeypatch):
    lines = [
        "data: not valid json",
        'data: {"choices":[{"delta":{"content":"ok"},"index":0,'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=_fake_stream_response(lines))

    async def fake_get_shared():
        return fake_client
    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )

    chunks: list[dict] = []
    async for chunk in chat_completion_stream({
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    }):
        chunks.append(chunk)

    pieces = [
        ch["choices"][0]["delta"].get("content", "")
        for ch in chunks
        if ch.get("choices")
    ]
    assert "".join(pieces) == "ok"


# --- Ollama bypass --------------------------------------------------------


def test_routes_to_ollama_recognises_local_and_ollama_prefix():
    assert _routes_to_ollama("local") is True
    assert _routes_to_ollama("ollama/qwen3:8b") is True
    assert _routes_to_ollama("ollama/llama3.2") is True
    assert _routes_to_ollama("claude-haiku") is False
    assert _routes_to_ollama("anthropic/claude-opus-4-7") is False
    assert _routes_to_ollama("agent:claude") is False


def test_resolve_ollama_model_strips_prefix_and_resolves_local():
    assert _resolve_ollama_model("ollama/llama3.2") == "llama3.2"
    assert _resolve_ollama_model("ollama/qwen3:8b") == "qwen3:8b"
    # `local` resolves via OllamaConfig.default_model (default qwen3:8b).
    assert _resolve_ollama_model("local") == "qwen3:8b"


def test_build_ollama_request_translates_max_tokens_and_temperature():
    out = _build_ollama_request({
        "model": "ollama/qwen3:8b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
        "temperature": 0.2,
        "top_p": 0.9,
    }, stream=False)
    assert out["model"] == "qwen3:8b"
    assert out["stream"] is False
    # `think` is gated on `reasoning_effort` — absent by default so the
    # whole `num_predict` budget reaches user-visible content.
    assert "think" not in out
    assert out["options"]["num_predict"] == 50
    assert out["options"]["temperature"] == 0.2
    assert out["options"]["top_p"] == 0.9


def test_build_ollama_request_enables_think_when_reasoning_effort_set():
    """OpenAI's `reasoning_effort` is the standard signal that the caller
    wants thinking. Pass-through to Ollama's `think: True`."""
    out = _build_ollama_request({
        "model": "ollama/qwen3:8b",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "medium",
    }, stream=False)
    assert out["think"] is True


def test_build_ollama_request_omits_think_without_reasoning_signal():
    """No `reasoning_effort` → no `think`. Thinking-capable models then
    treat the call as a plain completion and emit content, not hidden
    chain-of-thought that vanishes under a tight max_tokens."""
    out = _build_ollama_request({
        "model": "local",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    }, stream=False)
    assert "think" not in out


def test_build_ollama_request_prefers_max_completion_tokens():
    """OpenAI reasoning-model field wins over legacy `max_tokens`."""
    out = _build_ollama_request({
        "model": "local",
        "messages": [],
        "max_tokens": 50,
        "max_completion_tokens": 500,
    }, stream=False)
    assert out["options"]["num_predict"] == 500


def test_build_ollama_request_response_format_json_object():
    out = _build_ollama_request({
        "model": "local",
        "messages": [],
        "response_format": {"type": "json_object"},
    }, stream=False)
    assert out["format"] == "json"


def test_build_ollama_request_response_format_json_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = _build_ollama_request({
        "model": "local",
        "messages": [],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "X", "schema": schema, "strict": True},
        },
    }, stream=False)
    assert out["format"] == schema


def test_ollama_to_chat_completion_surfaces_thinking_as_reasoning_content():
    """When content is empty but `thinking` has text, the OpenAI response
    carries it as `reasoning_content` so consumers see what the model
    produced even after a tight `max_tokens` budget cut it off."""
    ollama_resp = {
        "model": "qwen3:8b",
        "message": {
            "role": "assistant",
            "content": "",
            "thinking": "Let me work through this step by step...",
        },
        "done": True,
        "done_reason": "length",
        "prompt_eval_count": 17,
        "eval_count": 50,
    }
    out = _ollama_to_chat_completion(ollama_resp, request_model="local")
    msg = out["choices"][0]["message"]
    assert msg["content"] == ""
    assert msg["reasoning_content"] == "Let me work through this step by step..."
    assert out["choices"][0]["finish_reason"] == "length"
    assert out["usage"]["prompt_tokens"] == 17
    assert out["usage"]["completion_tokens"] == 50
    assert out["usage"]["total_tokens"] == 67


def test_ollama_to_chat_completion_visible_content():
    ollama_resp = {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": "Hello!"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 5,
        "eval_count": 3,
    }
    out = _ollama_to_chat_completion(ollama_resp, request_model="local")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Hello!"
    assert "reasoning_content" not in msg
    assert out["choices"][0]["finish_reason"] == "stop"


def test_ollama_to_chat_completion_tool_calls_passthrough():
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "add", "arguments": "{}"}}]
    out = _ollama_to_chat_completion({
        "model": "llama3.2",
        "message": {"role": "assistant", "content": "", "tool_calls": tool_calls},
        "done": True,
        "done_reason": "stop",
    }, request_model="ollama/llama3.2")
    assert out["choices"][0]["message"]["tool_calls"] == tool_calls


@pytest.mark.asyncio
async def test_chat_completion_bypasses_litellm_for_ollama(monkeypatch):
    """End-to-end of the bypass: a `local` request hits Ollama's /api/chat,
    not LiteLLM's /chat/completions."""
    captured = {}

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={
        "message": {"role": "assistant", "content": "",
                    "thinking": "thinking text"},
        "done": True, "done_reason": "length",
        "prompt_eval_count": 1, "eval_count": 50,
    })

    async def fake_post(url, **kw):
        captured["url"] = url
        captured["body"] = kw.get("json")
        return fake_resp

    fake_client.post = fake_post

    async def fake_get_shared():
        return fake_client

    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared,
    )

    out = await chat_completion({
        "model": "local",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    })
    # Sanity: we hit Ollama, not LiteLLM.
    assert "/api/chat" in captured["url"]
    assert "chat/completions" not in captured["url"]
    # Acceptance: thinking surfaces as reasoning_content.
    assert out["choices"][0]["message"]["reasoning_content"] == "thinking text"
    assert out["choices"][0]["finish_reason"] == "length"
