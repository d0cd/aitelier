"""OpenAI ↔ aitelier translation.

aitelier's public inference contract is the OpenAI Chat Completions / Embeddings
shape. Two execution paths:

  - `model = "<alias-or-passthrough>"` → LiteLLM proxy. Pure passthrough.
  - `model = "agent:<backend>[/<inner-llm>]"` → Sandbox Agent. Translated.

The `aitelier.*` namespace inside `extra_body` carries agent-specific options
that OpenAI shape can't express (workspace, mcp_servers, prepare, artifacts,
…). Accepted only on the agent path; 400 otherwise.

This module holds the wire types and translation helpers. Routing decisions
and durable-state side effects live in server.py.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- Request models --------------------------------------------------------


class AitelierAgentOpts(BaseModel):
    """Agent-execution knobs that don't fit OpenAI's request shape. Carried
    in the request body as `aitelier`, accepted only when `model` starts with
    `agent:`.

    `extra="forbid"` matches the published JSON Schema's
    `additionalProperties: false`. Without this, Pydantic silently drops
    unknown fields (e.g. `aitelier.timeout: 999` — a common misplacement
    of the top-level `timeout` body field), and the request continues
    into the runner where the missing/bogus state surfaces as a raw
    Python exception like `KeyError: 'sessionId'`. Failing fast at the
    boundary with a 422 keeps both bugs from co-occurring.
    """
    model_config = ConfigDict(extra="forbid")

    workspace: str | None = None
    mcp_servers: list[dict] | None = Field(default=None, max_length=32)
    tool_allowlist: list[str] | None = Field(default=None, max_length=256)
    max_turns: int | None = None
    reasoning_effort: str | None = None
    """Inner-agent reasoning effort. Mapped to whichever session config option
    the backend advertises in the ACP `thought_level` category (codex
    `reasoning_effort`: low/medium/high/xhigh; claude `effort`:
    low/medium/high/xhigh/max). Validated against the backend's advertised
    values at session start — an unknown level fails fast with the list. Falls
    back to the top-level OpenAI `reasoning_effort` when this is unset."""
    approval_mode: str | None = None
    """Inner-agent approval / sandboxing preset, mapped to the backend's ACP
    `mode` option (codex: read-only/auto/full-access; claude:
    auto/default/acceptEdits/plan/dontAsk/bypassPermissions). Validated against
    advertised values at session start."""
    prepare: dict | None = None
    artifacts: dict | None = None
    trace_tag: str | None = None
    parent_run_id: str | None = None
    """Optional pointer to a parent run for multi-agent workflows. Pure
    pass-through — recorded on the child's run row and queryable via
    `/v1/runs?parent_run_id=X`, but aitelier enforces no semantics. Use
    with `trace_tag` to group a whole workflow's runs."""
    examples: list[dict] | None = Field(default=None, max_length=100)
    # Escape hatch for transports that emit OpenAI `tools` per a global
    # toolset config and can't suppress it per-profile. Default stays
    # hard-reject; opt-in to "I know my tools won't run; do it anyway."
    # Drops both `tools` and `tool_choice` server-side.
    allow_tool_drop: bool = False

    @field_validator("mcp_servers")
    @classmethod
    def _check_mcp_servers(cls, v: list[dict] | None) -> list[dict] | None:
        """Each MCP server needs a `name` and a recognized `transport`. Reject
        at the boundary instead of silently dropping (unknown transport) or
        raising a raw KeyError deep in session setup (missing name)."""
        if v is None:
            return v
        allowed = {"http", "stdio"}
        for i, s in enumerate(v):
            if not isinstance(s, dict):
                raise ValueError(f"mcp_servers[{i}] must be an object")
            if not isinstance(s.get("name"), str) or not s["name"].strip():
                raise ValueError(f"mcp_servers[{i}] requires a non-empty `name`")
            transport = s.get("transport", "http")
            if transport not in allowed:
                raise ValueError(
                    f"mcp_servers[{i}] transport {transport!r} not supported; "
                    f"use one of {sorted(allowed)}"
                )
        return v

    @field_validator("examples")
    @classmethod
    def _check_examples(cls, v: list[dict] | None) -> list[dict] | None:
        """Each few-shot example must carry non-empty `user` and `assistant`
        strings. Reject at the boundary instead of folding a malformed entry
        (e.g. `{input, output}`) into an empty `User:\\nAssistant:` block."""
        if v is None:
            return v
        for i, ex in enumerate(v):
            if not isinstance(ex, dict):
                raise ValueError(f"examples[{i}] must be an object")
            for key in ("user", "assistant"):
                val = ex.get(key)
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(
                        f"examples[{i}] must have a non-empty string `{key}` "
                        f"(got keys {sorted(ex)}); the shape is "
                        '{"user": "...", "assistant": "..."}'
                    )
        return v


