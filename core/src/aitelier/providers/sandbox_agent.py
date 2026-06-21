"""Sandbox Agent (Rivet) provider — runs coding agents via ACP.

Replaces the direct claude-code / codex subprocess paths. All agent calls go
through the Sandbox Agent HTTP server, which speaks ACP (Agent Client Protocol)
to the underlying agent. Sandboxing, env scoping, and event normalization are
the Sandbox Agent's responsibility.

Transport (Sandbox Agent's HTTP wrapping of ACP):
  - POST /v1/acp/{server_id}?agent=<id>  — send JSON-RPC envelope
      • 200 + AcpEnvelope for requests with a result
      • 202 (no body) for notifications
  - GET  /v1/acp/{server_id}             — SSE stream of envelopes
      Notifications (session/update) arrive on the stream during long-running
      prompts; request responses come back synchronously on the POST.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aitelier.config import get_config
from aitelier.errors import classify_error, scrub_error_text
from aitelier.providers.acp_transport import (
    ACP_PROTOCOL_VERSION as _ACP_PROTOCOL_VERSION,
)
from aitelier.providers.acp_transport import (
    AcpClient,
    AcpError,
    _is_local_url,
    _persist_sandbox_server_id,
    _scrub_sandbox_url,
    _warn_remote_misconfig,
)

logger = logging.getLogger("aitelier.sandbox_agent")

# Transport layer (AcpClient, AcpError, url scrubber, preflight warnings,
# run-row stamping) lives in `acp_transport.py`. Imported above so existing
# call sites here and external test patches like
# `aitelier.providers.sandbox_agent.AcpClient` keep resolving.


class _RunEventEmitter:
    """Tiny helper that appends run_events to the durable store, monotonically.

    Best-effort: if the store hiccups, the run keeps going — observability
    must never block agent execution.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._seq = 0

    async def emit(self, kind: str, payload: dict | None = None) -> None:
        from aitelier.storage import RunEvent, get_store
        self._seq += 1
        try:
            store = await get_store()
            await store.append_event(RunEvent(
                run_id=self.run_id, seq=self._seq, kind=kind,
                payload=payload or {},
            ))
        except Exception as exc:
            # Event-append is observability, not load-bearing. Don't fail
            # the run if storage is degraded — surface in logs instead.
            logger.debug(
                "event emit failed for run %s kind=%s: %s: %s",
                self.run_id, kind, type(exc).__name__, exc,
            )


def _build_session_new_meta(
    agent_name: str,
    *,
    system_prompt: str | None,
    agent_model: str | None,
    tool_allowlist: list[str] | None,
    max_turns: int | None,
) -> dict | None:
    """Build the backend-specific `_meta` block for ACP `session/new`.

    Each ACP bridge reads config from a different place. This builds the
    claude-only `_meta` block; non-claude backends return None here and get
    their config via the advertised verbs (`session/set_model`,
    `session/set_config_option`, `session/set_mode`) in `_open_acp_session`.

    claude-agent-acp ≥0.36 (dist/acp-agent.js:1371-1450) reads:
      - `_meta.systemPrompt`         → system prompt override
      - `_meta.claudeCode.options.*` → spread into Claude Agent SDK
        options (maxTurns, model, allowedTools, …).

    Other backends (codex-acp, opencode, …) ignore `_meta` keys they
    don't recognize, so passing claude-shaped meta to them is safe.
    """
    if agent_name != "claude":
        return None

    options: dict = {}
    if agent_model:
        options["model"] = agent_model
    if tool_allowlist:
        options["allowedTools"] = list(tool_allowlist)
    if max_turns is not None:
        options["maxTurns"] = max_turns

    meta: dict = {}
    if system_prompt:
        meta["systemPrompt"] = system_prompt
    if options:
        meta["claudeCode"] = {"options": options}
    return meta or None


