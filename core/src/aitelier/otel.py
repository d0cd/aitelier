"""OpenTelemetry GenAI export — opt-in instrumentation for inference spans.

Off by default. When `[otel] enabled = true` is set in aitelier.toml,
`init_tracer_provider()` is called from the server lifespan and wires
an OTLP exporter. The LLM and agent paths then emit spans tagged with
GenAI semantic conventions
(https://opentelemetry.io/docs/specs/semconv/gen-ai/), so any OTLP-
compatible backend (Langfuse, Phoenix, Honeycomb, Datadog, Grafana
Tempo) ingests them without custom adapters.

Why opt-in:
- The OTel SDK + exporter packages aren't in the default install
  (`pyproject.toml` lists them under `[project.optional-dependencies]
  otel`). Operators install with `uv pip install aitelier[otel]`.
- A typical personal-runtime deployment doesn't need OTel; aitelier's
  own `runs` / `run_events` tables are the durable audit record. OTel
  exists for operators who want their inference traces in their
  existing observability backend alongside the rest of their service
  spans.

Design notes:
- All OTel imports are LAZY inside the init function — modules that
  don't enable OTel never pay the import cost (~50ms on a cold start
  for the full SDK + gRPC bindings).
- Helper functions to build gen_ai attribute dicts live in this module
  and are pure (no SDK imports), so they can be unit-tested without
  the SDK installed.
- Span emission goes through `record_run_trace` (async; reads the run's
  events from the store) → `record_inference_span` (builds the span tree),
  both of which check `_tracer` directly — when OTel is off they no-op
  cleanly and a default deployment pays nothing.
- The trace id IS the run id (a 32-hex W3C value), so a run is addressable
  by id in any OTLP backend; agent tool calls become child spans.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("aitelier.otel")

# Set by `init_tracer_provider()` when OTel is enabled. None otherwise.
# `record_inference_span` checks this directly.
_tracer: Any | None = None


def init_tracer_provider() -> None:
    """Build the global tracer provider from `[otel]` config.

    Called once from the server lifespan when `[otel] enabled = true`.
    Lazy-imports the OTel SDK + exporter so operators who don't enable
    OTel never pay the import cost.

    No-op (with a clear log line) when:
      - `[otel] enabled = false` (default)
      - the OTel packages aren't installed (graceful degradation —
        the operator gets a clear error message instead of an
        ImportError at first request)
    """
    global _tracer

    from aitelier.config import get_config
    cfg = get_config().otel
    if not cfg.enabled:
        return
    if _tracer is not None:
        return  # already initialized; idempotent

    try:
        # Lazy imports — kept here so a default install (no [otel]
        # extra) doesn't trip on missing modules.
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning(
            "OpenTelemetry enabled in config but SDK not installed (%s). "
            "Install with: uv pip install aitelier[otel]. Spans will not "
            "be emitted until this is resolved.",
            exc,
        )
        return

    exporter = _build_exporter(cfg)
    if exporter is None:
        return  # _build_exporter already logged

    resource = Resource.create({"service.name": cfg.service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("aitelier", "0.1.0")

    logger.info(
        "OpenTelemetry GenAI export enabled — exporter=%s endpoint=%s "
        "service.name=%s capture_content=%s",
        cfg.protocol, cfg.endpoint or "(env-default)",
        cfg.service_name, cfg.capture_content,
    )


def shutdown_tracer_provider() -> None:
    """Flush + shutdown — called from the server lifespan exit."""
    global _tracer
    if _tracer is None:
        return
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception as exc:
        logger.debug("OTel shutdown errored (best-effort): %s", exc)
    _tracer = None


def _build_exporter(cfg) -> Any | None:
    """Return an OTLP span exporter or None on import/config failure.

    grpc vs http selected by `[otel] protocol`. Both speak OTLP/protobuf;
    grpc is the canonical default (port 4317), http is the firewall-
    friendly fallback (port 4318).
    """
    proto = (cfg.protocol or "grpc").lower()
    try:
        if proto == "http":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
    except ImportError as exc:
        logger.warning(
            "OTLP exporter for protocol=%s not installed (%s). Install "
            "with: uv pip install aitelier[otel]",
            proto, exc,
        )
        return None

    kwargs: dict[str, Any] = {}
    if cfg.endpoint:
        kwargs["endpoint"] = cfg.endpoint
    if proto == "grpc":
        # grpc accepts an `insecure` kwarg explicitly; http path infers
        # from the scheme of the endpoint URL.
        kwargs["insecure"] = cfg.insecure
    return OTLPSpanExporter(**kwargs)


# ---------------------------------------------------------------------------
# GenAI attribute builders — pure functions, no OTel imports needed.
# Unit-testable on a default install (no [otel] extra). The semantic-
# convention attribute names follow
# https://opentelemetry.io/docs/specs/semconv/gen-ai/llm-spans/ and are
# stable as of OTel semconv v1.30.
# ---------------------------------------------------------------------------


# `gen_ai.system` allowed values come from the convention's registry.
# We map aitelier's model-prefix routing to the canonical names so
# trace backends can filter by provider.
_GEN_AI_SYSTEM_PREFIX = (
    ("anthropic/", "anthropic"),
    ("claude-",    "anthropic"),
    ("openai/",    "openai"),
    ("gpt-",       "openai"),
    ("o1-",        "openai"),
    ("o3-",        "openai"),
    ("gemini-",    "gemini"),
    ("ollama/",    "ollama"),
)


def gen_ai_system_for_model(model: str | None) -> str:
    """Derive the `gen_ai.system` attribute from a model name.

    - `agent:<backend>[/<inner>]` → `aitelier.agent.<backend>` (custom
      namespace; standard convention has no name for "sandboxed agent
      runtime invoking an LLM"). Trace backends still filter cleanly.
    - `local` → `ollama` (our `local` alias resolves to Ollama).
    - Known prefixes (claude-, gpt-, ollama/, …) → canonical name.
    - Unknown → `"_OTHER"` per the convention's "unmapped" placeholder.
    """
    if not model:
        return "_OTHER"
    if model.startswith("agent:"):
        backend = model[len("agent:"):].split("/", 1)[0]
        return f"aitelier.agent.{backend}"
    if model == "local":
        return "ollama"
    lower = model.lower()
    for prefix, system in _GEN_AI_SYSTEM_PREFIX:
        if lower.startswith(prefix):
            return system
    return "_OTHER"


def gen_ai_request_attrs(request_body: dict | None) -> dict[str, Any]:
    """Build the per-request gen_ai.request.* attributes from a captured
    request body. Tolerant of partial / missing fields — only sets keys
    whose values are present.

    Conventions covered:
      - gen_ai.system
      - gen_ai.request.model
      - gen_ai.request.max_tokens (also for max_completion_tokens)
      - gen_ai.request.temperature
      - gen_ai.request.top_p
      - gen_ai.request.frequency_penalty
      - gen_ai.request.presence_penalty
      - gen_ai.request.stop_sequences

    `gen_ai.operation.name` is set by `record_inference_span` from its
    `operation` arg, not derived here. Response-side attributes live in
    `gen_ai_response_attrs`.
    """
    if not isinstance(request_body, dict):
        return {}
    out: dict[str, Any] = {}
    model = request_body.get("model")
    if model:
        out["gen_ai.request.model"] = model
        out["gen_ai.system"] = gen_ai_system_for_model(model)

    # max_completion_tokens wins (OpenAI reasoning-model field); fall
    # back to max_tokens. The convention has one slot.
    mct = request_body.get("max_completion_tokens") or request_body.get("max_tokens")
    if isinstance(mct, int) and mct > 0:
        out["gen_ai.request.max_tokens"] = mct

    for src, dst in (
        ("temperature",         "gen_ai.request.temperature"),
        ("top_p",                "gen_ai.request.top_p"),
        ("frequency_penalty",    "gen_ai.request.frequency_penalty"),
        ("presence_penalty",     "gen_ai.request.presence_penalty"),
    ):
        v = request_body.get(src)
        if isinstance(v, (int, float)):
            out[dst] = float(v)

    stop = request_body.get("stop")
    if isinstance(stop, str):
        out["gen_ai.request.stop_sequences"] = (stop,)
    elif isinstance(stop, list):
        out["gen_ai.request.stop_sequences"] = tuple(stop)

    return out


def gen_ai_response_attrs(result: dict | None) -> dict[str, Any]:
    """Build gen_ai.response.* + gen_ai.usage.* from a finalized result
    envelope (the dict aitelier persists to `runs.result_json`).

    Tolerates the shape differences between LLM, embedding, and agent
    paths — each has slightly different fields. Only sets keys whose
    values are present and well-typed.

    Conventions covered:
      - gen_ai.response.id
      - gen_ai.response.model
      - gen_ai.response.finish_reasons
      - gen_ai.usage.input_tokens
      - gen_ai.usage.output_tokens
    """
    if not isinstance(result, dict):
        return {}
    out: dict[str, Any] = {}
    rid = result.get("id")
    if isinstance(rid, str):
        out["gen_ai.response.id"] = rid
    rmodel = result.get("model")
    if isinstance(rmodel, str):
        out["gen_ai.response.model"] = rmodel

    # finish_reasons: array per the conventions (one entry per choice).
    # Aitelier's done envelope has a single `finish_reason` string at
    # the top level; OpenAI-shape responses have `choices[*].finish_reason`.
    finish_reasons: list[str] = []
    top = result.get("finish_reason")
    if isinstance(top, str):
        finish_reasons.append(top)
    choices = result.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if isinstance(c, dict):
                fr = c.get("finish_reason")
                if isinstance(fr, str) and fr not in finish_reasons:
                    finish_reasons.append(fr)
    if finish_reasons:
        out["gen_ai.response.finish_reasons"] = tuple(finish_reasons)

    usage = result.get("usage")
    if isinstance(usage, dict):
        # Accept both OpenAI (prompt_tokens/completion_tokens) and
        # aitelier's internal (input_tokens/output_tokens) shape.
        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
        )
        if isinstance(input_tokens, int):
            out["gen_ai.usage.input_tokens"] = input_tokens
        if isinstance(output_tokens, int):
            out["gen_ai.usage.output_tokens"] = output_tokens

    return out


# ---------------------------------------------------------------------------
# Span emission helpers — call sites use these so each instrumented path
# is a one-liner. Honors `[otel] capture_content` for the optional
# message-content events.
# ---------------------------------------------------------------------------


async def record_run_trace(
    *,
    run_id: str,
    operation: str,
    request_body: dict | None,
    result: dict | None,
    error_type: str | None = None,
    error_msg: str | None = None,
) -> None:
    """Emit the OTLP span tree for one finalized run and return.

    Reads the run's timestamps + durable `run_events` from the store (the
    single source of truth) and hands them to `record_inference_span`,
    which builds a root span carrying the gen_ai.* attributes plus a child
    span per tool call. The trace id IS the run id, so the run is
    addressable by id in any OTLP backend. Reconstructed at finalize —
    fully off the hot path. No-op (and one cheap `_tracer is None` check)
    when OTel is disabled, so a default deployment pays nothing."""
    if _tracer is None:
        return
    try:
        from aitelier.storage import get_store
        store = await get_store()
        run = await store.get_run(run_id)
        events = await store.list_events(run_id, limit=5000) if run else []
        record_inference_span(
            operation=operation, request_body=request_body, result=result,
            run_id=run_id, events=events,
            started_at=run.started_at if run else None,
            ended_at=run.ended_at if run else None,
            error_type=error_type, error_msg=error_msg,
        )
    except Exception as exc:
        logger.warning(
            "OTel run-trace emission failed (%s: %s) — request unaffected.",
            type(exc).__name__, exc,
        )


def record_inference_span(
    *,
    operation: str,
    request_body: dict | None,
    result: dict | None,
    run_id: str | None = None,
    events: list | None = None,
    started_at=None,
    ended_at=None,
    error_type: str | None = None,
    error_msg: str | None = None,
) -> None:
    """Emit a gen_ai.* root span for one inference call, plus a child span
    per surfaced `run_event` (tool calls), reconstructing the agent's step
    tree. When `run_id` is a 32-hex value the span tree's trace id IS the
    run id, so the run is addressable by id in any OTLP backend.

    `operation` is the OTel `gen_ai.operation.name` ("chat" or
    "embeddings"). With no `events`/`run_id`/timestamps this degrades to a
    single live-timed span (used by the unit tests). No-op when OTel isn't
    enabled.
    """
    if _tracer is None:
        return
    tracer = _tracer

    # Best-effort: a bug in the SDK (bad attribute value, exporter refused
    # at span end) must not propagate into the request path — the run is
    # already durably recorded. Surface OTel breakage as a warning, not a 500.
    try:
        from opentelemetry import trace as _trace
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )

        # Seed the trace id from the run id via a phantom parent context, so
        # the root span and its children all share trace_id == run_id.
        parent_ctx = None
        trace_id = _trace_id_from_run_id(run_id)
        if trace_id is not None:
            phantom = SpanContext(
                trace_id=trace_id, span_id=_derive_span_id(run_id, "root-parent"),
                is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            parent_ctx = set_span_in_context(NonRecordingSpan(phantom))

        span_name = (
            f"{operation} {request_body.get('model')}"
            if isinstance(request_body, dict) and request_body.get("model")
            else operation
        )
        root = tracer.start_span(
            span_name, context=parent_ctx, start_time=_to_ns(started_at),
        )
        try:
            root.set_attribute("gen_ai.operation.name", operation)
            for k, v in gen_ai_request_attrs(request_body).items():
                root.set_attribute(k, v)
            for k, v in gen_ai_response_attrs(result).items():
                root.set_attribute(k, v)

            from aitelier.config import get_config
            if get_config().otel.capture_content and operation == "chat":
                _emit_message_events(root, request_body, result)

            if error_type:
                root.set_status(_trace.Status(_trace.StatusCode.ERROR,
                                              description=error_msg or error_type))
                root.set_attribute("error.type", error_type)

            if events:
                _emit_event_child_spans(tracer, set_span_in_context(root), events)
        finally:
            root.end(end_time=_to_ns(ended_at))
    except Exception as exc:
        logger.warning(
            "OTel span emission failed (%s: %s) — request unaffected.",
            type(exc).__name__, exc,
        )


def _trace_id_from_run_id(run_id: str | None) -> int | None:
    """A run id is a 32-hex W3C trace id → use it verbatim as the OTLP trace
    id. None for pre-hex (legacy) ids, letting the SDK generate one."""
    if isinstance(run_id, str) and len(run_id) == 32:
        try:
            value = int(run_id, 16)
            return value or None  # all-zero trace ids are invalid
        except ValueError:
            return None
    return None


def _derive_span_id(run_id: str, salt: str) -> int:
    """Deterministic non-zero 64-bit span id from (run_id, salt) so a
    re-export of the same run reproduces the same span ids (idempotent)."""
    import hashlib
    digest = hashlib.sha256(f"{run_id}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") or 1


def _to_ns(dt) -> int | None:
    """Datetime → epoch nanoseconds for OTel start/end times. None → the SDK
    falls back to the current time."""
    return int(dt.timestamp() * 1_000_000_000) if dt is not None else None


def _emit_event_child_spans(tracer, parent_ctx, events) -> None:
    """One child `execute_tool` span per tool invocation (per the GenAI
    conventions), spanning the tool's wall-clock. start/finish/delta/thought/
    error frame the root span and aren't re-emitted as children.

    Pairs each `tool_call` with its `tool_result`(s) by the ACP `toolCallId`
    when present — ACP fires several result pings per call, so adjacency
    mispairs. The span runs from the call's ts to its LAST result's ts.
    Falls back to call→next-result adjacency for id-less events (runs
    recorded before the id was captured)."""
    calls = [e for e in events if getattr(e, "kind", None) == "tool_call"]
    results = [e for e in events if getattr(e, "kind", None) == "tool_result"]
    if not calls:
        return

    def _id(ev):
        return (getattr(ev, "payload", None) or {}).get("id")

    have_ids = all(_id(c) for c in calls) and any(_id(r) for r in results)
    if have_ids:
        last_result: dict = {}
        for r in results:
            rid = _id(r)
            if rid is not None:
                last_result[rid] = r  # events are seq-ordered → last wins
        for call in calls:
            end_ev = last_result.get(_id(call))
            _emit_tool_span(tracer, parent_ctx, call, end_ev)
        return

    # Adjacency fallback: each call closes on the next result.
    open_call = None
    for ev in events:
        kind = getattr(ev, "kind", None)
        if kind == "tool_call":
            open_call = ev
        elif kind == "tool_result" and open_call is not None:
            _emit_tool_span(tracer, parent_ctx, open_call, ev)
            open_call = None


def _emit_tool_span(tracer, parent_ctx, call, end_ev) -> None:
    """Emit one execute_tool span from a tool_call event to its terminal
    result event (None → span has just the call's start time)."""
    cp = getattr(call, "payload", None) or {}
    name = cp.get("tool") or "tool"
    child = tracer.start_span(
        f"execute_tool {name}", context=parent_ctx,
        start_time=_to_ns(getattr(call, "ts", None)),
    )
    try:
        child.set_attribute("gen_ai.operation.name", "execute_tool")
        child.set_attribute("gen_ai.tool.name", str(name))
        if cp.get("server"):
            child.set_attribute("gen_ai.tool.server", str(cp["server"]))
        if cp.get("id"):
            child.set_attribute("gen_ai.tool.call.id", str(cp["id"]))
        rp = getattr(end_ev, "payload", None) or {}
        if rp.get("elapsed_ms") is not None:
            child.set_attribute("aitelier.tool.elapsed_ms", rp["elapsed_ms"])
    finally:
        child.end(end_time=_to_ns(getattr(end_ev, "ts", None)))


def _emit_message_events(span: Any, request_body: dict | None,
                         result: dict | None) -> None:
    """Emit per-role events with content. Conforms to the GenAI semantic
    conventions' event names; backends like Langfuse render these as
    the conversation transcript."""
    if not isinstance(request_body, dict):
        return
    messages = request_body.get("messages")
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if role not in ("user", "system", "assistant", "tool"):
                continue
            event_name = f"gen_ai.{role}.message"
            attrs: dict[str, Any] = {}
            if content is not None:
                attrs["content"] = (
                    content if isinstance(content, str) else str(content)
                )
            span.add_event(event_name, attributes=attrs)

    # Final choice event — what the model returned.
    if isinstance(result, dict):
        choices = result.get("choices")
        if isinstance(choices, list):
            for idx, c in enumerate(choices):
                if not isinstance(c, dict):
                    continue
                msg = c.get("message") or {}
                attrs = {
                    "index": idx,
                    "finish_reason": c.get("finish_reason") or "",
                }
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if content is not None:
                        attrs["content"] = (
                            content if isinstance(content, str) else str(content)
                        )
                span.add_event("gen_ai.choice", attributes=attrs)
