"""Run/event -> dict projections for the HTTP boundary + credential redaction.

Pure functions, duck-typed over the storage dataclasses. Extracted from
server.py so the serialization/redaction concern lives on its own; server.py and
the endpoint routers import from here.
"""

from __future__ import annotations

_REDACTED = "[redacted]"

# Dict keys whose value is a credential and must be redacted from the wire
# projection. Matched case-insensitively so `Authorization` redacts the same
# as `authorization`.
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "token", "access_token", "refresh_token",
    "secret", "client_secret", "password", "passwd", "authorization",
})


def _redact_secrets(value):
    """Strip secret-bearing fields from any dict/list before it crosses the
    HTTP boundary.

    Authenticated callers reading `/v1/runs/{id}` or `/v1/schedules*` get
    `environment.mcp_servers[*].headers` (Bearer tokens for third-party MCP
    servers) and `prepare.commands[*].env` (DB DSNs, registry creds) back
    verbatim otherwise. Stored runs / schedules keep the original values —
    only the wire projection is redacted. The Sandbox Agent still receives
    real values at dispatch time."""
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            kl = k.lower() if isinstance(k, str) else k
            if kl in ("headers", "env") and isinstance(v, dict):
                # Schema shape for `env` / map-style headers: {name: value}.
                # Redact values, keep keys for debuggability.
                out[k] = {k2: _REDACTED for k2 in v}
            elif kl in ("headers", "env") and isinstance(v, list):
                # ACP `[{name, value}]` header shape: keep the name, redact the
                # value. Non-dict items (e.g. a list of header/env *names*) are
                # not credentials in this shape — recurse so nested dicts are
                # still redacted while bare scalars pass through unchanged.
                out[k] = [
                    {**i, "value": _REDACTED} if isinstance(i, dict) and "value" in i
                    else _redact_secrets(i)
                    for i in v
                ]
            elif kl in _SECRET_KEYS:
                out[k] = _REDACTED
            else:
                out[k] = _redact_secrets(v)
        return out
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


# Field set for the TraceRecord shape on /v1/traces — a strict subset of the
# Run dict. Kept narrower than _run_to_dict so /v1/traces stays focused on
# the observability summary while /v1/runs surfaces operational fields
# (state, sandbox info, environment).
_TRACE_RECORD_KEYS = frozenset({
    "trace_id", "started_at", "ended_at", "duration_ms", "model", "kind",
    "finish_reason", "tool_call_count", "input_tokens", "output_tokens",
    "total_tokens", "cost_usd", "system_prompt_hash", "trace_tag",
    "parent_run_id", "status", "error_type", "error_msg", "metadata",
})


def _duration_ms(run) -> int | None:
    """Wall-clock run duration in milliseconds (ended − started), or None
    when the run hasn't ended. Precomputed so dashboards don't re-derive it;
    maps to the OTel span duration."""
    if run.started_at and run.ended_at:
        return round((run.ended_at - run.started_at).total_seconds() * 1000)
    return None


def _run_to_dict(run) -> dict:
    """Canonical Run → dict converter used by /v1/runs*.

    Includes every operational field: state, sandbox info, environment,
    error info, tokens, cost. The narrower TraceRecord projection for
    /v1/traces is derived from this via `_run_to_trace_dict`.
    """
    return {
        "run_id": run.run_id,
        "trace_id": run.run_id,
        "state": run.state,
        "kind": run.kind,
        "agent_id": run.agent_id,
        "model": run.model,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "duration_ms": _duration_ms(run),
        "trace_tag": run.trace_tag,
        "correlation_id": run.correlation_id,
        "parent_run_id": run.parent_run_id,
        "sandbox_backend": run.sandbox_backend,
        "sandbox_url": run.sandbox_url,
        "sandbox_server_id": run.sandbox_server_id,
        "workspace": run.workspace,
        "environment": _redact_secrets(run.environment),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "cached_read_tokens": run.cached_read_tokens,
        "cached_write_tokens": run.cached_write_tokens,
        "cost_usd": run.cost_usd,
        "finish_reason": run.finish_reason,
        "tool_call_count": run.tool_call_count,
        "system_prompt_hash": run.system_prompt_hash,
        "status": run.status,
        "error_type": run.error_type,
        "error_msg": run.error_msg,
        "result": _redact_secrets(run.result),
        "metadata": _redact_secrets(run.metadata),
        # Same projection-boundary redaction as `environment` / `result` /
        # `metadata` — stored row keeps the originals; HTTP projection scrubs
        # `tools[*].function.parameters.api_key`-shaped fields and any
        # caller-supplied Authorization headers folded into the request.
        # `None` (no body captured — older run or schedule-side synthetic
        # failure) passes through unchanged so consumers can distinguish
        # "no record" from "empty body."
        "request_body": (
            _redact_secrets(run.request_body)
            if run.request_body is not None else None
        ),
        "rendered_messages": (
            _redact_secrets(run.rendered_messages)
            if run.rendered_messages is not None else None
        ),
    }


def _run_to_trace_dict(run) -> dict:
    """TraceRecord shape returned by /v1/traces.

    A narrower projection of `_run_to_dict` focused on observability fields
    (counts, tokens, cost, status). For full operational detail (state,
    sandbox info, environment), use /v1/runs.
    """
    full = _run_to_dict(run)
    return {k: full[k] for k in _TRACE_RECORD_KEYS if k in full}


def _event_to_dict(event) -> dict:
    """tool_call/tool_result payloads carry raw user arguments + tool
    outputs — both can contain credentials (a `bash` tool call's argv,
    or a `read_file` result returning a .env). Redact at the projection
    boundary; the durable row keeps the original for operator debugging."""
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "seq": event.seq,
        "kind": event.kind,
        "ts": event.ts.isoformat() if event.ts else None,
        "payload": _redact_secrets(event.payload),
    }