def _advertised_config_options(session_resp: Any) -> dict[str, dict]:
    """Map an ACP `session/new` response to its advertised config options,
    keyed by ACP `category` ("model", "thought_level", "mode", …).

    Each entry is `{"id": <option-id>, "values": [<advertised value>, …]}`.
    Option *ids* differ across backends (codex `reasoning_effort` vs claude
    `effort`) but the `category` is shared, so we normalize on category and
    use the per-backend `id` when calling `session/set_config_option`."""
    out: dict[str, dict] = {}
    if not isinstance(session_resp, dict):
        return out
    for opt in session_resp.get("configOptions") or []:
        if not isinstance(opt, dict):
            continue
        oid = opt.get("id")
        if not oid:
            continue
        category = opt.get("category") or oid
        values = [o["value"] for o in opt.get("options") or []
                  if isinstance(o, dict) and o.get("value") is not None]
        out[category] = {"id": oid, "values": values}
    return out


def _run_context_block(run_id: str) -> str:
    return f"<aitelier_context>\nrun_id={run_id}\n</aitelier_context>"


def _compose_system_text(system_prompt: str | None, run_id: str) -> str | None:
    """Combine the run-context block (so the inner agent can pass
    `parent_run_id` when dispatching subagents) with the caller's system
    prompt. Returns None when neither is present."""
    parts = []
    if run_id:
        parts.append(_run_context_block(run_id))
    if system_prompt:
        parts.append(system_prompt)
    return "\n\n".join(parts) or None


async def _open_acp_session(
    client: AcpClient,
    *,
    agent_name: str,
    workspace: str | None,
    mcp_servers: list[dict] | None,
    system_prompt: str | None,
    agent_model: str | None,
    tool_allowlist: list[str] | None,
    max_turns: int | None,
    reasoning_effort: str | None = None,
    approval_mode: str | None = None,
    run_id: str = "",
) -> str:
    """Drive the ACP handshake up to a usable session_id.

    initialize → session/new → start_stream → apply session config.

    Capabilities are advertised honestly: aitelier doesn't service fs/*
    or terminal/* (the SSE consumer rejects them in `_respond_to_agent_request`),
    so we say `false`. The agent then never asks, instead of asking and
    hanging on the rejection.

    `system_prompt` is consumed only by claude's session/new `_meta`; other
    backends have no system channel and receive their system instructions
    folded into the prompt by the caller (so it's harmlessly unused here).
    """
    await client.call("initialize", {
        "protocolVersion": _ACP_PROTOCOL_VERSION,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": "aitelier", "version": "0.1.0"},
    }, first=True)

    # Run-context + system prompt for claude's `_meta`. Non-claude backends
    # get this folded into the prompt by the caller; their `_meta` is None.
    system_prompt = _compose_system_text(system_prompt, run_id)

    session_new_params: dict = {
        "cwd": workspace or ".",
        "mcpServers": _adapt_mcp_servers(mcp_servers, run_id=run_id),
    }
    meta = _build_session_new_meta(
        agent_name,
        system_prompt=system_prompt,
        agent_model=agent_model,
        tool_allowlist=tool_allowlist,
        max_turns=max_turns,
    )
    if meta is not None:
        session_new_params["_meta"] = meta

    session_resp = await client.call("session/new", session_new_params)
    if isinstance(session_resp, dict):
        session_id = session_resp.get("sessionId")
        if not session_id:
            # The `mock` backend (and any future ACP server that botches
            # the handshake) returns a session/new response without a
            # `sessionId`. Surface this as a classified ProviderError
            # instead of letting KeyError leak through `error_type`.
            raise AcpError(
                -32603,
                f"session/new returned no sessionId "
                f"(keys: {sorted(session_resp.keys())}). The backend's "
                f"handshake is broken — the `mock` agent in older "
                f"Sandbox Agent builds is a known case.",
            )
    else:
        session_id = session_resp

    client.start_stream()

    # Apply session config from what the backend actually advertised, in the
    # order model → reasoning → approval (claude rebuilds its effort options
    # from the current model, so model must land first). claude takes its model
    # via `_meta` above; non-claude takes it via `session/set_model`.
    #
    # The session is live now — if a validation/apply step raises (bad model,
    # unadvertised reasoning_effort/approval_mode), close it before propagating
    # so we don't orphan the backend's child process. (Regression guard for the
    # leaked-subprocess class; see test_*_closes_session_when_*.)
    try:
        advertised = _advertised_config_options(session_resp)
        if agent_name != "claude" and agent_model:
            await _apply_model(client, session_id, agent_name, agent_model, advertised)
        if reasoning_effort is not None:
            await _apply_categorical(
                client, session_id, agent_name, advertised,
                category="thought_level", concept="reasoning_effort",
                value=reasoning_effort,
            )
        if approval_mode is not None:
            await _apply_categorical(
                client, session_id, agent_name, advertised,
                category="mode", concept="approval_mode", value=approval_mode,
            )
    except BaseException:
        await _close_acp_session(client, session_id)
        raise

    return session_id


