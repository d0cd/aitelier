"""Request preparation + validation for /v1/chat/completions.

Pure functions: OpenAI messages -> (system_prompt, prompt) translation, few-shot
+ response-format folding into the system prompt, and the agent-path field
rejections / aitelier.* option validation. Extracted from server.py; the
orchestration functions there and endpoints/inference.py import from here.
"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException

from aitelier.config import get_config
from aitelier.openai_compat import (
    ChatCompletionRequest,
    agent_usage_to_openai,
    chat_completion_chunk,
    summarize_tool_calls,
)

logger = logging.getLogger("aitelier")


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
