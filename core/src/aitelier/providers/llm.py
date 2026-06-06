"""LLM provider adapters — calls LiteLLM proxy via HTTP.

Primitives:
  complete() — structured chat completion (deepread contract)
  embed()    — batch embeddings
  call_llm() — legacy wrapper around complete() for fan-out tasks
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from aitelier.config import get_config
from aitelier.errors import classify_error

# Shared HTTP client for LiteLLM calls — pooled connections cut TLS/connect
# overhead off every request. Initialized lazily on first use; closed on
# server shutdown via close_shared_client().
_shared_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        async with _client_lock:
            if _shared_client is None or _shared_client.is_closed:
                _shared_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(60, connect=10),
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=40,
                    ),
                )
    return _shared_client


async def close_shared_client() -> None:
    """Close the module-level client. Call from server lifespan shutdown."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None

# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

async def complete(
    model: str,
    messages: list[dict],
    *,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    timeout: int = 60,
    run_id: str = "",
    trace_tag: str | None = None,
) -> dict:
    """Structured chat completion via LiteLLM proxy.

    Matches the deepread CompleteOpts contract. Retries transient failures
    up to 3 times.
    """
    cfg = get_config().litellm
    start = time.monotonic()
    last_exc: Exception | None = None

    # Build the messages list with optional system prompt
    full_messages: list[dict] = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    # Build the request body
    body: dict[str, Any] = {
        "model": model,
        "messages": full_messages,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if response_format:
        body["response_format"] = response_format

    for attempt in range(3):
        try:
            client = await get_shared_client()
            resp = await client.post(
                f"{cfg.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=httpx.Timeout(timeout, connect=10),
            )
            resp.raise_for_status()
            data = resp.json()

            elapsed = time.monotonic() - start
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
            finish_reason = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})

            usage = {
                "input_tokens": usage_raw.get("prompt_tokens", 0),
                "output_tokens": usage_raw.get("completion_tokens", 0),
                "total_tokens": usage_raw.get("total_tokens", 0),
            }

            # Parse JSON if response_format was json_schema or json_object
            parsed = None
            if response_format and response_format.get("type") in ("json_schema", "json_object"):
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    pass

            return {
                "kind": "complete",
                "provider": model,
                "status": "ok",
                "duration_s": round(elapsed, 2),
                "run_id": run_id,
                "trace_id": run_id,
                "content": content,
                "parsed": parsed,
                "usage": usage,
                "finish_reason": finish_reason,
                "cost_usd": usage_raw.get("cost"),
                "error_type": None,
                "error_msg": None,
            }
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code < 500:
                break
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(2**attempt)

    elapsed = time.monotonic() - start
    return _complete_error(model, run_id, elapsed, last_exc)


# ---------------------------------------------------------------------------
# complete_stream()
# ---------------------------------------------------------------------------

async def complete_stream(
    model: str,
    messages: list[dict],
    *,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    timeout: int = 60,
    run_id: str = "",
):
    """Streaming chat completion via LiteLLM SSE.

    Yields a sequence of events:
      {"type": "delta", "content": "<chunk>"}     — token/text increment
      {"type": "done",  "content": "<full>",       — terminal: full text + usage
       "usage": {...}, "finish_reason": "stop",
       "cost_usd": ..., "trace_id": "", "run_id": ""}

    No retries: once tokens are flowing to the caller, replaying is unsafe.
    """
    cfg = get_config().litellm

    full_messages: list[dict] = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    body: dict[str, Any] = {
        "model": model,
        "messages": full_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if response_format:
        body["response_format"] = response_format

    accumulated: list[str] = []
    usage: dict | None = None
    finish_reason: str | None = None
    cost: float | None = None

    client = await get_shared_client()
    async with client.stream(
        "POST",
        f"{cfg.base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=httpx.Timeout(timeout, connect=10),
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
                if "cost" in chunk:
                    cost = chunk.get("cost")

            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    accumulated.append(piece)
                    yield {"type": "delta", "content": piece}
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]

    yield {
        "type": "done",
        "content": "".join(accumulated),
        "usage": usage or {},
        "finish_reason": finish_reason or "stop",
        "cost_usd": cost,
        "trace_id": run_id,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# embed()
# ---------------------------------------------------------------------------

async def embed(
    texts: list[str],
    *,
    model: str | None = None,
    timeout: int = 30,
    run_id: str = "",
) -> dict:
    """Batch embedding via LiteLLM proxy.

    Default model returns 768-dim embeddings (nomic-embed-text).
    """
    cfg = get_config().litellm
    model = model or "nomic-embed-text"
    start = time.monotonic()

    try:
        client = await get_shared_client()
        resp = await client.post(
            f"{cfg.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": texts,
            },
            timeout=httpx.Timeout(timeout, connect=10),
        )
        resp.raise_for_status()
        data = resp.json()

        elapsed = time.monotonic() - start
        embeddings = [item["embedding"] for item in data["data"]]
        dimensions = len(embeddings[0]) if embeddings else 0

        return {
            "kind": "embed",
            "provider": model,
            "status": "ok",
            "duration_s": round(elapsed, 2),
            "run_id": run_id,
            "trace_id": run_id,
            "embeddings": embeddings,
            "dimensions": dimensions,
            "content": None,
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": 0,
                "total_tokens": data.get("usage", {}).get("total_tokens", 0),
            },
            "finish_reason": "stop",
            "cost_usd": data.get("usage", {}).get("cost"),
            "error_type": None,
            "error_msg": None,
        }
    except Exception as exc:
        elapsed = time.monotonic() - start
        return {
            "kind": "embed",
            "provider": model,
            "status": "error",
            "duration_s": round(elapsed, 2),
            "run_id": run_id,
            "trace_id": run_id,
            "embeddings": None,
            "dimensions": None,
            "content": None,
            "usage": None,
            "finish_reason": "error",
            "cost_usd": None,
            "error_type": classify_error(exc),
            "error_msg": str(exc),
        }


# ---------------------------------------------------------------------------
# call_llm() — backward-compatible wrapper
# ---------------------------------------------------------------------------

async def call_llm(
    model: str,
    prompt: str,
    *,
    timeout: int = 60,
    run_id: str = "",
) -> dict:
    """Legacy wrapper — converts a prompt string to a complete() call."""
    result = await complete(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        timeout=timeout,
        run_id=run_id,
    )
    # Map to old result shape for backward compat with runner/fanout
    return {
        "kind": "complete",
        "provider": result["provider"],
        "text": result.get("content") or "",
        "duration_s": result["duration_s"],
        "status": result["status"],
        "cost_usd": result.get("cost_usd"),
        "error_type": result.get("error_type"),
        "error_msg": result.get("error_msg"),
        "session_id": None,
        "files_changed": None,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _complete_error(model: str, run_id: str, elapsed: float, exc: Exception | None) -> dict:
    return {
        "kind": "complete",
        "provider": model,
        "status": "error",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": "",
        "parsed": None,
        "usage": None,
        "finish_reason": "error",
        "cost_usd": None,
        "error_type": classify_error(exc) if exc else "Unknown",
        "error_msg": str(exc) if exc else "Max retries exceeded",
    }