def _validate_advertised(
    agent_name: str, advertised: dict, *, category: str, concept: str, value: str,
) -> dict:
    """Return the advertised option for `category`, raising a precise AcpError
    if the backend doesn't offer it or `value` isn't one of its values."""
    opt = advertised.get(category)
    if opt is None:
        raise AcpError(
            -32601,
            f"backend '{agent_name}' does not support {concept} "
            f"(no '{category}' option advertised).",
        )
    values = opt.get("values") or []
    if values and value not in values:
        raise AcpError(
            -32602,
            f"backend '{agent_name}' {concept} '{value}' not offered. "
            f"Available: {', '.join(values)}.",
        )
    return opt


async def _apply_model(
    client: AcpClient, session_id: str, agent_name: str,
    agent_model: str, advertised: dict,
) -> None:
    """Set the inner model via `session/set_model`. Validates against the
    advertised model values first — `set_model` is permissive (accepts any id
    and only fails later at prompt time), so an eager check turns a wasted turn
    into a precise error. `modelId` must be backend-native (codex: 'gpt-5.4');
    `openai/*` and curated aliases are LLM-path ids the backend rejects."""
    opt = advertised.get("model")
    values = (opt or {}).get("values") or []
    if values and agent_model not in values:
        raise AcpError(
            -32602,
            f"backend '{agent_name}' does not offer inner model "
            f"'{agent_model}'. Available: {', '.join(values)}. Use a "
            f"backend-native id (e.g. 'agent:{agent_name}/{values[0]}'); "
            f"'openai/…' and curated aliases are LLM-path ids, not agent "
            f"inner-model ids.",
        )
    await client.call("session/set_model", {
        "sessionId": session_id, "modelId": agent_model,
    })


async def _apply_categorical(
    client: AcpClient, session_id: str, agent_name: str, advertised: dict,
    *, category: str, concept: str, value: str,
) -> None:
    """Validate `value` against the advertised option for `category`, then set
    it via the category's ACP method: `mode` → `session/set_mode`, everything
    else (e.g. `thought_level`) → `session/set_config_option {configId}`."""
    opt = _validate_advertised(
        agent_name, advertised, category=category, concept=concept, value=value,
    )
    if category == "mode":
        await client.call("session/set_mode", {
            "sessionId": session_id, "modeId": value,
        })
    else:
        await client.call("session/set_config_option", {
            "sessionId": session_id, "configId": opt["id"], "value": value,
        })


async def _close_acp_session(client: AcpClient, session_id: str) -> None:
    """Tear down an ACP session.

    Uses `client.call` (request/response) rather than `client.notify`
    (fire-and-forget) so the server acknowledges receipt before our HTTP
    connection closes. Without the round-trip, sandbox-agent can drop
    the close message on connection teardown and leave child agent
    processes alive indefinitely.

    Failures are swallowed and logged: this runs from cleanup paths
    where re-raising would mask the original error. A short timeout
    keeps a stuck server from hanging the cleanup.
    """
    try:
        await asyncio.wait_for(
            client.call("session/close", {"sessionId": session_id}),
            timeout=5.0,
        )
    except Exception as exc:
        logger.warning(
            "session/close failed for session %s: %s — child process "
            "may leak. Check sandbox-agent state.",
            session_id, exc,
        )


