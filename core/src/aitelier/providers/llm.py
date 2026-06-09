"""LiteLLM provider — passthrough for OpenAI Chat Completions and Embeddings.

aitelier's public inference contract is OpenAI shape. LiteLLM already speaks
that shape, so the LLM path is a thin pass-through: forward the request,
forward the response. We layer two concerns on top:

  - `_normalize_response_format` — apply per-provider capability gates
    (e.g. Anthropic doesn't support `json_object`; substitute a system-prompt
    directive). Hard-rejects with `UnsupportedResponseFormat` rather than
    silently downgrading `json_schema`.
  - Shared httpx client — pooled connections cut TLS/connect overhead off
    every request.

Errors propagate as `LLMError` (the endpoint maps them to OpenAI error
responses).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aitelier.config import get_config
from aitelier.errors import classify_error

logger = logging.getLogger("aitelier.llm")

# Per-provider response_format support. Used to soft-fall-back for `json_object`
# (intent: "give me JSON" — a system-prompt directive substitutes well) and to
# hard-reject `json_schema` on providers that can't enforce structured output
# (intent: "validate exact shape" — can't be honored by a system prompt).
#
# Key is a model-alias prefix; unmatched models pass through and any upstream
# 4xx surfaces as ProviderError. Update this table when adding a new provider.
_RESPONSE_FORMAT_SUPPORT: dict[str, set[str]] = {
    "openai/":    {"json_object", "json_schema"},
    "gpt-":       {"json_object", "json_schema"},
    # LiteLLM's Anthropic adapter currently raises in `get_optional_params`
    # when handed OpenAI-shape `json_schema` (strict or not), so we can't
    # honestly advertise it. Consumers asking for json_schema on a claude*
    # model get a typed UnsupportedResponseFormat (400) up front instead of
    # a 502 traceback from upstream. `json_object` still soft-falls back to
    # a system-prompt directive via _apply_response_format_gates.
    "claude":     set(),
    "anthropic/": set(),
    # `local` and `ollama/*` bypass LiteLLM and call Ollama's /api/chat
    # directly. `_build_ollama_request` maps `json_object` → Ollama's
    # `format: "json"` and `json_schema` → Ollama's schema-mode `format:
    # <schema>` — both are native and reliable. Advertise that.
    "ollama/":    {"json_object", "json_schema"},
    "local":      {"json_object", "json_schema"},
    # Bare `qwen-…` / `llama-…` model ids that go through LiteLLM (not
    # the Ollama bypass) have inconsistent json support upstream.
    "qwen":       set(),
    "llama":      set(),
}


def _provider_supports(model: str, fmt: str) -> bool | None:
    """Return True/False if we know whether `model` supports response_format
    type `fmt`, or None if unknown — caller should pass through to LiteLLM."""
    for prefix, supported in _RESPONSE_FORMAT_SUPPORT.items():
        if model.startswith(prefix):
            return fmt in supported
    return None


def model_response_format_capabilities(model: str) -> list[str] | None:
    """Sorted list of supported `response_format.type` values for `model`,
    or None when we have no registry entry (caller should advertise
    "unknown" rather than "none")."""
    for prefix, supported in _RESPONSE_FORMAT_SUPPORT.items():
        if model.startswith(prefix):
            return sorted(supported)
    return None


_JSON_DIRECTIVE = "Return JSON only — no markdown fences, no preamble."


class UnsupportedResponseFormat(ValueError):
    """Raised when the requested response_format can't be honored on the
    chosen model and there's no safe fallback."""


