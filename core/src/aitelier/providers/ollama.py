"""Direct Ollama routing — bypasses LiteLLM for `local` and `ollama/*` models.

LiteLLM's Ollama adapter does not surface Ollama's `message.thinking`
field in non-streaming responses. For reasoning models (qwen3,
deepseek-r1) under tight `max_tokens` budgets this leaves the consumer
with content="" and no recoverable trace of what the model did. We
bypass LiteLLM for these routes and map Ollama's documented response
shape directly to OpenAI's ChatCompletion shape.

Mapping (https://github.com/ollama/ollama/blob/main/docs/api.md):
    Ollama                              OpenAI ChatCompletion
    ────────────────────────────────────────────────────────
    message.content                     choices[0].message.content
    message.thinking                    choices[0].message.reasoning_content
    message.tool_calls                  choices[0].message.tool_calls
    done_reason: "length"               choices[0].finish_reason: "length"
    done_reason: "stop"                 choices[0].finish_reason: "stop"
    eval_count                          usage.completion_tokens
    prompt_eval_count                   usage.prompt_tokens

Shared helpers (`get_shared_client`, `LLMError`, status/error
classifiers) live in `providers/llm.py` and are imported here. The
back-import from `llm.py → ollama.py` is lazy (inside `chat_completion`)
to avoid a load-time cycle.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aitelier.config import get_config
from aitelier.errors import classify_error
from aitelier.providers import llm as _llm
from aitelier.providers.llm import (
    LLMError,
    _classify_llm_status,
    _safe_connect_message,
    _safe_upstream_message,
)

# `get_shared_client` is looked up via the llm module rather than imported
# directly so that test patches on `aitelier.providers.llm.get_shared_client`
# affect calls from this module too (they bind into llm.py's namespace, not
# ollama.py's — see test_llm.py:test_chat_completion_bypasses_litellm_for_ollama).


_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "load": "stop",
}


def routes_to_ollama(model: str) -> bool:
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
    # Map OpenAI's `reasoning_effort` to Ollama's binary `think` toggle.
    # Hybrid-reasoning models (qwen3 family) default to thinking ON and
    # will silently burn the `num_predict` budget on hidden reasoning under
    # tight max_tokens, returning `content=""` with `finish_reason=length`
    # — deepread hit this in production for 8 days on qwen3:8b summarize.
    # `minimal`/`none` are the OpenAI signals for "least reasoning" — map
    # both to `think: false`. `low`/`medium`/`high` enable thinking. When
    # the caller doesn't set `reasoning_effort` we leave the field
    # unspecified so Ollama applies the model default.
    effort = body.get("reasoning_effort")
    if isinstance(effort, str):
        out["think"] = effort.lower() not in ("none", "minimal")
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


async def chat_completion_via_ollama(
    body: dict, *, timeout: int,
) -> dict:
    cfg = get_config().ollama
    req_body = _build_ollama_request(body, stream=False)
    client = await _llm.get_shared_client()
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


async def chat_completion_via_ollama_stream(
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
    client = await _llm.get_shared_client()
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