async def probe_backend_config_options(
    cfg, backend: str, *, timeout: float = 10.0,
) -> dict | None:
    """Open a throwaway ACP session to read what `backend` actually advertises:
    `{"models": [...], "reasoning_levels": [...], "approval_modes": [...]}`.

    Used by `GET /v1/models` to surface a backend's real, selectable inner
    models / reasoning levels / approval modes (the LiteLLM catalog isn't the
    backend's list). Best-effort: returns None on any failure so `/v1/models`
    never fails because one backend probe timed out. Spawns and tears down a
    short-lived agent process (`session/new` → `session/close`)."""
    try:
        async with AcpClient(cfg.base_url, backend, token=cfg.token,
                             timeout=timeout) as client:
            await client.call("initialize", {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "aitelier", "version": "0.1.0"},
            }, first=True)
            resp = await asyncio.wait_for(
                client.call("session/new", {"cwd": "/tmp", "mcpServers": []}),
                timeout=timeout,
            )
            advertised = _advertised_config_options(resp)
            sid = resp.get("sessionId") if isinstance(resp, dict) else None
            if sid:
                await _close_acp_session(client, sid)
            return {
                "models": (advertised.get("model") or {}).get("values") or [],
                "reasoning_levels": (advertised.get("thought_level") or {}).get("values") or [],
                "approval_modes": (advertised.get("mode") or {}).get("values") or [],
            }
    except Exception as exc:
        logger.warning(
            "config-option probe for backend %s failed (%s: %s) — /v1/models "
            "will omit its advertised options this cycle.",
            backend, type(exc).__name__, exc,
        )
        return None


def _prompt_params(
    session_id: str, prompt: str, response_format: dict | None,
) -> dict:
    """Build the session/prompt request body."""
    params: dict = {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}],
    }
    if response_format and response_format.get("type") == "json_schema":
        params["responseFormat"] = response_format
    return params


async def call_via_sandbox(
    name: str,
    prompt: str,
    *,
    workspace: str | None = None,
    system_prompt: str | None = None,
    mcp_servers: list[dict] | None = None,
    tool_allowlist: list[str] | None = None,
    response_format: dict | None = None,
    max_turns: int | None = None,
    agent_model: str | None = None,
    reasoning_effort: str | None = None,
    approval_mode: str | None = None,
    timeout: int = 600,
    run_id: str = "",
) -> dict:
    """Run an agent via Sandbox Agent. Returns aitelier's standard result dict.

    Thin wrapper over call_via_sandbox_stream: consumes the event stream and
    returns the final aggregated `done` (or surfaces error/timeout) as a dict.

    Parameter routing (see _open_acp_session / _build_session_new_meta):
      system_prompt    → session/new `_meta` for claude; folded into the prompt
                         text for other backends
      agent_model      → session/new `_meta` for claude; validated
                         `session/set_model` for other backends
      tool_allowlist,
      max_turns        → session/new `_meta` for claude only (rejected upstream
                         with a 400 for other backends)
      reasoning_effort → validated `session/set_config_option` (advertised
                         `thought_level`)
      approval_mode    → validated `session/set_mode` (advertised `mode`)
      mcp_servers      → session/new mcpServers
      response_format  → session/prompt (json_schema only)
      workspace        → session/new cwd
    """
    start = time.monotonic()
    final: dict | None = None
    last_error: dict | None = None
    try:
        async def _consume() -> None:
            nonlocal final, last_error
            async for event in call_via_sandbox_stream(
                name, prompt,
                workspace=workspace, system_prompt=system_prompt,
                mcp_servers=mcp_servers, tool_allowlist=tool_allowlist,
                response_format=response_format, max_turns=max_turns,
                agent_model=agent_model,
                reasoning_effort=reasoning_effort, approval_mode=approval_mode,
                timeout=timeout, run_id=run_id,
            ):
                etype = event.get("type")
                if etype == "done":
                    final = {k: v for k, v in event.items() if k != "type"}
                elif etype == "error":
                    last_error = event

        await asyncio.wait_for(_consume(), timeout=timeout)
    except TimeoutError:
        return _timeout_result(name, run_id, time.monotonic() - start)

    if final is not None:
        return final
    if last_error is not None:
        # Reconstruct a full Result from the streamed error event. The
        # producer already classified the exception via `classify_error`
        # before serializing it into the event — preserve that type
        # instead of re-wrapping as `RuntimeError`, which would lose the
        # taxonomy and surface raw class names to consumers.
        cfg = get_config().sandbox_agent
        exc_msg = last_error.get("error_msg") or "stream error"
        return _error_result(
            name, run_id, RuntimeError(exc_msg),
            time.monotonic() - start, base_url=cfg.base_url,
            error_type=last_error.get("error_type"),
        )
    cfg = get_config().sandbox_agent
    return _error_result(
        name, run_id, RuntimeError("stream ended without a result"),
        time.monotonic() - start, base_url=cfg.base_url,
        error_type="ProviderError",
    )