def _apply_response_format_gates(
    model: str, body: dict,
) -> dict:
    """Apply per-provider response_format gates to an OpenAI-shape request body.

    Returns a new body dict (the input is not mutated). May raise
    `UnsupportedResponseFormat` for hard rejections (json_schema on a
    provider that can't enforce it).

    For `json_object` on providers without native support, the format is
    stripped and a system message is prepended/extended with a directive —
    the consumer still gets JSON, just via prompt engineering.
    """
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return body
    fmt_type = rf.get("type")
    if fmt_type not in ("json_object", "json_schema"):
        return body

    supported = _provider_supports(model, fmt_type)
    if supported is None or supported:
        return body

    if fmt_type == "json_schema":
        raise UnsupportedResponseFormat(
            f"{model} does not support response_format=json_schema. Use "
            "response_format=json_object (soft-falls-back via system prompt), "
            "switch to gpt-* / openai/* for native enforcement, or drop "
            "response_format.",
        )

    # json_object — soft fallback: strip format, prepend JSON directive to
    # the (possibly new) system message.
    logger.debug(
        "response_format=json_object not supported by %s; "
        "stripping and injecting system-prompt directive", model,
    )
    new_body = {k: v for k, v in body.items() if k != "response_format"}
    messages = list(new_body.get("messages", []))
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        first["content"] = f"{_JSON_DIRECTIVE}\n\n{first.get('content', '')}"
        messages[0] = first
    else:
        messages.insert(0, {"role": "system", "content": _JSON_DIRECTIVE})
    new_body["messages"] = messages
    return new_body


# Shared HTTP client for LiteLLM calls — pooled connections cut TLS/connect
# overhead off every request. Initialized lazily on first use; closed on
# server shutdown via close_shared_client().
#
# httpx.AsyncClient binds its connection pool to the event loop that created
# it; sharing across loops is unsafe (see
# https://www.python-httpx.org/async/#async-environments). Production runs
# one loop so we never hit this, but pytest-asyncio gives each test a fresh
# loop. We track the originating loop and rebuild the client when the loop
# changes — that way the production singleton property holds AND test
# isolation works without per-test cleanup hooks.
_shared_client: httpx.AsyncClient | None = None
_shared_client_loop: asyncio.AbstractEventLoop | None = None
_client_lock = asyncio.Lock()


def _client_is_stale(client: httpx.AsyncClient | None,
                     loop: asyncio.AbstractEventLoop | None) -> bool:
    """A cached client is stale when it's missing, closed, or bound to a
    different running loop than the caller's."""
    if client is None or client.is_closed:
        return True
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        return True
    return loop is not current


async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client, _shared_client_loop
    if not _client_is_stale(_shared_client, _shared_client_loop):
        return _shared_client  # type: ignore[return-value]
    async with _client_lock:
        if _client_is_stale(_shared_client, _shared_client_loop):
            # Drop the prior client without awaiting aclose: its sockets
            # belong to a loop we no longer have. Garbage collection will
            # release the underlying fds.
            _shared_client = httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=10),
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=40,
                ),
            )
            _shared_client_loop = asyncio.get_running_loop()
    return _shared_client


async def close_shared_client() -> None:
    """Close the module-level client. Call from server lifespan shutdown.

    No-op when the client is from a different event loop — aclose() would
    raise `Event loop is closed` since httpx tries to close sockets via the
    original loop. Letting GC reclaim the fds is the documented safe path.
    """
    global _shared_client, _shared_client_loop
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = None
        _shared_client_loop = None
        return
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if _shared_client_loop is current:
        await _shared_client.aclose()
    _shared_client = None
    _shared_client_loop = None


