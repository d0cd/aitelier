"""Request preparation + validation for /v1/chat/completions.

Pure functions: OpenAI messages -> (system_prompt, prompt) translation, few-shot
+ response-format folding into the system prompt, and the agent-path field
rejections / aitelier.* option validation. Extracted from server.py; the
orchestration functions there and endpoints/inference.py import from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import aitelier.idempotency as _idem
from aitelier.config import get_config
from aitelier.errors import classify_error, scrub_error_text
from aitelier.openai_compat import (
    AitelierAgentOpts,
    ChatCompletionRequest,
    agent_error_to_chat_completion_error,
    agent_result_to_chat_completion,
    agent_usage_to_openai,
    chat_completion_chunk,
    chat_completion_error_envelope,
    normalize_response_extras,
    stream_final_extras,
    summarize_tool_calls,
)
from aitelier.providers.llm import (
    LLMError,
    UnsupportedResponseFormat,
    chat_completion,
    chat_completion_stream,
)
from aitelier.runs import hash_system_prompt, record_run, start_run
from aitelier.runtime import (
    _SSE_KEEPALIVE_SECONDS,
    _active_runs,
    _cancelled_result,
    _pending_finalize_tasks,
    _sse_event,
    _sse_response,
    _track_inflight_run,
)
from aitelier.sandbox_proxy import fetch_artifacts as _fetch_artifacts
from aitelier.sandbox_proxy import prepare_failed_result as _prepare_failed_result
from aitelier.sandbox_proxy import run_prepare as _run_prepare
from aitelier.sandbox_proxy import stop_sidecars as _stop_sidecars
from aitelier.storage import RunSpec, get_store

logger = logging.getLogger("aitelier")

_STREAM_IDEMPOTENCY_MAX_CHUNKS = _idem.STREAM_IDEMPOTENCY_MAX_CHUNKS
_IdempotencyContext = _idem.IdempotencyContext
_check_idempotency = _idem.check_idempotency
_record_idempotency = _idem.record_idempotency
_release_idempotency_ctx = _idem.release_idempotency_ctx

# Sentinel pushed onto the agent-stream queue to signal end-of-stream.
_STREAM_QUEUE_SENTINEL: dict = {"_eof": True}


def _fold_examples(system_prompt: str | None, examples: list[dict] | None) -> str | None:
    """Server-side concat of few-shot examples into the system prompt.

    Each example is a dict with `user` and `assistant` keys. Returns None
    only if both inputs were None/empty.
    """
    if not examples:
        return system_prompt
    blocks: list[str] = []
    for ex in examples:
        u = ex.get("user", "") if isinstance(ex, dict) else ""
        a = ex.get("assistant", "") if isinstance(ex, dict) else ""
        blocks.append(f"User: {u}\nAssistant: {a}")
    section = "## Examples\n\n" + "\n\n".join(blocks)
    return f"{system_prompt}\n\n{section}" if system_prompt else section


_RESPONSE_FORMAT_SCHEMA_MAX_BYTES = 32 * 1024


def _fold_response_format(
    system_prompt: str | None, response_format: dict | None,
) -> str | None:
    """Best-effort schema enforcement for the agent path.

    The ACP `session/prompt.responseFormat` parameter is passed through
    to the backend, but coding-agent backends (claude-code, codex) often
    ignore it. To raise the floor, the JSON Schema is also rendered into
    the system prompt as text — agents that read instructions in plain
    English will conform without needing native schema support.

    Accepts both the OpenAI-spec nested shape
    `{type: "json_schema", json_schema: {name, schema, strict}}` and the
    flat shape `{type: "json_schema", schema: {...}}`. Schemas larger
    than 32 KiB are dropped from the prompt fold (still passed via ACP)
    to bound the token cost.

    Best-effort, not guaranteed. Consumers needing hard enforcement
    should run the response through their own validator and surface a
    typed retry.
    """
    fmt_type = (response_format or {}).get("type")
    if fmt_type == "json_object":
        # No schema to render; inject the same "return JSON only" directive the
        # LLM path uses for providers without native json_object support, so a
        # json_object request isn't silently dropped on the agent path.
        section = (
            "## Required output format\n\n"
            "Your final assistant message MUST be a single JSON object. Emit "
            "only the JSON object — no prose, code fences, or commentary."
        )
        return f"{system_prompt}\n\n{section}" if system_prompt else section
    if fmt_type != "json_schema":
        return system_prompt
    nested = response_format.get("json_schema")
    if isinstance(nested, dict) and isinstance(nested.get("schema"), dict):
        schema = nested["schema"]
    else:
        schema = response_format.get("schema")
    if not isinstance(schema, dict) or not schema:
        return system_prompt
    rendered = json.dumps(schema, indent=2)
    if len(rendered) > _RESPONSE_FORMAT_SCHEMA_MAX_BYTES:
        logger.warning(
            "response_format schema too large to fold into system prompt "
            "(%d bytes > %d cap); passing only via ACP responseFormat",
            len(rendered), _RESPONSE_FORMAT_SCHEMA_MAX_BYTES,
        )
        return system_prompt
    section = (
        "## Required output format\n\n"
        "Your final assistant message MUST be a JSON object conforming "
        "exactly to this JSON Schema. Emit only the JSON object, with "
        "no prose, code fences, or commentary.\n\n"
        f"{rendered}"
    )
    return f"{system_prompt}\n\n{section}" if system_prompt else section


def _translate_messages(messages: list[dict]) -> tuple[str | None, str]:
    """Map OpenAI `messages` → aitelier's (system_prompt, initial_message).

    Strategy:
      - All `system` messages concatenated → system_prompt.
      - Single trailing user message → initial_message.
      - Multi-turn (user/assistant alternation) → history folded into an
        `<conversation_history>` envelope, last user message wrapped in
        `<current_task>`. The inner agent treats it as one prompt.

    The last non-system message MUST be `role=user` — 400 otherwise.
    """
    system_msgs: list[str] = []
    non_system: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_msgs.append(str(m.get("content") or ""))
        else:
            non_system.append(m)
    system_prompt = "\n\n".join(s for s in system_msgs if s) or None

    if not non_system:
        return system_prompt, ""

    last = non_system[-1]
    if last.get("role") != "user":
        raise HTTPException(
            status_code=400,
            detail="last message must have role='user' on the agent path",
        )

    if len(non_system) == 1:
        return system_prompt, str(last.get("content") or "")

    parts = ["<conversation_history>"]
    for m in non_system[:-1]:
        role = m.get("role") or "user"
        content = str(m.get("content") or "").replace("\n", "\n    ")
        parts.append(f'  <message role="{role}">{content}</message>')
    parts.append("</conversation_history>")
    parts.append("")
    parts.append("<current_task>")
    parts.append(str(last.get("content") or ""))
    parts.append("</current_task>")
    return system_prompt, "\n".join(parts)


def _reject_agent_incompatible_fields(
    req: ChatCompletionRequest, agent_backend: str,
) -> None:
    """Hard-reject OpenAI fields that have no honest mapping to the agent
    path. Silent drops are the bug class we explicitly fight.

    Consumers whose transport can't suppress `tools` / `tool_choice`
    per-profile can opt into a silent drop via
    `aitelier.allow_tool_drop = true`. The drop is documented and audited;
    that's the difference from a silent bug.
    """
    # `max_turns` and `tool_allowlist` only have an ACP channel on claude (they
    # ride session/new `_meta` into the Claude Agent SDK). Other backends — codex
    # advertises no such options — can't honor them, so reject rather than drop
    # silently. (A non-claude system prompt IS delivered: it's folded into the
    # prompt text by the SA layer, so it is not rejected here.)
    if agent_backend != "claude" and req.aitelier:
        unsupported = []
        if req.aitelier.max_turns is not None:
            unsupported.append("max_turns")
        if req.aitelier.tool_allowlist:
            unsupported.append("tool_allowlist")
        if unsupported:
            fields = ", ".join(f"`{u}`" for u in unsupported)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"agent backend '{agent_backend}' can't honor {fields} — "
                    "these are claude-only (Claude Agent SDK options). For tool "
                    f"access on '{agent_backend}', use `aitelier.approval_mode` "
                    "(e.g. read-only/auto/full-access on codex); the run is bounded "
                    "by the top-level `timeout`."
                ),
            )
    # The streaming agent handler doesn't run the prepare → run → artifacts
    # workflow (only the sync path does), so accepting prepare/artifacts with
    # stream:true would silently skip them. Reject rather than drop silently.
    if req.stream and req.aitelier:
        unsupported = [n for n in ("prepare", "artifacts")
                       if getattr(req.aitelier, n, None)]
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{', '.join('`' + u + '`' for u in unsupported)} "
                    "is not supported with `stream: true` — the prepare/artifacts "
                    "workflow only runs on the non-streaming path. Use a "
                    "non-streaming request (`stream: false`) or submit async via "
                    "`POST /v1/runs`."
                ),
            )
    allow_tool_drop = bool(req.aitelier and req.aitelier.allow_tool_drop)
    if req.tools and not allow_tool_drop:
        raise HTTPException(
            status_code=400,
            detail="`tools` is not supported on the agent path — the inner "
                   "agent runs its own tools. Use `aitelier.tool_allowlist` "
                   "to constrain them, or set "
                   "`aitelier.allow_tool_drop = true` to opt into silent drop.",
        )
    if req.tool_choice is not None and not allow_tool_drop:
        raise HTTPException(
            status_code=400,
            detail="`tool_choice` is not supported on the agent path. Set "
                   "`aitelier.allow_tool_drop = true` to opt into silent drop.",
        )
    if req.n is not None and req.n > 1:
        raise HTTPException(
            status_code=400, detail="`n` > 1 is not supported on the agent path.",
        )
    # The inner agent owns its own sampling / decoding / length budget — these
    # OpenAI fields have no honest mapping on the agent path. Reject rather than
    # accept-and-silently-ignore (the documented anti-pattern). Use the
    # top-level `timeout` to bound a run.
    _sampling = [
        name for name, val in (
            ("temperature", req.temperature),
            ("top_p", req.top_p),
            ("max_tokens", req.max_tokens),
            ("max_completion_tokens", req.max_completion_tokens),
            ("seed", req.seed),
            ("stop", req.stop),
            ("frequency_penalty", req.frequency_penalty),
            ("presence_penalty", req.presence_penalty),
            ("logprobs", req.logprobs),
            ("top_logprobs", req.top_logprobs),
        ) if val is not None
    ]
    if _sampling:
        fields = ", ".join(f"`{f}`" for f in _sampling)
        raise HTTPException(
            status_code=400,
            detail=(
                f"{fields} {'is' if len(_sampling) == 1 else 'are'} not supported "
                "on the agent path — the inner agent controls its own sampling, "
                "decoding, and length budget. Bound a run with the top-level "
                "`timeout`."
            ),
        )
    if req.tools and allow_tool_drop:
        logger.info(
            "aitelier.allow_tool_drop active — dropping %d `tools` entries "
            "and tool_choice on agent path",
            len(req.tools),
        )


async def _validate_aitelier_opts(req: ChatCompletionRequest, *, agent_path: bool) -> None:
    """Refuse aitelier.* on the LLM path. The namespace is agent-specific.

    On the agent path, also runs the workspace + artifacts + prepare.files
    path checks and SSRF-checks every HTTP MCP server URL. List-size caps
    on `mcp_servers` and `tool_allowlist` live on the Pydantic model as
    `Field(max_length=…)` — those reject at parse time. The checks here
    bound what aitelier hands to SA. They do not constrain the agent's
    own tool calls (claude-code's `Read`, `Bash`, etc., which traverse
    SA's filesystem layer directly) — that fix lives in SA upstream.
    """
    if req.aitelier is not None and not agent_path:
        raise HTTPException(
            status_code=400,
            detail="`aitelier.*` options are only valid when model starts "
                   "with `agent:`.",
        )
    if not agent_path or req.aitelier is None:
        return

    from aitelier.security import is_public_url, validate_workspace_path
    cfg = get_config().service
    roots = cfg.allowed_workspace_roots or None

    opts = req.aitelier
    validate_workspace_path(opts.workspace, roots=roots, label="aitelier.workspace")

    if opts.prepare:
        for f in opts.prepare.get("files") or []:
            validate_workspace_path(
                f.get("path"), roots=roots,
                label="aitelier.prepare.files[].path",
            )
    if opts.artifacts:
        for p in opts.artifacts.get("fetch") or []:
            validate_workspace_path(
                p, roots=roots, label="aitelier.artifacts.fetch[]",
            )

    # SSRF: every consumer-supplied URL that aitelier causes to be
    # connected to is gated against private / loopback / metadata ranges.
    # stdio servers have no URL; skip them. The `allow_loopback_webhooks`
    # toggle relaxes both webhook and MCP-URL checks for local dev.
    if opts.mcp_servers and not cfg.allow_loopback_webhooks:
        for s in opts.mcp_servers:
            if (s.get("transport") or "http") != "http":
                continue
            url = s.get("url") or ""
            if not url:
                continue
            if not await is_public_url(url):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "aitelier.mcp_servers[].url must resolve to a "
                        "public, non-loopback host. Set "
                        "`service.allow_loopback_webhooks = true` for "
                        "local-dev MCP servers."
                    ),
                )


def _stream_chunks_for_delta(
    event: dict, *, model: str, run_id: str,
    stamp, first: bool,
) -> tuple[list[dict], bool]:
    """Build the openai-chunk(s) for an ACP delta event.

    The first delta also seeds the assistant role on its own chunk,
    matching OpenAI's streaming convention. Returns the chunks to write
    + the updated `first` flag.
    """
    chunks: list[dict] = []
    if first:
        chunks.append(stamp(chat_completion_chunk(
            request_model=model, run_id=run_id,
            delta={"role": "assistant"},
        )))
    chunks.append(stamp(chat_completion_chunk(
        request_model=model, run_id=run_id,
        delta={"content": event.get("content") or ""},
    )))
    return chunks, False


def _stream_chunk_for_done(
    event: dict, *, model: str, run_id: str, stamp, include_usage: bool = True,
) -> tuple[dict, dict]:
    """Build the terminal chunk for a `done` event + the `final` dict
    that drives finalize_run. Routes usage through `agent_usage_to_openai`
    so the OpenAI invariant `total == prompt + completion` holds on the
    streaming wire and inner-agent overhead lands in `aitelier_inner_tokens`.
    Mirrors the non-streaming response shape's `aitelier_tool_call_count`
    + `aitelier_tool_names` so consumers don't need a separate code path
    for "did the agent use my tools?" on stream vs non-stream.
    """
    final = {k: v for k, v in event.items() if k != "type"}
    chunk_usage = agent_usage_to_openai(final.get("usage")) or {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
    tool_names, tool_count = summarize_tool_calls(final)
    # Honor OpenAI's `stream_options.include_usage: false` — omit the usage
    # block from the terminal chunk (parity with the LLM path).
    c = stamp(chat_completion_chunk(
        request_model=model, run_id=run_id,
        delta={}, finish_reason="stop",
        usage=chunk_usage if include_usage else None,
    ))
    c["aitelier_tool_call_count"] = tool_count
    c["aitelier_tool_names"] = tool_names
    return c, final


def _stream_error_payload(
    event: dict, *, run_id: str, cid: str, agent_backend: str,
) -> tuple[dict, dict]:
    """Build the SSE error frame for an `error` event + the `final`
    dict for storage. Errors are NOT recorded for idempotency replay —
    a retrying consumer should get a fresh attempt at success."""
    err_type = event.get("error_type") or "ProviderError"
    final = {
        "kind": "agent", "provider": agent_backend,
        "status": "error",
        "error_type": err_type,
        "error_msg": event.get("error_msg") or "stream error",
        "finish_reason": (
            "cancelled" if err_type == "Cancelled" else "error"
        ),
    }
    frame = {
        "error": {"type": err_type, "message": event.get("error_msg")},
        "aitelier_run_id": run_id,
        "correlation_id": cid,
    }
    return frame, final


def _terminal_state_from_final(final: dict) -> str:
    """Map a captured `final` dict to a run terminal state. Shared by the agent
    and LLM streaming finalizers so the state taxonomy can't drift between the
    two paths."""
    if final.get("error_type") == "Cancelled":
        return "cancelled"
    if final.get("status") == "error":
        return "failed"
    return "completed"


def _stream_terminal_state(final: dict | None, *, agent_backend: str) -> tuple[dict, str]:
    """Decide the run's terminal state from the captured `final` dict.

    None → consumer disconnected mid-stream before observing a terminal
    event; fabricate a `cancelled` final so the run doesn't stay
    state=running forever (which would contaminate dashboards +
    /v1/metrics in_flight)."""
    if final is None:
        final = {
            "kind": "agent", "provider": agent_backend,
            "status": "cancelled",
            "error_type": "Cancelled",
            "error_msg": "consumer disconnected mid-stream",
            "finish_reason": "cancelled",
        }
    return final, _terminal_state_from_final(final)


async def _agent_chat_completion(
    req: ChatCompletionRequest, request: Request, *,
    agent_backend: str, inner_llm: str | None, run_id: str,
    webhook_url: str | None = None,
) -> dict:
    """Sync agent path: prepare → execute → artifacts → ChatCompletion.

    Always returns a plain dict. On error, the dict carries an OpenAI-shape
    `error` envelope plus an `aitelier_status_code` hint so the outer caller
    (HTTP endpoint or async webhook deliverer) can render it consistently —
    either as a 500-class JSONResponse or as a JSON webhook payload.

    `webhook_url` (when set, async path) is persisted in run metadata so the
    orphan-sweep on startup can deliver a terminal webhook if this process
    dies mid-run.
    """

    cid = request.state.correlation_id
    _reject_agent_incompatible_fields(req, agent_backend)
    opts = req.aitelier or AitelierAgentOpts()

    system_prompt, initial_message = _translate_messages(req.messages)
    system_prompt = _fold_examples(system_prompt, opts.examples)
    system_prompt = _fold_response_format(system_prompt, req.response_format)

    prep_result = await _run_prepare(opts.prepare)
    if prep_result.get("error"):
        await _stop_sidecars(prep_result.get("sidecars") or [])
        failed = _prepare_failed_result(run_id, prep_result, cid)
        status, body = agent_error_to_chat_completion_error(failed)
        body["aitelier_status_code"] = status
        body["correlation_id"] = cid
        return body

    run_metadata: dict[str, Any] = {"correlation_id": cid}
    if webhook_url:
        run_metadata["webhook_url"] = webhook_url

    # Rendered messages for the agent path: SA receives system_prompt +
    # the single initial_message (the message list collapses to "last
    # user turn"). Capturing this here so consumers reading
    # `/v1/runs/{id}.rendered_messages` see what the agent actually saw,
    # which can differ from `request_body.messages` (multi-turn history
    # is folded into the system prompt before dispatch).
    rendered_messages = [
        {"role": "system", "content": system_prompt or ""},
        {"role": "user", "content": initial_message or ""},
    ]
    spec = RunSpec(
        run_id=run_id, kind="agent",
        agent_id=agent_backend, model=inner_llm,
        trace_tag=opts.trace_tag, correlation_id=cid,
        parent_run_id=opts.parent_run_id,
        workspace=opts.workspace,
        environment={
            "mcp_servers": opts.mcp_servers or [],
            "tool_allowlist": opts.tool_allowlist or [],
        },
        system_prompt_hash=hash_system_prompt(system_prompt),
        metadata=run_metadata,
        request_body=req.model_dump(exclude_none=True),
        rendered_messages=rendered_messages,
    )

    async def _do() -> dict:
        from aitelier.providers.sandbox_agent import call_via_sandbox
        return await call_via_sandbox(
            agent_backend, initial_message,
            workspace=opts.workspace,
            system_prompt=system_prompt,
            mcp_servers=opts.mcp_servers,
            tool_allowlist=opts.tool_allowlist,
            response_format=req.response_format,
            max_turns=opts.max_turns,
            agent_model=inner_llm,
            reasoning_effort=opts.reasoning_effort or req.reasoning_effort,
            approval_mode=opts.approval_mode,
            timeout=req.timeout or 600,
            run_id=run_id,
        )

    run_task = asyncio.create_task(record_run(spec, _do()))
    _active_runs[run_id] = run_task
    try:
        result = await run_task
    except asyncio.CancelledError:
        if not run_task.cancelled():
            run_task.cancel()
        result = _cancelled_result(run_id, "agent")
    except Exception as exc:
        result = {
            "kind": "agent", "provider": agent_backend, "status": "error",
            "run_id": run_id, "trace_id": run_id,
            "content": None,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "finish_reason": "error",
            "error_type": classify_error(exc), "error_msg": scrub_error_text(str(exc)),
        }
    finally:
        _active_runs.pop(run_id, None)
        await _stop_sidecars(prep_result.get("sidecars") or [])

    # OTel: emit the gen_ai.chat span for this agent run. We pass the
    # caller's request_body (captured into spec earlier) and the result
    # envelope so input/output tokens, finish_reason, and the rendered
    # model land on the span. No-op when `[otel] enabled = false`.
    from aitelier.otel import record_run_trace
    await record_run_trace(
        run_id=run_id,
        operation="chat",
        request_body=spec.request_body,
        result=result if result.get("status") != "error" else None,
        error_type=result.get("error_type"),
        error_msg=result.get("error_msg"),
    )

    if result.get("status") == "error":
        result.setdefault(
            "_aitelier_http_status", _http_status_for_agent_error(result)
        )
        return chat_completion_error_envelope(
            result, run_id=run_id, correlation_id=cid,
        )

    response = agent_result_to_chat_completion(
        result, request_model=req.model, run_id=run_id,
    )
    if opts.artifacts:
        response["aitelier_artifacts"] = await _fetch_artifacts(opts.artifacts)
    response["correlation_id"] = cid
    return response


def _render_chat_completion(payload: dict) -> dict | JSONResponse:
    """Translate the dict returned by `_agent_chat_completion` (or LLM-path
    helpers) into an HTTP response. Error dicts carry `aitelier_status_code`;
    everything else is a success body."""
    status = payload.pop("aitelier_status_code", None)
    if status is not None:
        return JSONResponse(status_code=status, content=payload)
    return payload


async def _producer_for_acp_stream(
    queue: asyncio.Queue, *,
    agent_backend: str, initial_message: str, system_prompt: str | None,
    opts: AitelierAgentOpts, req: ChatCompletionRequest,
    inner_llm: str | None, run_id: str,
) -> None:
    """Drive `call_via_sandbox_stream` into a queue. Wraps cancellation
    and unexpected exceptions as `error` events so the consumer side
    always sees terminal traffic before the sentinel. Always pushes the
    sentinel last so the consumer drains cleanly even on failure."""
    try:
        from aitelier.providers.sandbox_agent import call_via_sandbox_stream
        async for event in call_via_sandbox_stream(
            agent_backend, initial_message,
            workspace=opts.workspace,
            system_prompt=system_prompt,
            mcp_servers=opts.mcp_servers,
            tool_allowlist=opts.tool_allowlist,
            response_format=req.response_format,
            max_turns=opts.max_turns,
            agent_model=inner_llm,
            reasoning_effort=opts.reasoning_effort or req.reasoning_effort,
            approval_mode=opts.approval_mode,
            timeout=req.timeout or 600,
            run_id=run_id,
        ):
            await queue.put(event)
    except asyncio.CancelledError:
        await queue.put({"type": "error", "error_type": "Cancelled",
                          "error_msg": "run cancelled"})
        raise
    except Exception as exc:
        await queue.put({"type": "error",
                          "error_type": classify_error(exc),
                          "error_msg": scrub_error_text(str(exc))})
    finally:
        await queue.put(_STREAM_QUEUE_SENTINEL)


async def _agent_chat_completion_stream(
    req: ChatCompletionRequest, request: Request, *,
    agent_backend: str, inner_llm: str | None, run_id: str,
    idem: _IdempotencyContext | None = None,
):
    """Streaming agent path. Maps ACP `session/update` deltas → OpenAI
    chunks. Tool-call / tool-result events from the inner agent are *not*
    surfaced as OpenAI tool_calls (those imply the consumer should respond);
    consumers can fetch the full event trace via `/v1/runs/{id}/events`.

    When `idem` is provided and the stream completes successfully, the
    accumulated chunks are persisted under the Idempotency-Key so a retry
    with the same key+body replays the exact same SSE stream instead of
    re-running the inner agent (re-executing side effects, double-billing
    the subscription). Failed or cancelled streams are NOT cached — the
    consumer's retry should get a fresh attempt at success.
    """
    cid = request.state.correlation_id
    _reject_agent_incompatible_fields(req, agent_backend)
    opts = req.aitelier or AitelierAgentOpts()

    system_prompt, initial_message = _translate_messages(req.messages)
    system_prompt = _fold_examples(system_prompt, opts.examples)
    system_prompt = _fold_response_format(system_prompt, req.response_format)

    rendered_messages = [
        {"role": "system", "content": system_prompt or ""},
        {"role": "user", "content": initial_message or ""},
    ]
    await start_run(RunSpec(
        run_id=run_id, kind="agent",
        agent_id=agent_backend, model=inner_llm,
        trace_tag=opts.trace_tag, correlation_id=cid,
        parent_run_id=opts.parent_run_id,
        workspace=opts.workspace,
        environment={
            "mcp_servers": opts.mcp_servers or [],
            "tool_allowlist": opts.tool_allowlist or [],
        },
        system_prompt_hash=hash_system_prompt(system_prompt),
        metadata={"correlation_id": cid},
        request_body=req.model_dump(exclude_none=True),
        rendered_messages=rendered_messages,
    ))

    def _stamp(chunk: dict) -> dict:
        chunk["aitelier_run_id"] = run_id
        chunk["correlation_id"] = cid
        return chunk

    # Producer task pulls ACP events into a queue; the SSE generator drains
    # the queue and translates to OpenAI chunks. Registering the producer
    # in `_active_runs` makes the stream cancellable via POST /v1/runs/{id}/cancel.
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    recorded_chunks: list[dict] = []
    producer_task = asyncio.create_task(_producer_for_acp_stream(
        queue,
        agent_backend=agent_backend, initial_message=initial_message,
        system_prompt=system_prompt, opts=opts, req=req,
        inner_llm=inner_llm, run_id=run_id,
    ))
    _active_runs[run_id] = producer_task

    async def event_generator():
        final: dict | None = None
        first = True
        # Track time-since-last-wire-emission rather than relying on
        # `queue.get()` timing out. Tool-call / tool-result events arrive
        # frequently during agent planning but are dropped from the wire;
        # without this counter, queue.get() never times out and the
        # consumer sees no traffic until the next `delta`.
        last_yield_at = time.monotonic()
        try:
            while True:
                remaining = _SSE_KEEPALIVE_SECONDS - (
                    time.monotonic() - last_yield_at
                )
                if remaining <= 0:
                    yield ": keepalive\n\n"
                    last_yield_at = time.monotonic()
                    continue
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    last_yield_at = time.monotonic()
                    continue
                if event is _STREAM_QUEUE_SENTINEL:
                    break
                etype = event.get("type")
                if etype == "delta":
                    chunks, first = _stream_chunks_for_delta(
                        event, model=req.model, run_id=run_id,
                        stamp=_stamp, first=first,
                    )
                    for c in chunks:
                        recorded_chunks.append(c)
                        yield _sse_event("", c)
                    last_yield_at = time.monotonic()
                elif etype == "done":
                    c, final = _stream_chunk_for_done(
                        event, model=req.model, run_id=run_id, stamp=_stamp,
                        include_usage=(req.stream_options or {}).get(
                            "include_usage", True),
                    )
                    recorded_chunks.append(c)
                    yield _sse_event("", c)
                    last_yield_at = time.monotonic()
                elif etype == "error":
                    frame, final = _stream_error_payload(
                        event, run_id=run_id, cid=cid,
                        agent_backend=agent_backend,
                    )
                    # Error frames are not recorded — see _stream_error_payload.
                    yield _sse_event("", frame)
                    last_yield_at = time.monotonic()
                # tool_call / tool_result / thought intentionally dropped from
                # the SSE wire (the consumer asked for a completion, not the
                # agent's reasoning/tool trace) — all recoverable via
                # GET /v1/runs/{id}/events. Unlike the LLM path, agent reasoning
                # is not surfaced as delta.reasoning_content by design.
            yield "data: [DONE]\n\n"
        finally:
            _active_runs.pop(run_id, None)
            if not producer_task.done():
                producer_task.cancel()
            final, state = _stream_terminal_state(final, agent_backend=agent_backend)
            # The current task may be mid-cancellation (uvicorn cancels the
            # response task when the client disconnects), which would
            # interrupt any `await` here and skip the storage write. Detach
            # to a background task so the finalize survives the cancellation.
            should_cache = (
                idem is not None
                and state == "completed"
                and final.get("status") != "error"
                and len(recorded_chunks) <= _STREAM_IDEMPOTENCY_MAX_CHUNKS
            )
            # Always pass `idem` (when present) so _finalize_stream_run
            # releases the per-key lock regardless of caching. The
            # `should_cache` decision controls whether we *record* the
            # response, not whether we release the lock.
            _task = asyncio.create_task(_finalize_stream_run(
                run_id=run_id, final=final, state=state,
                idem=idem,
                recorded_chunks=recorded_chunks if should_cache else None,
                otel_request_body=req.model_dump(exclude_none=True),
                otel_model=req.model,
            ))
            _pending_finalize_tasks.add(_task)
            _task.add_done_callback(_pending_finalize_tasks.discard)

    return _sse_response(event_generator())


async def _finalize_stream_run(
    *, run_id: str, final: dict, state: str,
    idem: _IdempotencyContext | None, recorded_chunks: list[dict] | None,
    otel_request_body: dict | None = None, otel_model: str | None = None,
) -> None:
    """Run the storage write + optional idempotency cache for a streaming
    agent run in its own task. The caller (event_generator's finally) may
    be in the middle of cancellation propagation from a client disconnect;
    awaiting storage from that context raises CancelledError before the
    write lands. Running here, in a fresh task spawned via
    asyncio.create_task, decouples cleanup from request lifecycle."""
    try:
        store = await get_store()
        try:
            await store.finalize_run(run_id, final, state=state)
        except (KeyError, ValueError):
            # Race: cancel endpoint or another path already finalized.
            pass
        # Durability writes (finalize + idem cache) MUST land before any
        # observability emission. record_inference_span has its own
        # best-effort guard, but ordering still matters: if anything in
        # the finalize path raises into the outer except, the lock is
        # released without a cache row — and a retry would re-execute
        # the agent. Cache first, observe second.
        if idem is not None and recorded_chunks is not None:
            await _record_idempotency(idem, {
                "_aitelier_stream": True,
                "chunks": recorded_chunks,
                "run_id": run_id,
            })
        elif idem is not None:
            # Stream didn't complete cleanly enough to cache (truncated,
            # too many chunks, etc.) — release the idempotency lock so a
            # retry under the same key isn't blocked. We don't write a
            # cache row in this case, so the retry will re-run.
            _release_idempotency_ctx(idem)
        # OTel: emit gen_ai.chat span for this streaming agent run.
        # Synthesize an OpenAI-shape result so `gen_ai_response_attrs`
        # finds usage in the OpenAI slots. No-op when OTel is disabled;
        # internally guarded against SDK-side failure.
        if otel_request_body is not None:
            from aitelier.otel import record_run_trace
            await record_run_trace(
                run_id=run_id,
                operation="chat",
                request_body=otel_request_body,
                result=_synth_otel_result(final, otel_model),
                error_type=final.get("error_type"),
                error_msg=final.get("error_msg"),
            )
    except Exception as exc:
        logger.warning(
            "background finalize for stream run %s failed: %s",
            run_id, exc,
        )
        # On any exception in the finalize path, ensure the lock is
        # released. _record_idempotency releases on its own try/finally,
        # so this catches the pre-record exceptions.
        _release_idempotency_ctx(idem)


def _replay_cached_stream(cached: dict) -> StreamingResponse:
    """Replay a previously-recorded stream as a fresh SSE response. The
    chunks were captured verbatim from the original `event_generator`, so
    the consumer sees the same wire bytes (modulo HTTP framing) as the
    first call. Same `aitelier_run_id` on every chunk per the original."""
    chunks = cached.get("chunks") or []

    async def generator():
        for chunk in chunks:
            yield _sse_event("", chunk)
        yield "data: [DONE]\n\n"

    return _sse_response(generator())


async def _llm_chat_completion(
    req: ChatCompletionRequest, request: Request, *, run_id: str,
) -> dict:
    """LLM path: passthrough to LiteLLM, stamp run state, return OpenAI shape."""
    cid = request.state.correlation_id

    # Pull a system prompt hash for the run record without rebuilding messages.
    sys_msgs = [m for m in req.messages if m.get("role") == "system"]
    sp_hash = hash_system_prompt(
        "\n\n".join(str(m.get("content") or "") for m in sys_msgs) or None,
    )

    body = _llm_body_from_request(req)
    # On the LLM path, `body` IS the rendered message set (we don't fold
    # messages into a system prompt the way the agent path does — the
    # LLM provider sees `request_body.messages` verbatim modulo
    # response_format gates). Capture both for symmetry.
    spec = RunSpec(
        run_id=run_id, kind="complete", model=req.model,
        trace_tag=None, correlation_id=cid,
        system_prompt_hash=sp_hash,
        metadata={"correlation_id": cid},
        request_body=req.model_dump(exclude_none=True),
        rendered_messages=body.get("messages") if isinstance(body, dict) else None,
    )

    async def _do() -> dict:
        try:
            resp = await chat_completion(body, timeout=req.timeout or 60)
        except UnsupportedResponseFormat as exc:
            return {
                "status": "error",
                "error_type": "UnsupportedResponseFormat",
                "error_msg": scrub_error_text(str(exc)),
                "_aitelier_http_status": 400,
            }
        except LLMError as exc:
            return {
                "status": "error",
                "error_type": exc.error_type,
                "error_msg": scrub_error_text(str(exc)),
                "_aitelier_http_status": _http_status_for_llm_error(exc),
            }
        normalize_response_extras(body, resp)
        resp["aitelier_run_id"] = run_id
        resp["correlation_id"] = cid
        return resp

    with _track_inflight_run(run_id):
        result = await record_run(spec, _do())
    # OTel: emit a gen_ai.chat span describing this LLM call. No-op when
    # `[otel] enabled = false` (the default). Off the hot path, after
    # the run is durably recorded.
    from aitelier.otel import record_run_trace
    await record_run_trace(
        run_id=run_id,
        operation="chat",
        request_body=spec.request_body,
        result=result if result.get("status") != "error" else None,
        error_type=result.get("error_type"),
        error_msg=result.get("error_msg"),
    )
    if result.get("status") == "error":
        return chat_completion_error_envelope(
            result, run_id=run_id, correlation_id=cid,
        )
    return result


def _http_status_for_llm_error(exc: LLMError) -> int:
    """Map our LLMError taxonomy onto HTTP statuses the consumer can use.

    Upstream-supplied status_code wins when present (rate limits, auth);
    otherwise we fall back to error_type. Bad-gateway (502) is the catch-all
    for opaque provider failures."""
    if exc.status_code in (401, 403):
        return 401
    if exc.status_code == 429:
        return 429
    if exc.error_type == "Timeout":
        return 504
    if exc.error_type == "ProviderUnavailable":
        return 503
    return 502


def _http_status_for_agent_error(result: dict) -> int:
    """Map an agent-path error result onto an HTTP status, mirroring
    `_http_status_for_llm_error` so the same failure class returns the same
    status regardless of route (LLM vs agent). Agent errors carry the same
    `error_type` taxonomy (`classify_error`); 502 is the catch-all for opaque
    upstream/agent failures — never a misleading 500."""
    error_type = result.get("error_type")
    if error_type == "Timeout":
        return 504
    if error_type == "ProviderUnavailable":
        return 503
    if error_type == "RateLimited":
        return 429
    if error_type == "AuthError":
        return 401
    return 502


def _llm_body_from_request(req: ChatCompletionRequest) -> dict:
    body: dict[str, Any] = {"model": req.model, "messages": req.messages}
    for field in (
        "temperature", "max_tokens", "max_completion_tokens",
        "top_p", "n", "response_format", "reasoning_effort",
        "tool_choice", "user", "stream_options", "seed",
        "frequency_penalty", "presence_penalty", "stop",
        "logprobs", "top_logprobs",
    ):
        value = getattr(req, field, None)
        if value is not None:
            body[field] = value
    if req.tools:
        body["tools"] = req.tools
    # num_ctx is Ollama-specific — only attach it for Ollama routes so it
    # never reaches LiteLLM (which would reject an unknown param). The
    # Ollama adapter maps it to options.num_ctx.
    if req.num_ctx is not None:
        from aitelier.providers.ollama import routes_to_ollama
        if routes_to_ollama(req.model):
            body["num_ctx"] = req.num_ctx
    return body


async def _llm_chat_completion_stream(
    req: ChatCompletionRequest, request: Request, *, run_id: str,
):
    """Streaming LLM path. LiteLLM emits OpenAI-shape chunks; we forward
    them with `aitelier_run_id` stamped on each. Run state is finalized
    when the stream completes (usage from the final chunk)."""
    cid = request.state.correlation_id
    sys_msgs = [m for m in req.messages if m.get("role") == "system"]
    sp_hash = hash_system_prompt(
        "\n\n".join(str(m.get("content") or "") for m in sys_msgs) or None,
    )

    body = _llm_body_from_request(req)
    await start_run(RunSpec(
        run_id=run_id, kind="complete", model=req.model,
        correlation_id=cid, system_prompt_hash=sp_hash,
        metadata={"correlation_id": cid},
        request_body=req.model_dump(exclude_none=True),
        rendered_messages=body.get("messages") if isinstance(body, dict) else None,
    ))

    async def event_generator():
        final: dict | None = None
        accumulated: list[str] = []
        reasoning_accumulated: list[str] = []
        tool_call_seen = False
        usage: dict | None = None
        finish_reason: str | None = None
        # Count this stream against the concurrency cap + /v1/runs/active for
        # its lifetime, matching the agent stream path (which registers its
        # producer task). Popped in the finally below.
        task = asyncio.current_task()
        if task is not None:
            _active_runs[run_id] = task
        try:
            async for chunk in chat_completion_stream(
                body, timeout=req.timeout or 60,
            ):
                chunk["aitelier_run_id"] = run_id
                chunk["correlation_id"] = cid
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        accumulated.append(piece)
                    rpiece = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                    )
                    if rpiece:
                        reasoning_accumulated.append(rpiece)
                    if delta.get("tool_calls"):
                        tool_call_seen = True
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                yield _sse_event("", chunk)

            # Synthetic final chunk with aitelier extras: parsed JSON for
            # response_format consumers and the empty-exit signal for
            # reasoning-budget-burned cases. OpenAI clients ignore unknown
            # fields, so this is safe drop-in even mid-spec.
            extras = stream_final_extras(
                body,
                accumulated_content="".join(accumulated),
                reasoning_seen="".join(reasoning_accumulated),
                tool_call_seen=tool_call_seen,
                completion_tokens=(usage or {}).get("completion_tokens", 0),
            )
            if extras:
                yield _sse_event("", {
                    "aitelier_run_id": run_id,
                    "correlation_id": cid,
                    **extras,
                })
            yield "data: [DONE]\n\n"
            final = {
                "kind": "complete", "provider": req.model, "status": "ok",
                "content": "".join(accumulated),
                "usage": {
                    "input_tokens": (usage or {}).get("prompt_tokens", 0),
                    "output_tokens": (usage or {}).get("completion_tokens", 0),
                    "total_tokens": (usage or {}).get("total_tokens", 0),
                },
                "finish_reason": finish_reason or "stop",
            }
        except (LLMError, UnsupportedResponseFormat) as exc:
            err_type = (
                "UnsupportedResponseFormat"
                if isinstance(exc, UnsupportedResponseFormat)
                else getattr(exc, "error_type", "ProviderError")
            )
            scrubbed_msg = scrub_error_text(str(exc))
            final = {
                "kind": "complete", "provider": req.model, "status": "error",
                "error_type": err_type, "error_msg": scrubbed_msg,
                "finish_reason": "error",
            }
            yield _sse_event("", {
                "error": {"type": err_type, "message": scrubbed_msg},
                "aitelier_run_id": run_id,
            })
        finally:
            _active_runs.pop(run_id, None)
            # final is None when the consumer disconnected mid-stream (the task
            # is cancelled before a terminal chunk). Fabricate a `cancelled`
            # terminal so the run doesn't stay state=running forever — the same
            # guard the agent stream path uses, but with kind="complete".
            if final is None:
                final = {
                    "kind": "complete", "provider": req.model, "status": "cancelled",
                    "error_type": "Cancelled",
                    "error_msg": "consumer disconnected mid-stream",
                    "finish_reason": "cancelled",
                }
            state = _terminal_state_from_final(final)
            store = await get_store()
            try:
                await store.finalize_run(run_id, final, state=state)
            except (KeyError, ValueError):
                # Race: cancel endpoint or another path already finalized the
                # row. Same guard the agent stream's _finalize_stream_run uses.
                pass
            # OTel: emit gen_ai.chat span after the stream terminates,
            # carrying accumulated usage + finish_reason. No-op when
            # OTel is disabled. Synthesize a chat-completions-shape
            # result so `gen_ai_response_attrs` finds usage in the
            # OpenAI slots (`prompt_tokens` / `completion_tokens`).
            from aitelier.otel import record_run_trace
            await record_run_trace(
                run_id=run_id,
                operation="chat",
                request_body=req.model_dump(exclude_none=True),
                result=_synth_otel_result(final, req.model),
                error_type=final.get("error_type"),
                error_msg=final.get("error_msg"),
            )

    return _sse_response(event_generator())


def _synth_otel_result(final: dict, model: str) -> dict | None:
    """OpenAI-shape envelope so `gen_ai_response_attrs` finds usage/finish
    in the slots it reads (`prompt_tokens` / `completion_tokens`). None on
    error so the span records the error_type instead of empty usage."""
    if final.get("status") == "error":
        return None
    usage = final.get("usage") or {}
    return {
        "model": model,
        "choices": [{"finish_reason": final.get("finish_reason")}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
        },
    }