# ---------------------------------------------------------------------------
# Streaming entry point used by the agent path of /v1/chat/completions
# (`stream: true`) and by the non-streaming call_via_sandbox wrapper.
# ---------------------------------------------------------------------------


async def _translate_note(
    note: dict, *,
    text_chunks: list[str], tool_calls: list[dict],
    emitter: _RunEventEmitter,
) -> dict | None:
    """Map one ACP notification → an aitelier event, with side effects.

    Mutates `text_chunks` / `tool_calls` so the caller can aggregate
    them into the final `done` payload, and emits an event to
    `emitter`. Returns the event dict (for the caller to yield) or
    None when the notification doesn't map to a surfaced event.

    Each notification is consumed exactly once (from the client queue),
    so both the live phase and the post-prompt drain accumulate into the
    same `text_chunks` / `tool_calls` — a tool event that lands during
    drain still counts toward `done.tool_call_count`, keeping the terminal
    payload consistent with the events the consumer saw on the stream.
    """
    ev = _notification_to_event(note)
    if ev is None:
        return None
    if ev["type"] == "delta":
        text_chunks.append(ev["content"])
    elif ev["type"] in ("tool_call", "tool_result"):
        tool_calls.append({k: v for k, v in ev.items() if k != "type"})
    await emitter.emit(
        ev["type"], {k: v for k, v in ev.items() if k != "type"},
    )
    return ev