class LLMError(Exception):
    """Raised by the LLM passthrough on upstream failure. Carries an
    `error_type` (classified via `errors.classify_error`) and the original
    exception/status for the endpoint to render."""
    def __init__(
        self, error_type: str, message: str, *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


def _classify_llm_status(status: int) -> str:
    """Map an upstream HTTP status to aitelier's error taxonomy."""
    if status == 429:
        return "RateLimited"
    if status in (401, 403):
        return "AuthError"
    if status >= 500:
        return "ProviderError"
    return "ProviderError"


def _safe_connect_message(exc: BaseException) -> str:
    """Sanitize a transport-layer failure (DNS, connect, read timeout).

    `str(exc)` from httpx routinely includes the upstream URL/host. In
    hosted mode that leaks internal topology to consumers via the error
    envelope. Surface only the exception class + a generic phrase and
    log the full string server-side.
    """
    cls = type(exc).__name__
    logger.warning("LLM transport failure: %s: %s", cls, exc)
    return f"Upstream transport failure ({cls})"


def _safe_upstream_message(status: int, resp: httpx.Response) -> str:
    """Build a sanitized error message from an upstream LiteLLM response.

    The raw response body can contain provider names, model SKUs,
    internal request IDs, or echoed API key fragments — all of which
    leak to /v1/* consumers as 4xx/5xx response bodies in hosted mode.
    We surface only status + a short canonical phrase so consumers can
    branch on type and operators can correlate via the run row's
    `error_msg` (and full body in the log).
    """
    canonical = {
        "RateLimited":   "Upstream rate limit",
        "AuthError":     "Upstream auth failure",
        "ProviderError": "Upstream provider error",
    }.get(_classify_llm_status(status), "Upstream provider error")
    body_preview = resp.text[:500] if resp.text else ""
    if body_preview:
        logger.warning(
            "Upstream %d (%s); response body: %s",
            status, canonical, body_preview,
        )
    return f"{canonical} (HTTP {status})"


# ---------------------------------------------------------------------------
# Ollama bypass routing — direct /api/chat call for `local` and `ollama/*`
# ---------------------------------------------------------------------------
#
# LiteLLM's Ollama adapter does not surface Ollama's `message.thinking`
# field in non-streaming responses. For reasoning models (qwen3,
# deepseek-r1) under tight `max_tokens` budgets this leaves the consumer
# with content="" and no recoverable trace of what the model did.
#
# We bypass LiteLLM for these routes and map Ollama's documented response
# shape (https://github.com/ollama/ollama/blob/main/docs/api.md) to OpenAI's
# ChatCompletion shape ourselves:
#
#   Ollama                              OpenAI ChatCompletion
#   ────────────────────────────────────────────────────────
#   message.content                     choices[0].message.content
#   message.thinking                    choices[0].message.reasoning_content
#   message.tool_calls                  choices[0].message.tool_calls
#   done_reason: "length"               choices[0].finish_reason: "length"
#   done_reason: "stop"                 choices[0].finish_reason: "stop"
#   eval_count                          usage.completion_tokens
#   prompt_eval_count                   usage.prompt_tokens


def _routes_to_ollama(model: str) -> bool:
    """`local` resolves via OllamaConfig.default_model; `ollama/...` strips
    the prefix. Everything else (claude-*, anthropic/*, openai/*, gpt-*,
    agent:*) goes through LiteLLM."""
    return model == "local" or model.startswith("ollama/")


def _resolve_ollama_model(model: str) -> str:
    if model == "local":
        return get_config().ollama.default_model
    return model[len("ollama/"):]  # strip prefix


def _build_ollama_request(body: dict, *, stream: bool) -> dict:
    """OpenAI ChatCompletions body → Ollama /api/chat body."""
    options: dict[str, Any] = {}
    # `max_completion_tokens` is the OpenAI reasoning-model field; fall back
    # to `max_tokens` for legacy/non-reasoning calls. Either maps to Ollama's
    # `num_predict` (total output budget; Ollama doesn't split visible vs
    # reasoning the way OpenAI does).
    mct = body.get("max_completion_tokens") or body.get("max_tokens")
    if mct is not None:
        options["num_predict"] = int(mct)
    if body.get("temperature") is not None:
        options["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        options["top_p"] = body["top_p"]
    out: dict[str, Any] = {
        "model": _resolve_ollama_model(body["model"]),
        "messages": body["messages"],
        "stream": stream,
    }
    # Gate Ollama's `think` on consumer intent. Forcing it on caused
    # thinking-capable models like qwen3 to silently burn the entire
    # `num_predict` budget on hidden reasoning under tight max_tokens,
    # returning `content=""` even though tokens were generated. OpenAI's
    # `reasoning_effort` is the standard signal that the caller wants
    # reasoning; we map any non-None value to `think: True`.
    if body.get("reasoning_effort") is not None:
        out["think"] = True
    if options:
        out["options"] = options
    rf = body.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_object":
            out["format"] = "json"
        elif rf.get("type") == "json_schema":
            schema = rf.get("json_schema", rf).get("schema") or rf.get("schema")
            if schema is not None:
                out["format"] = schema
    return out


_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "load": "stop",
}


def _ollama_to_chat_completion(
    ollama_resp: dict, *, request_model: str,
) -> dict:
    """Ollama /api/chat response → OpenAI ChatCompletion."""
    msg = ollama_resp.get("message") or {}
    out_msg: dict[str, Any] = {"role": msg.get("role", "assistant")}
    out_msg["content"] = msg.get("content") or ""
    thinking = msg.get("thinking")
    if thinking:
        out_msg["reasoning_content"] = thinking
    if msg.get("tool_calls"):
        out_msg["tool_calls"] = msg["tool_calls"]
    done_reason = ollama_resp.get("done_reason") or "stop"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model,
        "choices": [{
            "index": 0,
            "message": out_msg,
            "finish_reason": _FINISH_REASON_MAP.get(done_reason, "stop"),
            "logprobs": None,
        }],
        "usage": {
            "prompt_tokens": ollama_resp.get("prompt_eval_count", 0) or 0,
            "completion_tokens": ollama_resp.get("eval_count", 0) or 0,
            "total_tokens": (
                (ollama_resp.get("prompt_eval_count", 0) or 0)
                + (ollama_resp.get("eval_count", 0) or 0)
            ),
        },
    }


