"""Tests for the OTel GenAI attribute mapping helpers.

The helpers in `aitelier.otel` are split deliberately: the attribute
builders (`gen_ai_request_attrs`, `gen_ai_response_attrs`,
`gen_ai_system_for_model`) are pure functions that DON'T import the OTel
SDK, so they work on a default install without the `[otel]` extra. Span
emission goes through `record_inference_span` which is a no-op when
`_tracer` is None (i.e., `init_tracer_provider` hasn't been called).

Pure-function tests at the top of this file run without the SDK
installed; integration tests below use `pytest.importorskip` and an
`InMemorySpanExporter` to exercise the SDK-bound surface.
"""

from __future__ import annotations

from aitelier.otel import (
    gen_ai_request_attrs,
    gen_ai_response_attrs,
    gen_ai_system_for_model,
    record_inference_span,
)

# --- gen_ai_system_for_model ------------------------------------------------


def test_system_maps_known_prefixes_to_canonical_names():
    """OTel's GenAI conventions enumerate provider names (anthropic,
    openai, gemini, ollama, …). We map aitelier's model-prefix routing
    to those so trace backends can filter cleanly."""
    cases = [
        ("claude-sonnet-4-5", "anthropic"),
        ("anthropic/claude-haiku", "anthropic"),
        ("openai/gpt-4o", "openai"),
        ("gpt-4-turbo", "openai"),
        ("o1-preview", "openai"),
        ("o3-mini", "openai"),
        ("gemini-2.5-pro", "gemini"),
        ("ollama/qwen3:8b", "ollama"),
        # `local` is aitelier's curated alias that resolves to Ollama.
        ("local", "ollama"),
    ]
    for model, expected in cases:
        assert gen_ai_system_for_model(model) == expected, (
            f"model={model!r} mapped wrong"
        )


def test_system_maps_agent_models_to_aitelier_namespace():
    """`agent:<backend>` doesn't fit OTel's registered system list.
    Use a custom `aitelier.agent.<backend>` namespace so trace backends
    can still group / filter on it."""
    assert gen_ai_system_for_model("agent:claude/claude-sonnet-4-5") == "aitelier.agent.claude"
    assert gen_ai_system_for_model("agent:codex/gpt-4o") == "aitelier.agent.codex"
    assert gen_ai_system_for_model("agent:opencode/grok-code") == "aitelier.agent.opencode"


def test_system_unknown_maps_to_other_placeholder():
    """The convention's escape hatch when no canonical name applies."""
    assert gen_ai_system_for_model("some-future-model-v9") == "_OTHER"
    assert gen_ai_system_for_model("") == "_OTHER"
    assert gen_ai_system_for_model(None) == "_OTHER"  # type: ignore[arg-type]


# --- gen_ai_request_attrs ---------------------------------------------------