async def call_via_sandbox_stream(
    name: str,
    prompt: str,
    *,
    workspace: str | None = None,
    system_prompt: str | None = None,
    mcp_servers: list[dict] | None = None,
    tool_allowlist: list[str] | None = None,
    response_format: dict | None = None,
    max_turns: int | None = None,
    agent_model: str | None = None,
    reasoning_effort: str | None = None,
    approval_mode: str | None = None,
    timeout: int = 600,
    run_id: str = "",
):
    """Streaming variant of call_via_sandbox.

    Yields events as they arrive from the ACP session/update notifications,
    then a terminal `done` (or `error`) event:

      {"type": "delta",       "content": "..."}
      {"type": "tool_call",   "server": "...", "tool": "...", "input": {...}}
      {"type": "tool_result", "tool": "...", "output": ..., "elapsed_ms": ...}
      {"type": "done",        ... full aggregated Result dict ...}
      {"type": "error",       "error_type": "...", "error_msg": "..."}
    """
    cfg = get_config().sandbox_agent
    start = time.monotonic()

    _warn_remote_misconfig(cfg.base_url, workspace, mcp_servers)

    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    emitter = _RunEventEmitter(run_id)
    # `sandbox_url` is internal topology — keep it out of consumer-visible
    # event payloads so it can't end up in dashboards or leak to remote
    # callers in hosted mode. The full URL stays in the runs row
    # (`sandbox_url` column) for operator debugging.
    await emitter.emit("start", {
        "agent": name,
        "sandbox": "local" if _is_local_url(cfg.base_url) else "remote",
        "workspace": workspace,
    })

    # claude takes its system prompt natively via session/new `_meta`; every
    # other backend has no system channel, so fold the run-context + system
    # prompt into the prompt text and don't pass it to the session opener.
    if name == "claude":
        session_system_prompt = system_prompt
        effective_prompt = prompt
    else:
        session_system_prompt = None
        sys_text = _compose_system_text(system_prompt, run_id)
        effective_prompt = f"{sys_text}\n\n{prompt}" if sys_text else prompt

    try:
        async with AcpClient(cfg.base_url, name, token=cfg.token,
                             timeout=timeout) as client:
            await _persist_sandbox_server_id(run_id, cfg.base_url, client.server_id)
            session_id: str | None = None
            try:
                session_id = await _open_acp_session(
                    client,
                    agent_name=name,
                    workspace=workspace, mcp_servers=mcp_servers,
                    system_prompt=session_system_prompt, agent_model=agent_model,
                    tool_allowlist=tool_allowlist, max_turns=max_turns,
                    reasoning_effort=reasoning_effort,
                    approval_mode=approval_mode, run_id=run_id,
                )
                prompt_task = asyncio.create_task(client.call(
                    "session/prompt",
                    _prompt_params(session_id, effective_prompt, response_format),
                ))

                turn_result: dict | None = None
                prompt_err: dict | None = None
                try:
                    # Live phase: pump notifications until session/prompt
                    # completes. Tool events here count toward tool_calls.
                    while not prompt_task.done():
                        note = await client.next_notification(timeout=0.25)
                        if note is None:
                            continue
                        ev = await _translate_note(
                            note, text_chunks=text_chunks,
                            tool_calls=tool_calls, emitter=emitter,
                        )
                        if ev is not None:
                            yield ev

                    try:
                        turn_result = await prompt_task
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        prompt_err = {
                            "type": "error",
                            "error_type": classify_error(exc),
                            "error_msg": scrub_error_text(
                                _scrub_sandbox_url(str(exc), cfg.base_url)
                            ),
                        }

                    # Drain phase: surface trailing notifications even on
                    # prompt error, so the event stream is consistent.
                    # Final agent_message_chunks can land on the SSE stream
                    # slightly after session/prompt returns, so poll with a
                    # short timeout until the stream goes quiet rather than
                    # only sweeping what's already buffered. Bounded by the
                    # overall run timeout: unlike the non-streaming
                    # call_via_sandbox wrapper, this streaming entry point has
                    # no enclosing wait_for, so a backend that never goes quiet
                    # would drain forever without this deadline.
                    drain_deadline = start + timeout
                    while time.monotonic() < drain_deadline:
                        note = await client.next_notification(timeout=0.25)
                        if note is None:
                            break
                        ev = await _translate_note(
                            note, text_chunks=text_chunks,
                            tool_calls=tool_calls, emitter=emitter,
                        )
                        if ev is not None:
                            yield ev
                    else:
                        logger.warning(
                            "Sandbox %s run %s: drain exceeded run timeout "
                            "(%ss) without going quiet; ending stream.",
                            name, run_id, timeout,
                        )
                finally:
                    # Close session on every exit path — cancellation, prompt
                    # error, successful completion. Without this the inner
                    # agent process stays alive indefinitely.
                    await _close_acp_session(client, session_id)
                    session_id = None

                if prompt_err is not None:
                    await emitter.emit("error", prompt_err)
                    yield prompt_err
                    return

                yield await _build_done_event(
                    client=client, run_id=run_id, start=start,
                    turn_result=turn_result, response_format=response_format,
                    text_chunks=text_chunks, tool_calls=tool_calls,
                    emitter=emitter,
                )
            finally:
                # Race-window guard: if cancellation hit between
                # _close_acp_session() and `session_id = None` above,
                # session_id stays non-None and we retry the close here.
                if session_id is not None:
                    await _close_acp_session(client, session_id)

    except asyncio.CancelledError:
        await emitter.emit("cancelled", {})
        raise
    except Exception as exc:
        err = {
            "type": "error",
            "error_type": classify_error(exc),
            "error_msg": scrub_error_text(
                _scrub_sandbox_url(str(exc), cfg.base_url)
            ),
        }
        await emitter.emit("error", err)
        yield err


async def _build_done_event(
    *, client, run_id: str, start: float,
    turn_result: dict | None, response_format: dict | None,
    text_chunks: list[str], tool_calls: list[dict],
    emitter: _RunEventEmitter,
) -> dict:
    """Assemble the terminal `done` event from turn_result + accumulators.

    Lives in its own function because the streaming entry point's
    happy-path tail was the densest part of `call_via_sandbox_stream` —
    aggregator call + accumulator merge + finish emit. Pulling it out
    keeps the orchestration loop readable."""
    done = _aggregate_result(
        agent=client.agent,
        run_id=run_id,
        turn_result=turn_result,
        elapsed=time.monotonic() - start,
        response_format=response_format,
    )
    if text_chunks:
        done["content"] = "".join(text_chunks)
    if tool_calls:
        done["tool_calls"] = tool_calls
    done["type"] = "done"
    await emitter.emit("finish", {
        "finish_reason": done.get("finish_reason"),
        "tool_call_count": len(tool_calls),
    })
    return done


# ---------------------------------------------------------------------------
# Adapters / aggregation
# ---------------------------------------------------------------------------