async def _chat_completion_via_ollama(
    body: dict, *, timeout: int,
) -> dict:
    cfg = get_config().ollama
    req_body = _build_ollama_request(body, stream=False)
    client = await get_shared_client()
    try:
        resp = await client.post(
            f"{cfg.host_base_url()}/api/chat",
            json=req_body,
            timeout=httpx.Timeout(timeout, connect=10),
        )
    except Exception as exc:
        raise LLMError(classify_error(exc), _safe_connect_message(exc)) from exc
    if resp.status_code >= 400:
        raise LLMError(
            _classify_llm_status(resp.status_code),
            _safe_upstream_message(resp.status_code, resp),
            status_code=resp.status_code,
        )
    return _ollama_to_chat_completion(resp.json(), request_model=body["model"])


async def _chat_completion_via_ollama_stream(
    body: dict, *, timeout: int,
) -> AsyncIterator[dict]:
    """Stream Ollama → OpenAI chunks.

    Ollama emits one JSON object per line (NDJSON), each with a partial
    `message.content` and/or `message.thinking`. The final line has
    `done: true` + usage counters."""
    cfg = get_config().ollama
    req_body = _build_ollama_request(body, stream=True)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    client = await get_shared_client()
    first = True

    async with client.stream(
        "POST",
        f"{cfg.host_base_url()}/api/chat",
        json=req_body,
        timeout=httpx.Timeout(timeout, connect=10),
    ) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            raise LLMError(
                _classify_llm_status(resp.status_code),
                _safe_upstream_message(resp.status_code, resp),
                status_code=resp.status_code,
            )
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message") or {}
            delta: dict[str, Any] = {}
            if first:
                delta["role"] = "assistant"
                first = False
            if msg.get("content"):
                delta["content"] = msg["content"]
            if msg.get("thinking"):
                delta["reasoning_content"] = msg["thinking"]
            if msg.get("tool_calls"):
                delta["tool_calls"] = msg["tool_calls"]

            choices: list[dict[str, Any]] = []
            if delta:
                choices.append({
                    "index": 0, "delta": delta, "finish_reason": None,
                })
            if chunk.get("done"):
                done_reason = chunk.get("done_reason") or "stop"
                choices.append({
                    "index": 0, "delta": {},
                    "finish_reason": _FINISH_REASON_MAP.get(done_reason, "stop"),
                })
                yield {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body["model"],
                    "choices": choices,
                    "usage": {
                        "prompt_tokens": chunk.get("prompt_eval_count", 0) or 0,
                        "completion_tokens": chunk.get("eval_count", 0) or 0,
                        "total_tokens": (
                            (chunk.get("prompt_eval_count", 0) or 0)
                            + (chunk.get("eval_count", 0) or 0)
                        ),
                    },
                }
                return
            if choices:
                yield {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body["model"],
                    "choices": choices,
                }