class ChatCompletionRequest(BaseModel):
    """Subset of the OpenAI Chat Completions request we accept.

    Fields we ignore on purpose are listed in the agent-path validator —
    silently dropping safety knobs is a bug class we explicitly fight.
    `extra="forbid"` extends that posture to unknown top-level fields
    (`temperture=`, `max_token=`) — better a 422 than a silently
    untemperated request.
    """
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict] = Field(min_length=1, max_length=1000)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    n: int | None = None
    response_format: dict | None = None
    reasoning_effort: str | None = None
    # `tools` is capped at 256 to match the practical limit any provider
    # would honor — Anthropic accepts dozens, OpenAI hundreds. The cap
    # exists to bound the parse cost when a hostile caller maxes out
    # the body-size limit with millions of tiny tool entries.
    tools: list[dict] | None = Field(default=None, max_length=256)
    tool_choice: Any = None
    user: str | None = None
    timeout: int | None = None
    stream_options: dict | None = None
    seed: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None

    # aitelier extension — agent-specific options.
    aitelier: AitelierAgentOpts | None = None


class AsyncRunRequest(ChatCompletionRequest):
    """POST /v1/runs body. Same shape as /v1/chat/completions plus an
    optional webhook_url that fires with the final ChatCompletion (or error)
    when the run completes."""
    webhook_url: str | None = None