def _notification_to_event(note: dict) -> dict | None:
    """Map an ACP session/update notification to an aitelier streaming event.

    The discriminator per the ACP `SessionUpdate` schema is `sessionUpdate`
    with snake_case values (`agent_message_chunk`, `tool_call`,
    `tool_call_update`, …). Older sandbox-agent versions emitted camelCase
    aliases (`messageChunk`, `toolCall`); we accept both.
    """
    params = note.get("params") or {}
    update = params.get("update") or params
    kind = (
        update.get("sessionUpdate")
        or update.get("type")
        or update.get("kind")
    )

    if kind in (
        "agent_message_chunk",
        "agentMessageChunk", "messageChunk", "text",
    ):
        content = update.get("content") or update.get("text")
        if isinstance(content, dict):
            content = content.get("text")
        if isinstance(content, str):
            return {"type": "delta", "content": content}
        return None

    if kind == "agent_thought_chunk":
        content = update.get("content")
        if isinstance(content, dict):
            content = content.get("text")
        if isinstance(content, str):
            return {"type": "thought", "content": content}
        return None

    if kind in ("tool_call", "toolCall"):
        return {
            "type": "tool_call",
            # ACP's stable per-invocation id — correlates this call with its
            # tool_result update(s), which arrive as separate notifications
            # (often several status pings per call). Without it, call↔result
            # pairing degrades to fragile adjacency.
            "id":     _acp_tool_call_id(update),
            "server": update.get("server") or update.get("serverName"),
            "tool":   update.get("name") or update.get("toolName") or update.get("title"),
            "input":  update.get("arguments") or update.get("input") or update.get("rawInput"),
        }

    if kind in ("tool_call_update", "toolCallUpdate", "toolResult", "tool_result"):
        output = (update.get("result") or update.get("output")
                  or update.get("rawOutput") or update.get("content"))
        if kind in ("tool_call_update", "toolCallUpdate"):
            # Multi-fire status pings. Surface a tool_result only on a
            # terminal update carrying output; intermediate pings would
            # otherwise append duplicate, output=None entries and inflate
            # tool_call_count.
            status = update.get("status")
            terminal = status in ("completed", "failed", "error", "cancelled")
            if not terminal and output is None:
                return None
        return {
            "type":       "tool_result",
            "id":         _acp_tool_call_id(update),
            "tool":       update.get("name") or update.get("toolName"),
            "output":     output,
            "elapsed_ms": update.get("elapsed_ms") or update.get("elapsedMs"),
        }

    return None


def _acp_tool_call_id(update: dict) -> str | None:
    """ACP's stable per-invocation id, across naming variants."""
    return (update.get("toolCallId") or update.get("tool_call_id")
            or update.get("id"))


def _adapt_mcp_servers(
    servers: list[dict] | None, *, run_id: str = "",
) -> list[dict]:
    """Convert aitelier's MCP server shape to ACP's wire schema.

    aitelier's public API uses `transport: "http" | "stdio"`. ACP's
    schema/schema.json (McpServerHttp / McpServerStdio) requires:
      - `type` as the discriminator (literal const, not `transport`)
      - `headers: [{name, value}]` required on http (empty list is valid)
      - `env: [{name, value}]` required on stdio (empty list is valid)

    When `run_id` is non-empty, AITELIER_RUN_ID is injected into every
    stdio server's env (unless the caller already set it). This lets
    aitelier-mcp expose the parent's run_id to the inner agent so it
    can dispatch subagents with the correct parent_run_id without
    out-of-band coordination.
    """
    if not servers:
        return []
    out: list[dict] = []
    for s in servers:
        transport = s.get("transport", "http")
        if transport == "http":
            out.append({
                "type": "http",
                "name": s["name"],
                "url": s.get("url", ""),
                "headers": s.get("headers", []),
            })
        elif transport == "stdio":
            env = list(s.get("env", []))
            if run_id and not any(
                e.get("name") == "AITELIER_RUN_ID" for e in env
            ):
                env.append({"name": "AITELIER_RUN_ID", "value": run_id})
            out.append({
                "type": "stdio",
                "name": s["name"],
                "command": s.get("command", ""),
                "args": s.get("args", []),
                "env": env,
            })
    return out