# ---------------------------------------------------------------------------
# chat_completion() — OpenAI passthrough
# ---------------------------------------------------------------------------


async def chat_completion(
    body: dict, *, timeout: int = 60,
) -> dict:
    """Forward an OpenAI Chat Completions request to LiteLLM, return its
    OpenAI-shape response unchanged.

    Ollama-routed models (`local`, `ollama/*`) bypass LiteLLM and call
    Ollama directly — LiteLLM's adapter drops `message.thinking` which
    breaks reasoning-model contracts.

    Applies `_apply_response_format_gates` first. Raises
    `UnsupportedResponseFormat` for hard rejections and `LLMError` for
    transport/upstream failures.
    """
    if _routes_to_ollama(body["model"]):
        return await _chat_completion_via_ollama(body, timeout=timeout)
    cfg = get_config().litellm
    body = _apply_response_format_gates(body["model"], body)

    client = await get_shared_client()
    try:
        resp = await client.post(
            f"{cfg.base_url}/chat/completions",
            headers=_litellm_headers(cfg, body),
            json=body,
            timeout=httpx.Timeout(timeout, connect=10),
        )
    except Exception as exc:
        raise LLMError(classify_error(exc), _safe_connect_message(exc)) from exc
    if resp.status_code >= 400:
        _raise_for_preflight_response_format(body, resp)
        raise LLMError(
            _classify_llm_status(resp.status_code),
            _safe_upstream_message(resp.status_code, resp),
            status_code=resp.status_code,
        )
    return resp.json()


def _litellm_headers(cfg, body: dict) -> dict[str, str]:
    """Headers for a LiteLLM chat-completions call. Auto-attaches the
    Anthropic prompt-caching beta header when any message content block
    carries a `cache_control` marker — LiteLLM honors the marker on
    `claude*` / `anthropic/*` routes only when the beta header is set,
    so callers using `cache_control` would otherwise see zero cache
    hits and pay full input cost on every turn.

    Safe to send unconditionally on Anthropic routes (Anthropic ignores
    it when no cache_control is present), but we keep it conditional so
    non-Anthropic LiteLLM routes don't see an unexpected header."""
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    if _wants_anthropic_prompt_caching(body):
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"
    return headers


def _wants_anthropic_prompt_caching(body: dict) -> bool:
    """True when the request targets a claude/anthropic model AND any
    message content block declares `cache_control`. The beta header is
    only useful for that combination; sending it elsewhere is harmless
    but we'd rather keep request headers minimal."""
    model = body.get("model", "")
    if not (model.startswith("claude") or model.startswith("anthropic/")):
        return False
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("cache_control"):
                    return True
        # Top-level cache_control on a message (Anthropic also accepts this).
        if isinstance(msg, dict) and msg.get("cache_control"):
            return True
    return False


def _raise_for_preflight_response_format(body: dict, resp: httpx.Response) -> None:
    """Defense in depth: when LiteLLM's pre-flight parameter validation
    blows up on an OpenAI-shape `response_format` (typical signature:
    `get_optional_params` choking on claude+json_schema), surface a
    typed 400 instead of leaking a 5xx with a Python traceback.

    We only reclassify when the request actually carried `response_format`
    AND the error body smells like an OpenAI-param translation failure.
    Anything else falls through to the generic LLMError path."""
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return
    text = resp.text[:1000]
    if not text:
        return
    needles = ("json_schema", "response_format", "get_optional_params")
    if any(n in text for n in needles):
        fmt = rf.get("type", "<unset>")
        raise UnsupportedResponseFormat(
            f"{body.get('model')} rejected response_format={fmt} at the "
            "provider's pre-flight validation. Try response_format=json_object "
            "or switch to a model with native structured-output support.",
        )