def test_request_attrs_populates_model_and_system():
    out = gen_ai_request_attrs({
        "model": "claude-haiku",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert out["gen_ai.request.model"] == "claude-haiku"
    assert out["gen_ai.system"] == "anthropic"


def test_request_attrs_prefers_max_completion_tokens_over_max_tokens():
    """Same precedence as `_build_ollama_request` — OpenAI's reasoning-
    model field wins. The convention has one slot (`gen_ai.request.max_tokens`)."""
    out = gen_ai_request_attrs({
        "model": "gpt-4o",
        "max_tokens": 50,
        "max_completion_tokens": 500,
    })
    assert out["gen_ai.request.max_tokens"] == 500


def test_request_attrs_falls_back_to_max_tokens():
    out = gen_ai_request_attrs({
        "model": "gpt-4o",
        "max_tokens": 50,
    })
    assert out["gen_ai.request.max_tokens"] == 50


def test_request_attrs_sets_temperature_and_top_p():
    out = gen_ai_request_attrs({
        "model": "claude-haiku",
        "temperature": 0.7,
        "top_p": 0.9,
    })
    assert out["gen_ai.request.temperature"] == 0.7
    assert out["gen_ai.request.top_p"] == 0.9


def test_request_attrs_sets_frequency_and_presence_penalty():
    """Both penalty fields ride the same float coercion path as temperature
    / top_p — covered so the docstring's listed conventions stay honest."""
    out = gen_ai_request_attrs({
        "model": "gpt-4o",
        "frequency_penalty": 0.5,
        "presence_penalty": -0.25,
    })
    assert out["gen_ai.request.frequency_penalty"] == 0.5
    assert out["gen_ai.request.presence_penalty"] == -0.25


def test_request_attrs_stop_sequences_string_and_array():
    """OpenAI accepts both a single string and an array; both must
    render as a tuple of strings (the convention specifies array)."""
    one = gen_ai_request_attrs({"model": "x", "stop": "END"})
    assert one["gen_ai.request.stop_sequences"] == ("END",)

    many = gen_ai_request_attrs({"model": "x", "stop": ["END", "\\n\\n"]})
    assert many["gen_ai.request.stop_sequences"] == ("END", "\\n\\n")


def test_request_attrs_tolerates_missing_fields():
    """Partial bodies (no temperature, no top_p) just omit the missing
    keys instead of setting None."""
    out = gen_ai_request_attrs({"model": "claude-haiku"})
    assert "gen_ai.request.temperature" not in out
    assert "gen_ai.request.top_p" not in out
    assert "gen_ai.request.max_tokens" not in out


def test_request_attrs_empty_input_returns_empty_dict():
    assert gen_ai_request_attrs(None) == {}
    assert gen_ai_request_attrs({}) == {}
    assert gen_ai_request_attrs("not a dict") == {}  # type: ignore[arg-type]


# --- gen_ai_response_attrs --------------------------------------------------


def test_response_attrs_openai_shape():
    """Standard OpenAI chat-completion shape — id, model, finish_reason
    on the single choice, usage in prompt_tokens / completion_tokens."""
    out = gen_ai_response_attrs({
        "id": "chatcmpl-abc",
        "model": "gpt-4o-2024-08-06",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    })
    assert out["gen_ai.response.id"] == "chatcmpl-abc"
    assert out["gen_ai.response.model"] == "gpt-4o-2024-08-06"
    assert out["gen_ai.response.finish_reasons"] == ("stop",)
    assert out["gen_ai.usage.input_tokens"] == 12
    assert out["gen_ai.usage.output_tokens"] == 5


def test_response_attrs_agent_done_shape():
    """Aitelier's agent path emits a different shape — top-level
    finish_reason, usage in input_tokens / output_tokens (aitelier's
    internal naming). The helper accepts both."""
    out = gen_ai_response_attrs({
        "finish_reason": "stop",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    assert out["gen_ai.response.finish_reasons"] == ("stop",)
    assert out["gen_ai.usage.input_tokens"] == 100
    assert out["gen_ai.usage.output_tokens"] == 50


def test_response_attrs_dedupes_finish_reasons():
    """Multiple choices with the same finish_reason should not duplicate."""
    out = gen_ai_response_attrs({
        "choices": [
            {"finish_reason": "stop"},
            {"finish_reason": "stop"},
            {"finish_reason": "length"},
        ],
    })
    assert out["gen_ai.response.finish_reasons"] == ("stop", "length")


def test_response_attrs_tolerates_missing_or_empty_input():
    assert gen_ai_response_attrs(None) == {}
    assert gen_ai_response_attrs({}) == {}
    assert gen_ai_response_attrs("not a dict") == {}  # type: ignore[arg-type]


# --- record_inference_span no-op when OTel disabled -------------------------


def test_record_inference_span_noop_when_tracer_unset():
    """When `[otel] enabled = false` (default) and `init_tracer_provider`
    hasn't been called, `record_inference_span` must be a silent no-op.
    Call sites depend on this for the zero-cost-by-default contract."""
    # Should not raise. Anything that exercises the OTel SDK would
    # ImportError on a default install.
    record_inference_span(
        operation="chat",
        request_body={"model": "gpt-4o", "messages": []},
        result={"id": "x", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )


# --- Span emission against a real tracer ------------------------------------
#
# These tests require the [otel] / dev extras (opentelemetry-sdk). The
# SDK ships an `InMemorySpanExporter` we can hook in instead of OTLP, so
# we get the emitted span back as a Python object and can assert on its
# attributes / status / events without running a collector.

import pytest  # noqa: E402

pytest.importorskip("opentelemetry.sdk.trace")  # noqa: E402

from contextlib import contextmanager  # noqa: E402

from aitelier import otel as _otel_module  # noqa: E402
from aitelier.config import (  # noqa: E402
    Config,
    OtelConfig,
    get_config,
    reset_config,
    set_config,
)


@contextmanager
def _capturing_tracer(*, capture_content: bool = False):
    """Install an InMemorySpanExporter-backed tracer, yield the exporter,
    restore on exit. Bypasses `init_tracer_provider` (which would try to
    set the global TracerProvider and conflict across tests) and writes
    directly to `_otel_module._tracer`."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    prev_tracer = _otel_module._tracer
    _otel_module._tracer = provider.get_tracer("aitelier-test", "0.0.0")

    # The content path reads get_config().otel.capture_content; install a
    # config override so we can toggle it per test without polluting the
    # singleton.
    prev_cfg = get_config()
    set_config(Config(
        litellm=prev_cfg.litellm, sandbox_agent=prev_cfg.sandbox_agent,
        service=prev_cfg.service, ollama=prev_cfg.ollama,
        database=prev_cfg.database, storage=prev_cfg.storage,
        purge=prev_cfg.purge,
        otel=OtelConfig(enabled=True, capture_content=capture_content),
        runs_dir=prev_cfg.runs_dir,
    ))
    try:
        yield exporter
    finally:
        _otel_module._tracer = prev_tracer
        reset_config()
        provider.shutdown()


def test_record_span_emits_with_request_and_response_attrs():
    """Happy path: tracer set, span lands with gen_ai.* attributes
    derived from both request and response."""
    with _capturing_tracer() as exporter:
        record_inference_span(
            operation="chat",
            request_body={
                "model": "gpt-4o", "temperature": 0.5,
                "messages": [{"role": "user", "content": "hi"}],
            },
            result={
                "id": "chatcmpl-x", "model": "gpt-4o-2024-08-06",
                "choices": [{"finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    # Span name should include the model so trace backends can group.
    assert span.name == "chat gpt-4o"
    attrs = dict(span.attributes)
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.request.temperature"] == 0.5
    assert attrs["gen_ai.response.id"] == "chatcmpl-x"
    assert attrs["gen_ai.response.model"] == "gpt-4o-2024-08-06"
    assert attrs["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs["gen_ai.usage.input_tokens"] == 12
    assert attrs["gen_ai.usage.output_tokens"] == 5


def test_record_span_falls_back_to_operation_name_when_model_missing():
    """When request_body has no model, span name is just the operation."""
    with _capturing_tracer() as exporter:
        record_inference_span(
            operation="embeddings",
            request_body={"input": ["a", "b"]},
            result={"data": []},
        )
    span = exporter.get_finished_spans()[0]
    assert span.name == "embeddings"
    assert dict(span.attributes)["gen_ai.operation.name"] == "embeddings"


def test_record_span_marks_error_status_and_attribute():
    """Error path: error_type + error_msg flow into the span as
    error.type + a non-OK span status. Trace backends filter on these
    for failure rate dashboards."""
    from opentelemetry.trace import StatusCode

    with _capturing_tracer() as exporter:
        record_inference_span(
            operation="chat",
            request_body={"model": "gpt-4o"},
            result=None,
            error_type="RateLimited",
            error_msg="429 from upstream",
        )
    span = exporter.get_finished_spans()[0]
    assert dict(span.attributes)["error.type"] == "RateLimited"
    assert span.status.status_code == StatusCode.ERROR
    assert "429 from upstream" in (span.status.description or "")


def test_record_span_tree_uses_run_id_as_trace_id_and_emits_tool_children():
    """run_id (32-hex) becomes the trace id; tool_call → tool_result events
    become child spans under the root, reconstructing the agent step tree."""
    from datetime import UTC, datetime, timedelta

    from aitelier.storage.models import RunEvent

    rid = "abcdef0123456789abcdef0123456789"
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # ACP fires multiple result pings per call; pairing is by toolCallId, not
    # adjacency — so this still yields exactly ONE span for the call.
    events = [
        RunEvent(run_id=rid, seq=1, kind="start", payload={}, ts=t0),
        RunEvent(run_id=rid, seq=2, kind="tool_call",
                 payload={"id": "tc-1", "tool": "Read", "server": "fs"},
                 ts=t0 + timedelta(seconds=1)),
        RunEvent(run_id=rid, seq=3, kind="tool_result",
                 payload={"id": "tc-1", "tool": None},
                 ts=t0 + timedelta(seconds=1, milliseconds=500)),
        RunEvent(run_id=rid, seq=4, kind="tool_result",
                 payload={"id": "tc-1", "elapsed_ms": 120}, ts=t0 + timedelta(seconds=2)),
        RunEvent(run_id=rid, seq=5, kind="finish", payload={}, ts=t0 + timedelta(seconds=3)),
    ]
    with _capturing_tracer() as exporter:
        record_inference_span(
            operation="chat",
            request_body={"model": "agent:claude/claude-sonnet"},
            result={"usage": {"input_tokens": 10, "output_tokens": 5}},
            run_id=rid, events=events,
            started_at=t0, ended_at=t0 + timedelta(seconds=3),
        )
    spans = exporter.get_finished_spans()
    assert len(spans) == 2  # root + one tool span (start/finish/delta aren't spans)
    by_name = {s.name: s for s in spans}
    root = by_name["chat agent:claude/claude-sonnet"]
    tool = by_name["execute_tool Read"]
    # Both share the trace id derived verbatim from run_id.
    assert format(root.context.trace_id, "032x") == rid
    assert format(tool.context.trace_id, "032x") == rid
    # Tool span nests under the root.
    assert tool.parent.span_id == root.context.span_id
    assert dict(tool.attributes)["gen_ai.tool.name"] == "Read"


def test_record_span_content_off_by_default_emits_no_message_events():
    """Default (capture_content=false): no per-message events on the
    span. Protects against accidentally exporting PII/secrets."""
    with _capturing_tracer(capture_content=False) as exporter:
        record_inference_span(
            operation="chat",
            request_body={
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
            result={"choices": [{"message": {"role": "assistant",
                                              "content": "hello"},
                                  "finish_reason": "stop"}]},
        )
    span = exporter.get_finished_spans()[0]
    assert span.events == ()


def test_record_span_content_on_emits_per_role_events():
    """capture_content=true: one event per request message + per choice."""
    with _capturing_tracer(capture_content=True) as exporter:
        record_inference_span(
            operation="chat",
            request_body={
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
            result={"choices": [{"message": {"role": "assistant",
                                              "content": "hello"},
                                  "finish_reason": "stop"}]},
        )
    span = exporter.get_finished_spans()[0]
    event_names = [e.name for e in span.events]
    assert "gen_ai.system.message" in event_names
    assert "gen_ai.user.message" in event_names
    assert "gen_ai.choice" in event_names
    user_event = next(e for e in span.events if e.name == "gen_ai.user.message")
    assert dict(user_event.attributes)["content"] == "hi"


def test_record_span_content_skipped_for_embeddings_even_when_on():
    """Embeddings have no message structure — content events would be
    meaningless. capture_content gates message emission on chat only."""
    with _capturing_tracer(capture_content=True) as exporter:
        record_inference_span(
            operation="embeddings",
            request_body={"model": "nomic-embed-text", "input": ["hi"]},
            result={"data": [{"embedding": [0.1, 0.2]}]},
        )
    span = exporter.get_finished_spans()[0]
    assert span.events == ()


# --- init_tracer_provider state transitions ---------------------------------


def test_init_tracer_provider_noop_when_disabled():
    """With [otel] disabled, init must leave _tracer None. Sets the config
    explicitly rather than relying on the on-disk aitelier.toml — that file
    is machine-local/gitignored and may enable otel for a real deployment."""
    # Save existing state; the conftest doesn't reset _tracer between tests.
    prev_tracer = _otel_module._tracer
    prev_cfg = get_config()
    _otel_module._tracer = None
    set_config(Config(otel=OtelConfig(enabled=False)))
    try:
        _otel_module.init_tracer_provider()
        assert _otel_module._tracer is None
    finally:
        _otel_module._tracer = prev_tracer
        set_config(prev_cfg)


def test_otel_config_default_is_disabled():
    """The dataclass default is disabled — a default install pays no OTel
    import cost and emits nothing until an operator opts in."""
    assert OtelConfig().enabled is False


def test_init_tracer_provider_is_idempotent():
    """Second call when _tracer is already set must be a no-op (must not
    register a second BatchSpanProcessor or call set_tracer_provider
    again — global state pollution would surface as duplicated spans)."""
    prev_tracer = _otel_module._tracer
    sentinel = object()
    _otel_module._tracer = sentinel
    prev_cfg = get_config()
    set_config(Config(
        litellm=prev_cfg.litellm, sandbox_agent=prev_cfg.sandbox_agent,
        service=prev_cfg.service, ollama=prev_cfg.ollama,
        database=prev_cfg.database, storage=prev_cfg.storage,
        purge=prev_cfg.purge,
        otel=OtelConfig(enabled=True),
        runs_dir=prev_cfg.runs_dir,
    ))
    try:
        _otel_module.init_tracer_provider()
        # _tracer must still be our sentinel — the function returned early.
        assert _otel_module._tracer is sentinel
    finally:
        _otel_module._tracer = prev_tracer
        reset_config()


def test_shutdown_tracer_provider_noop_when_uninitialized():
    """When _tracer is None, shutdown must not raise — the lifespan calls
    it unconditionally on shutdown."""
    prev_tracer = _otel_module._tracer
    _otel_module._tracer = None
    try:
        _otel_module.shutdown_tracer_provider()  # must not raise
        assert _otel_module._tracer is None
    finally:
        _otel_module._tracer = prev_tracer


def test_init_tracer_provider_graceful_when_sdk_missing(caplog, monkeypatch):
    """When `[otel] enabled = true` but the SDK isn't installed, init
    must log a clear WARNING and leave `_tracer` None — the request
    path then keeps no-op'ing instead of ImportError-ing on first call.

    Simulated by stubbing the lazy `opentelemetry.sdk.trace` import
    with `None` in `sys.modules` — Python raises ImportError on
    submodule access, which the function catches."""
    import sys

    prev_tracer = _otel_module._tracer
    _otel_module._tracer = None
    prev_cfg = get_config()
    set_config(Config(
        litellm=prev_cfg.litellm, sandbox_agent=prev_cfg.sandbox_agent,
        service=prev_cfg.service, ollama=prev_cfg.ollama,
        database=prev_cfg.database, storage=prev_cfg.storage,
        purge=prev_cfg.purge,
        otel=OtelConfig(enabled=True),
        runs_dir=prev_cfg.runs_dir,
    ))

    # Pre-bind the names the function imports lazily; `sys.modules[k] =
    # None` makes Python raise ModuleNotFoundError on `import k`.
    saved: dict[str, object] = {}
    targets = [
        "opentelemetry",
        "opentelemetry.sdk",
        "opentelemetry.sdk.resources",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
    ]
    for name in targets:
        saved[name] = sys.modules.get(name)
        sys.modules[name] = None  # type: ignore[assignment]
    try:
        import logging
        with caplog.at_level(logging.WARNING, logger="aitelier.otel"):
            _otel_module.init_tracer_provider()
        assert _otel_module._tracer is None
        # Operator-actionable message: must name the install command so
        # they can fix without reading source.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("uv pip install aitelier[otel]" in r.getMessage()
                   for r in warnings), (
            f"missing install hint in warnings: {[r.getMessage() for r in warnings]}"
        )
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        _otel_module._tracer = prev_tracer
        reset_config()


# --- OtelConfig TOML loading -------------------------------------------------


def test_otel_config_loads_from_toml(tmp_path):
    """The `[otel]` section in aitelier.toml must hydrate OtelConfig.
    A regression here would silently disable OTel for operators who
    enabled it in config (worst-case: trust without verification)."""
    cfg_path = tmp_path / "aitelier.toml"
    cfg_path.write_text(
        '[otel]\n'
        'enabled = true\n'
        'endpoint = "http://collector:4317"\n'
        'protocol = "http"\n'
        'insecure = false\n'
        'service_name = "my-service"\n'
        'capture_content = true\n'
    )
    from aitelier.config import load_config
    cfg = load_config(cfg_path)
    assert cfg.otel.enabled is True
    assert cfg.otel.endpoint == "http://collector:4317"
    assert cfg.otel.protocol == "http"
    assert cfg.otel.insecure is False
    assert cfg.otel.service_name == "my-service"
    assert cfg.otel.capture_content is True


def test_otel_config_defaults_when_section_absent(tmp_path):
    """No `[otel]` section → defaults; enabled stays false so a default
    install pays zero observability cost."""
    cfg_path = tmp_path / "aitelier.toml"
    cfg_path.write_text('[service]\nport = 7777\n')
    from aitelier.config import load_config
    cfg = load_config(cfg_path)
    assert cfg.otel.enabled is False
    assert cfg.otel.protocol == "grpc"
    assert cfg.otel.capture_content is False


# --- _build_exporter protocol selection -------------------------------------


def test_build_exporter_grpc_default():
    """`protocol = "grpc"` (the default) wires the gRPC OTLPSpanExporter
    against the canonical port (4317)."""
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcExporter,
    )
    cfg = OtelConfig(enabled=True, endpoint="http://localhost:4317",
                     protocol="grpc", insecure=True)
    exporter = _otel_module._build_exporter(cfg)
    assert isinstance(exporter, GrpcExporter)


def test_build_exporter_http_when_protocol_http():
    """`protocol = "http"` picks the HTTP OTLPSpanExporter (port 4318).
    Different class; same wire format (OTLP/protobuf)."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpExporter,
    )
    cfg = OtelConfig(enabled=True, endpoint="http://localhost:4318",
                     protocol="http", insecure=True)
    exporter = _otel_module._build_exporter(cfg)
    assert isinstance(exporter, HttpExporter)


# --- End-to-end: HTTP endpoints actually invoke record_inference_span -------
#
# These tests drive a TestClient through the real FastAPI app with the
# OTel tracer set to an InMemorySpanExporter. They prove the call sites
# in server.py / endpoints/inference.py are wired — a regression that
# removes the `record_inference_span(...)` line would show up here as
# "no spans emitted".

from unittest.mock import AsyncMock, patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


@contextmanager
def _tracer_and_client():
    """Install the in-memory tracer and yield (TestClient, exporter).
    Mirrors `_capturing_tracer` but builds a TestClient against the real
    app so HTTP integration is exercised end-to-end."""
    from aitelier.server import app
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    prev = _otel_module._tracer
    _otel_module._tracer = provider.get_tracer("aitelier-test", "0.0.0")
    client = TestClient(app)
    try:
        yield client, exporter
    finally:
        _otel_module._tracer = prev
        provider.shutdown()


def _openai_chat_response() -> dict:
    return {
        "id": "chatcmpl-x", "object": "chat.completion",
        "created": 1_700_000_000, "model": "claude-sonnet",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def test_llm_chat_completions_endpoint_emits_span():
    """POST /v1/chat/completions on the LLM path (non-agent model) must
    invoke `record_inference_span` after the run is recorded. Proves the
    call site at the end of `_llm_chat_completion` is wired."""
    with _tracer_and_client() as (client, exporter):
        with patch("aitelier.inference_exec.chat_completion",
                    new_callable=AsyncMock,
                    return_value=_openai_chat_response()):
            resp = client.post("/v1/chat/completions", json={
                "model": "claude-sonnet",
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert resp.status_code == 200, resp.text
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "claude-sonnet"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.response.id"] == "chatcmpl-x"
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 3


def test_embeddings_endpoint_emits_span():
    """POST /v1/embeddings must emit a `gen_ai.operation.name = embeddings`
    span. Proves the call site in `endpoints/inference.py:embeddings_endpoint`
    is wired."""
    fake = {
        "object": "list",
        "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
        "model": "nomic-embed-text",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }
    with _tracer_and_client() as (client, exporter):
        with patch("aitelier.endpoints.inference.embeddings",
                    new_callable=AsyncMock, return_value=fake):
            resp = client.post("/v1/embeddings", json={
                "model": "nomic-embed-text",
                "input": "hello",
            })
        assert resp.status_code == 200, resp.text
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["gen_ai.operation.name"] == "embeddings"
    assert attrs["gen_ai.request.model"] == "nomic-embed-text"


def test_agent_chat_completions_endpoint_emits_span(monkeypatch):
    """POST /v1/chat/completions with `model = "agent:<backend>"` runs the
    agent path. Proves the call site at the end of `_agent_chat_completion`
    is wired and that the system attribute uses the
    `aitelier.agent.<backend>` namespace."""
    async def fake_prepare(prepare):
        return {"commands": [], "files": [], "sidecars": []}

    async def fake_call(name, prompt, **kw):
        return {
            "kind": "agent", "status": "ok", "provider": name,
            "content": "done", "finish_reason": "stop",
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            "run_id": kw.get("run_id"), "trace_id": kw.get("run_id"),
            "tool_calls": [], "cost_usd": None,
            "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr("aitelier.inference_exec._run_prepare", fake_prepare)
    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call,
    )

    with _tracer_and_client() as (client, exporter):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude/claude-sonnet-4-5",
            "messages": [{"role": "user", "content": "audit"}],
        })
        assert resp.status_code == 200, resp.text
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "agent:claude/claude-sonnet-4-5"
    assert attrs["gen_ai.system"] == "aitelier.agent.claude"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7


def test_llm_chat_completions_stream_endpoint_emits_span():
    """Streaming LLM path: span must fire after the stream finishes
    (inside `event_generator`'s `finally`). Token counts on the span
    reflect what the synthetic terminal chunk reported."""
    async def fake_stream(body, *, timeout):
        yield {"choices": [{"index": 0, "delta": {"content": "Hi"},
                            "finish_reason": None}]}
        yield {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1,
                      "total_tokens": 5},
        }

    with _tracer_and_client() as (client, exporter):
        with patch("aitelier.inference_exec.chat_completion_stream", fake_stream):
            resp = client.post("/v1/chat/completions", json={
                "model": "claude-sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            })
            # Drain so the generator's finally block runs.
            assert resp.status_code == 200, resp.text
            _ = resp.text
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "claude-sonnet"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs["gen_ai.usage.input_tokens"] == 4
    assert attrs["gen_ai.usage.output_tokens"] == 1


def test_record_inference_span_swallows_sdk_failures(caplog):
    """Best-effort guard: if span emission raises (bad attr, exporter
    bug), the function must catch, log, and return — never propagate
    into the request path. Validated by installing a tracer whose
    `start_span` raises."""
    import logging
    from unittest.mock import MagicMock

    bad_tracer = MagicMock()
    bad_tracer.start_span.side_effect = RuntimeError("simulated SDK bug")

    prev_tracer = _otel_module._tracer
    _otel_module._tracer = bad_tracer
    try:
        with caplog.at_level(logging.WARNING, logger="aitelier.otel"):
            # Must not raise.
            record_inference_span(
                operation="chat",
                request_body={"model": "gpt-4o"},
                result={"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            )
        assert any("OTel span emission failed" in r.getMessage()
                   for r in caplog.records)
    finally:
        _otel_module._tracer = prev_tracer


def test_lifespan_initializes_and_shuts_down_tracer_when_enabled(monkeypatch):
    """`with TestClient(app) as c:` triggers FastAPI's lifespan
    startup/shutdown, which calls `init_tracer_provider` /
    `shutdown_tracer_provider`. With `[otel] enabled = true`, the
    tracer must be set during the request window and cleared on exit.

    Other lifespan dependencies (LiteLLM health probe, Postgres pool)
    are not stubbed — this test runs against the same fixtures the
    other test_server.py tests run against."""
    from aitelier.server import app

    prev_tracer = _otel_module._tracer
    _otel_module._tracer = None
    prev_cfg = get_config()
    set_config(Config(
        litellm=prev_cfg.litellm, sandbox_agent=prev_cfg.sandbox_agent,
        service=prev_cfg.service, ollama=prev_cfg.ollama,
        database=prev_cfg.database, storage=prev_cfg.storage,
        purge=prev_cfg.purge,
        otel=OtelConfig(enabled=True, protocol="grpc",
                        endpoint="http://127.0.0.1:4317", insecure=True,
                        service_name="lifespan-test"),
        runs_dir=prev_cfg.runs_dir,
    ))
    try:
        with TestClient(app):
            # Inside the lifespan window, the tracer must be set.
            assert _otel_module._tracer is not None, (
                "init_tracer_provider didn't run during lifespan startup"
            )
        # After lifespan exit, shutdown_tracer_provider has cleared it.
        assert _otel_module._tracer is None, (
            "shutdown_tracer_provider didn't clear _tracer on lifespan exit"
        )
    finally:
        _otel_module._tracer = prev_tracer
        reset_config()


def test_batch_span_processor_flushes_on_shutdown(monkeypatch):
    """Production uses `BatchSpanProcessor` (buffered). On a clean
    shutdown the buffer must flush so the last batch reaches the
    exporter.

    The OTel SDK's `set_tracer_provider` is one-shot (it silently
    refuses on second-set with a WARNING). Since other tests in this
    module may have already set a global provider, we can't reliably
    install ours globally. Instead we patch
    `trace.get_tracer_provider` so `shutdown_tracer_provider` finds
    *our* provider — which exercises the same code path that production
    would hit (init→set→get→shutdown), minus the one-shot wiring."""
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "flush-test"}))
    provider.add_span_processor(BatchSpanProcessor(exporter))

    monkeypatch.setattr(_trace, "get_tracer_provider", lambda: provider)

    prev_tracer = _otel_module._tracer
    _otel_module._tracer = provider.get_tracer("aitelier-flush", "0.0.0")
    try:
        record_inference_span(
            operation="chat",
            request_body={"model": "gpt-4o"},
            result={"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )
        # BatchSpanProcessor buffers; the in-memory exporter may have
        # nothing yet — that's the whole point of testing shutdown.
        # shutdown_tracer_provider must force the flush.
        _otel_module.shutdown_tracer_provider()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1, (
            f"BatchSpanProcessor didn't flush on shutdown — "
            f"got {len(spans)} spans"
        )
    finally:
        _otel_module._tracer = prev_tracer


def test_agent_chat_completions_stream_endpoint_emits_span(monkeypatch):
    """Streaming agent path finalizes in a detached task
    (`_finalize_stream_run`). The span emission lives there — this test
    proves it fires by waiting for the async finalize to complete."""
    async def fake_prepare(prepare):
        return {"commands": [], "files": [], "sidecars": []}

    async def fake_producer(queue, **kw):
        """Mimic the ACP event producer: push one delta then a done
        envelope to drive the SSE generator to its finally block."""
        await queue.put({
            "type": "delta",
            "delta": {"content": "hello"},
        })
        await queue.put({
            "type": "done",
            "finish_reason": "stop",
            "usage": {"input_tokens": 9, "output_tokens": 4,
                      "total_tokens": 13},
            "content": "hello",
        })
        # Sentinel signals end-of-stream.
        from aitelier.server import _STREAM_QUEUE_SENTINEL
        await queue.put(_STREAM_QUEUE_SENTINEL)

    monkeypatch.setattr("aitelier.inference_exec._run_prepare", fake_prepare)
    monkeypatch.setattr(
        "aitelier.inference_exec._producer_for_acp_stream", fake_producer,
    )

    import time as _time

    from aitelier.server import _pending_finalize_tasks

    with _tracer_and_client() as (client, exporter):
        resp = client.post("/v1/chat/completions", json={
            "model": "agent:claude/claude-sonnet-4-5",
            "messages": [{"role": "user", "content": "audit"}],
            "stream": True,
        })
        assert resp.status_code == 200, resp.text
        _ = resp.text  # drain SSE so the generator's finally runs
        # _finalize_stream_run is scheduled via asyncio.create_task on
        # TestClient's portal loop (a worker thread), so we can't
        # `gather` it from a fresh asyncio.run loop. Instead we poll on
        # production code's own self-signal: the task removes itself
        # from `_pending_finalize_tasks` via add_done_callback when it
        # completes. When the set is empty, finalize is durably done.
        deadline = _time.monotonic() + 2.0
        while _pending_finalize_tasks and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert not _pending_finalize_tasks, (
            "finalize task did not complete within 2s — possible deadlock"
        )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, (
        f"expected 1 span from agent stream, got {len(spans)}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "agent:claude/claude-sonnet-4-5"
    assert attrs["gen_ai.system"] == "aitelier.agent.claude"
    assert attrs["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs["gen_ai.usage.input_tokens"] == 9
    assert attrs["gen_ai.usage.output_tokens"] == 4