class ScheduleRequest(BaseModel):
    """Schedule a recurring or one-shot task. `task` mirrors the chat-
    completions request body and is validated when the schedule fires.

    `name` is charset-restricted because it flows into log lines and into
    the inner agent's `<aitelier_context>` system-prompt block via
    `make_run_id`; permitting arbitrary text would enable stored prompt-
    injection across team users on the same aitelier."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        default="scheduled", min_length=1, max_length=64,
        pattern=r"^[A-Za-z0-9_\-\.]+$",
    )
    task: dict
    interval_seconds: int | None = None
    at_iso: str | None = None
    webhook_url: str | None = None


class EmbeddingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "nomic-embed-text"
    input: str | list[str]
    encoding_format: str | None = None
    dimensions: int | None = None
    user: str | None = None


class ScoreRequest(BaseModel):
    """One score written back against a run by an external grader.

    `name` and `evaluator` are charset-restricted because they flow into
    aggregate queries and log lines; permitting arbitrary text invites
    log-line confusion and accidental SQL-like input in custom
    aggregators. `value` is unconstrained on purpose — different rubrics
    use different ranges (0..1, 1..5, raw token counts, latency
    budgets). Consumers normalize as they need."""
    model_config = ConfigDict(extra="forbid")

    name:      str = Field(min_length=1, max_length=128,
                            pattern=r"^[A-Za-z0-9_\-\.]+$")
    value:     float
    evaluator: str = Field(min_length=1, max_length=128,
                            pattern=r"^[A-Za-z0-9_\-\.:/]+$")
    comment:   str | None = Field(default=None, max_length=4096)
    metadata:  dict | None = None


# --- Model routing ---------------------------------------------------------


def parse_model_route(model: str) -> tuple[str, str | None, str | None]:
    """Decide whether `model` is an LLM call or an agent call.

    Returns `(route, agent_backend, inner_llm)`:
      - `("llm", None, None)` for any model that doesn't start with `agent:`.
        LiteLLM resolves the actual provider.
      - `("agent", backend, inner)` for `agent:<backend>[/<inner>]`. `inner`
        may be `None` when the consumer didn't specify one (the backend's
        default applies).

    Examples:
      `"claude-sonnet-4-6"`                 → ("llm", None, None)
      `"anthropic/claude-opus-4-7"`         → ("llm", None, None)
      `"agent:claude"`                      → ("agent", "claude", None)
      `"agent:claude/claude-sonnet-4-5"`    → ("agent", "claude", "claude-sonnet-4-5")

    Raises `ValueError` for `"agent:"` with an empty backend — callers
    convert it to a 400 at the boundary rather than letting an empty
    backend surface as a confusing Sandbox-Agent-side handshake error.
    """
    if not model.startswith("agent:"):
        return "llm", None, None
    tail = model[len("agent:"):]
    if "/" in tail:
        backend, inner = tail.split("/", 1)
    else:
        backend, inner = tail, None
    if not backend:
        raise ValueError(
            "agent model must name a backend: 'agent:<backend>[/<inner-llm>]' "
            "(got an empty backend)"
        )
    return "agent", backend, inner or None


# --- Result translation ----------------------------------------------------


def _chat_completion_id(run_id: str) -> str:
    """Prefix run_id so OpenAI clients recognize it as a completion id."""
    return f"chatcmpl-{run_id or uuid.uuid4().hex}"


def _map_finish_reason(reason: str | None) -> str | None:
    """Map aitelier finish_reason → OpenAI finish_reason vocabulary.

    OpenAI's set: `stop`, `length`, `tool_calls`, `content_filter`,
    `function_call`. Anything else collapses to `stop`.
    """
    if reason is None:
        return None
    if reason in ("stop", "end_turn", "completed"):
        return "stop"
    if reason in ("length", "max_tokens"):
        return "length"
    if reason in ("tool_calls", "tool_use"):
        return "tool_calls"
    if reason == "content_filter":
        return "content_filter"
    return "stop"


def agent_usage_to_openai(usage: dict | None) -> dict | None:
    """Translate aitelier's internal usage shape to OpenAI's, including the
    cache-hit breakdown if the upstream surfaced it (Anthropic via LiteLLM
    normalizes `cache_read_input_tokens` → `cached_tokens`; OpenAI gives it
    directly).

    Preserves the OpenAI invariant `total_tokens == prompt_tokens +
    completion_tokens`. On the agent path the inner backend often reports a
    much larger `total_tokens` (system prompt + tool schemas + intermediate
    reasoning of the inner agent) than the user-visible I/O. Surfacing that
    as `total_tokens` would lie to every consumer that displays it. Instead,
    we expose the overhead as `aitelier_inner_tokens` so naive OpenAI
    consumers see honest visible usage and curious consumers can sum the
    two for the full subscription cost.
    """
    if not usage:
        return None
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    total_reported = usage.get("total_tokens", 0) or 0
    inner_overhead = total_reported - prompt - completion
    if inner_overhead < 0:
        # Upstream undercounted total — trust the components and recompute.
        inner_overhead = 0
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if inner_overhead > 0:
        out["aitelier_inner_tokens"] = inner_overhead
    cached = usage.get("cached_tokens") or usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    if cached is not None or cache_creation is not None:
        details: dict[str, int] = {}
        if cached is not None:
            details["cached_tokens"] = int(cached)
        if cache_creation is not None:
            details["cache_creation_tokens"] = int(cache_creation)
        out["prompt_tokens_details"] = details
    return out


def summarize_tool_calls(result: dict) -> tuple[list[str], int]:
    """Pull names + count of tool invocations from an agent result.

    Saves the consumer's extra `/v1/runs/{id}/events` roundtrip for the
    common "did the agent use my tools?" question.
    """
    calls = result.get("tool_calls") or []
    names: list[str] = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        name = c.get("tool") or c.get("name")
        if name:
            names.append(str(name))
    return names, len(calls)


def agent_result_to_chat_completion(
    result: dict, *, request_model: str, run_id: str,
) -> dict:
    """Convert an aitelier agent Result dict to an OpenAI ChatCompletion."""
    tool_names, tool_count = summarize_tool_calls(result)
    response: dict[str, Any] = {
        "id": _chat_completion_id(run_id),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": result.get("content") or "",
            },
            "finish_reason": _map_finish_reason(result.get("finish_reason")),
            "logprobs": None,
        }],
        "usage": agent_usage_to_openai(result.get("usage")) or {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        },
        # aitelier extensions — non-standard but harmless to OpenAI clients.
        "aitelier_run_id": result.get("run_id") or run_id,
    }
    # Saves the consumer's `/v1/runs/{id}/events` roundtrip for the common
    # "did the agent use my tools?" question. Empty list when nothing fired.
    response["aitelier_tool_call_count"] = tool_count
    response["aitelier_tool_names"] = tool_names
    return response


def chat_completion_error_envelope(
    result: dict, *, run_id: str | None = None, correlation_id: str | None = None,
    status_code: int | None = None,
) -> dict:
    """Canonical error-envelope shape for chat-completion failures from
    either the LLM or agent path. Both paths produce a result dict with
    `status="error"`, `error_type`, `error_msg`, plus optional
    `finish_reason` and `_aitelier_http_status`; this helper renders them
    into one consistent wire shape so consumers don't need to branch on
    "which subsystem failed".

    Shape:
        {
          "error": {"type": "...", "message": "...", "code": "..."},
          "aitelier_run_id": "...",
          "correlation_id":  "...",
          "aitelier_status_code": <int>,
        }
    """
    return {
        "error": {
            "type":    result.get("error_type") or "ProviderError",
            "message": result.get("error_msg") or "request failed",
            "code":    result.get("finish_reason") or "error",
        },
        "aitelier_run_id": result.get("run_id") or run_id,
        "correlation_id":  correlation_id,
        "aitelier_status_code": (
            status_code
            or result.get("_aitelier_http_status")
            or 502
        ),
    }


def agent_error_to_chat_completion_error(
    result: dict, *, status_code: int = 500,
) -> tuple[int, dict]:
    """Legacy two-tuple shim around `chat_completion_error_envelope`.

    Kept for the prepare/sidecar paths in `server.py` that destructure
    `(status, body)` for explicit HTTPException raise sites. New code
    should use the envelope helper directly.
    """
    body = chat_completion_error_envelope(result, status_code=status_code)
    return status_code, body


def chat_completion_chunk(
    *, request_model: str, run_id: str, delta: dict,
    finish_reason: str | None = None, usage: dict | None = None,
) -> dict:
    """Build a single OpenAI streaming chunk."""
    chunk: dict[str, Any] = {
        "id": _chat_completion_id(run_id),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": request_model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


# --- aitelier extras: response signals layered onto OpenAI shape -----------


def wants_json(body: dict) -> bool:
    """True when the request asked for `response_format: json_object|json_schema`."""
    rf = body.get("response_format")
    return isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema")


def try_parse_json(content: str) -> Any:
    """Best-effort JSON parse with fence + prose stripping.

    The consumer asked for JSON. The model may have wrapped it in
    ```json fences``` or prefixed prose ("Here's the JSON: { ... }").
    Returns the parsed value, or None if nothing parses.
    """
    if not content:
        return None
    candidates = [content.strip()]
    stripped = candidates[0]
    if stripped.startswith("```"):
        inner = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if inner.endswith("```"):
            inner = inner[:-3].rstrip()
        candidates.append(inner.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(stripped[start:end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def _normalize_usage_cache_details(usage: dict | None) -> None:
    """Pull Anthropic-shape cache fields into OpenAI's nested
    `prompt_tokens_details` so consumers reading the OpenAI convention find
    them. Mutates `usage` in place; no-op when nothing to normalize.

    Anthropic via LiteLLM emits `cache_read_input_tokens` /
    `cache_creation_input_tokens` at the top level. OpenAI uses
    `prompt_tokens_details.cached_tokens`.
    """
    if not isinstance(usage, dict):
        return
    cached = usage.get("cache_read_input_tokens") or usage.get("cached_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    if cached is None and cache_creation is None:
        return
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    if cached is not None and "cached_tokens" not in details:
        details["cached_tokens"] = int(cached)
    if cache_creation is not None and "cache_creation_tokens" not in details:
        details["cache_creation_tokens"] = int(cache_creation)
    usage["prompt_tokens_details"] = details


def normalize_response_extras(body: dict, response: dict) -> None:
    """Stamp aitelier-specific side-channel fields on an OpenAI ChatCompletion.

    Mutates `response` in place. Three signals (each on `choices[i]`):

    - `message.reasoning_content` — left as LiteLLM emits it (Anthropic
      extended-thinking, qwen3 reasoning, Bedrock thinking all land here).
      No rename; that's the convention.
    - `message.aitelier_parsed` — best-effort JSON parse of `content` when
      the consumer asked for `response_format: json_*`. Handles fenced and
      prose-wrapped JSON so consumers don't reinvent fence-stripping.
    - `aitelier_exit: "empty"` on the choice when `completion_tokens > 0`
      but `content == ""` and no reasoning and no tool_calls — diagnoses
      "reasoning model burned its budget on hidden thinking" which
      OpenAI's `finish_reason` vocabulary can't express.

    Also normalizes usage cache fields into OpenAI's nested shape so the
    `prompt_tokens > completion_tokens + prompt_tokens` mismatch consumers
    saw with cache hits is now traceable.
    """
    consumer_wants_json = wants_json(body)
    usage = response.get("usage") or {}
    _normalize_usage_cache_details(usage)
    completion_tokens = usage.get("completion_tokens", 0) or 0
    for choice in response.get("choices") or []:
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if consumer_wants_json and content:
            parsed = try_parse_json(content)
            if parsed is not None:
                msg["aitelier_parsed"] = parsed
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        tool_calls = msg.get("tool_calls")
        if (not content and completion_tokens > 0
                and not reasoning and not tool_calls):
            choice["aitelier_exit"] = "empty"


def stream_final_extras(
    body: dict, *, accumulated_content: str, reasoning_seen: str,
    tool_call_seen: bool, completion_tokens: int,
) -> dict[str, Any]:
    """Compute the aitelier-extras to emit on the final streaming chunk.

    Mirror of `normalize_response_extras` for the streaming path. Returns
    `{}` when there's nothing to say (caller skips the extra chunk).
    """
    extras: dict[str, Any] = {}
    if wants_json(body) and accumulated_content:
        parsed = try_parse_json(accumulated_content)
        if parsed is not None:
            extras["aitelier_parsed"] = parsed
    if (not accumulated_content and completion_tokens > 0
            and not reasoning_seen and not tool_call_seen):
        extras["aitelier_exit"] = "empty"
    return extras