async def chat_completion_stream(
    body: dict, *, timeout: int = 60,
) -> AsyncIterator[dict]:
    """Stream an OpenAI Chat Completions request to LiteLLM, yielding parsed
    chunk dicts (already in OpenAI chunk shape).

    Ollama-routed models bypass LiteLLM (see `chat_completion`).

    No retries: once tokens are flowing, replay is unsafe. The caller is
    responsible for SSE-serializing the chunks and persisting run state.
    """
    if _routes_to_ollama(body["model"]):
        async for chunk in _chat_completion_via_ollama_stream(
            body, timeout=timeout,
        ):
            yield chunk
        return
    cfg = get_config().litellm
    body = _apply_response_format_gates(body["model"], body)
    body = dict(body)
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})

    client = await get_shared_client()
    async with client.stream(
        "POST",
        f"{cfg.base_url}/chat/completions",
        headers=_litellm_headers(cfg, body),
        json=body,
        timeout=httpx.Timeout(timeout, connect=10),
    ) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            _raise_for_preflight_response_format(body, resp)
            raise LLMError(
                _classify_llm_status(resp.status_code),
                _safe_upstream_message(resp.status_code, resp),
                status_code=resp.status_code,
            )
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------------------
# embeddings() — OpenAI passthrough
# ---------------------------------------------------------------------------


async def embeddings(body: dict, *, timeout: int = 30) -> dict:
    """Forward an OpenAI Embeddings request to LiteLLM, return its response."""
    cfg = get_config().litellm
    client = await get_shared_client()
    try:
        resp = await client.post(
            f"{cfg.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=httpx.Timeout(timeout, connect=10),
        )
    except Exception as exc:
        raise LLMError(classify_error(exc), _safe_connect_message(exc)) from exc
    if resp.status_code >= 400:
        raise LLMError(
            _classify_llm_status(resp.status_code),
            _safe_upstream_message(resp.status_code, resp),
            status_code=resp.status_code,
        )
    return resp.json()


# ---------------------------------------------------------------------------
# list_models() — surface LiteLLM's /models with our capability registry
# ---------------------------------------------------------------------------


async def list_models() -> list[dict]:
    """Return the model list LiteLLM advertises, each annotated with
    `response_format` capabilities we know about. Used by `GET /v1/models`."""
    cfg = get_config().litellm
    client = await get_shared_client()
    try:
        resp = await client.get(
            f"{cfg.base_url}/models",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=5.0,
        )
    except Exception as exc:
        raise LLMError(classify_error(exc), _safe_connect_message(exc)) from exc
    if resp.status_code != 200:
        raise LLMError(
            _classify_llm_status(resp.status_code),
            _safe_upstream_message(resp.status_code, resp),
            status_code=resp.status_code,
        )
    data = resp.json()
    raw_models = data.get("data", []) if isinstance(data, dict) else data
    out: list[dict] = []
    for m in raw_models:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid:
            continue
        entry: dict[str, Any] = {
            "id": mid,
            "object": "model",
            "owned_by": m.get("owned_by", "litellm"),
        }
        supports = model_response_format_capabilities(mid)
        if supports is not None:
            entry["response_format"] = supports
        # Declarative request-field caps. LLM-routed models accept the full
        # OpenAI surface; agent-routed entries (built in server.py) declare
        # the stricter agent-path gates. Lets consumers (model pickers,
        # auto-strippers) avoid hard-coding aitelier-specific quirks.
        # `response_format` is only declared when we have a registry entry
        # for the model — for unknowns the request path passes through to
        # LiteLLM, so the honest answer is "we don't know."
        caps: dict[str, Any] = {
            "tools": True,
            "tool_choice": True,
            "n_gt_1": True,
            "top_p": True,
            "streaming": True,
        }
        if supports is not None:
            caps["response_format"] = supports
        entry["aitelier_request_caps"] = caps
        out.append(entry)
    return out