def _aggregate_result(
    *,
    agent: str,
    run_id: str,
    turn_result: dict | None,
    elapsed: float,
    response_format: dict | None,
) -> dict:
    """Build the final aitelier result dict from an ACP turn response.

    The streaming caller already drains notifications inline; we don't
    re-process them here. This function exists to centralise turn_result
    → result conversion (finish_reason, usage extraction, content
    fallback, json_schema parsing) so streaming and non-streaming
    paths produce identical shapes.
    """
    content = ""
    # Final stop_reason from the turn response if present
    finish_reason = "completed"
    # None until the backend actually reports usage — some backends (codex)
    # surface none, and a null run is honest where a fabricated 0/0/0 lies.
    usage = None
    if isinstance(turn_result, dict):
        finish_reason = (
            turn_result.get("stopReason")
            or turn_result.get("finish_reason")
            or finish_reason
        )
        # Some agents return the full text in turn_result.content; prefer it if chunks were empty.
        if not content:
            tr_content = turn_result.get("content")
            if isinstance(tr_content, str):
                content = tr_content
            elif isinstance(tr_content, list):
                content = "".join(
                    c.get("text", "") for c in tr_content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
        # Best-effort usage extraction. Different agent backends surface
        # tokens under different keys; we accept either snake or camel case
        # and the OpenAI-flavored {prompt,completion} or our own
        # {input,output} convention.
        raw_usage = turn_result.get("usage")
        if isinstance(raw_usage, dict):
            in_tok = (
                raw_usage.get("input_tokens")
                or raw_usage.get("inputTokens")
                or raw_usage.get("prompt_tokens")
                or raw_usage.get("promptTokens")
                or 0
            )
            out_tok = (
                raw_usage.get("output_tokens")
                or raw_usage.get("outputTokens")
                or raw_usage.get("completion_tokens")
                or raw_usage.get("completionTokens")
                or 0
            )
            tot_tok = (
                raw_usage.get("total_tokens")
                or raw_usage.get("totalTokens")
                or (in_tok + out_tok)
            )
            usage = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": tot_tok,
            }

    parsed = None
    if response_format and response_format.get("type") == "json_schema" and content:
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "kind": "agent",
        "provider": agent,
        "status": "ok",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": content,
        "parsed": parsed,
        "usage": usage,
        "finish_reason": finish_reason,
        "tool_calls": [],
        "cost_usd": None,  # See docs/INTEGRATION.md → "Cost tracking" for why.
        "error_type": None,
        "error_msg": None,
    }


def _timeout_result(agent: str, run_id: str, elapsed: float) -> dict:
    return {
        "kind": "agent",
        "provider": agent,
        "status": "error",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "timeout",
        "tool_calls": [],
        "cost_usd": None,
        "error_type": "Timeout",
        "error_msg": f"Agent exceeded timeout after {elapsed:.1f}s",
    }


def _error_result(
    agent: str, run_id: str, exc: Exception, elapsed: float,
    *, base_url: str | None = None,
    error_type: str | None = None,
) -> dict:
    """Build an error result dict with a descriptive message.

    Some httpx exceptions (notably ReadTimeout) have an empty str(exc).
    We always include a sandbox locator + elapsed time so consumers can
    tell *what* timed out without digging through logs — symbolic
    `sandbox=local|remote` rather than the literal URL, since the URL is
    internal topology that shouldn't appear in error payloads visible to
    remote callers.

    `error_type` lets callers preserve a previously-classified type
    (e.g. one already derived from a streamed `error` event inside the
    producer). Without that override we'd re-classify a generic wrapper
    exception and lose the original taxonomy, leaking `RuntimeError` /
    `ReadTimeout` etc. to consumers instead of the documented vocabulary.
    """
    msg = _scrub_sandbox_url(str(exc) or type(exc).__name__, base_url)
    parts = [msg]
    if base_url:
        parts.append(
            "sandbox=" + ("local" if _is_local_url(base_url) else "remote"),
        )
    parts.append(f"elapsed={elapsed:.1f}s")
    return {
        "kind": "agent",
        "provider": agent,
        "status": "error",
        "duration_s": round(elapsed, 2),
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "error",
        "tool_calls": [],
        "cost_usd": None,
        "error_type": error_type or classify_error(exc),
        "error_msg": " | ".join(parts),
    }
