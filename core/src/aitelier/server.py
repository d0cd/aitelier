"""FastAPI HTTP service for aitelier."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import resource
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from aitelier.config import get_config
from aitelier.errors import classify_error, scrub_error_text
from aitelier.inference_exec import (  # noqa: F401  (re-exported for endpoints)
    _STREAM_QUEUE_SENTINEL,
    _agent_chat_completion,
    _agent_chat_completion_stream,
    _finalize_stream_run,
    _fold_examples,
    _fold_response_format,
    _http_status_for_agent_error,
    _http_status_for_llm_error,
    _llm_body_from_request,
    _llm_chat_completion,
    _llm_chat_completion_stream,
    _producer_for_acp_stream,
    _reject_agent_incompatible_fields,
    _render_chat_completion,
    _replay_cached_stream,
    _stream_chunk_for_done,
    _stream_chunks_for_delta,
    _stream_error_payload,
    _stream_terminal_state,
    _synth_otel_result,
    _terminal_state_from_final,
    _translate_messages,
    _validate_aitelier_opts,
)
from aitelier.openai_compat import (
    ChatCompletionRequest,
    parse_model_route,
)
from aitelier.probes import (  # noqa: F401  (re-exported for endpoints)
    _normalize_agents_payload,
    _probe_litellm,
    _probe_sandbox_agent,
    _probe_traces,
    _sandbox_agents_request,
)
from aitelier.providers.llm import (
    model_response_format_capabilities,
)
from aitelier.runner import make_run_id
from aitelier.runs import _finalize_terminal
from aitelier.runtime import (  # noqa: F401  (re-exported for endpoints)
    _SSE_HEADERS,
    _SSE_KEEPALIVE_SECONDS,
    _active_runs,
    _cancelled_result,
    _check_webhook_url_or_die,
    _enqueue_webhook,
    _pending_finalize_tasks,
    _reject_if_saturated,
    _sse_event,
    _sse_response,
    _track_inflight_run,
)
from aitelier.security import validate_path_component
from aitelier.serializers import (  # noqa: F401  (re-exported for endpoints/tests)
    _REDACTED,
    _SECRET_KEYS,
    _TRACE_RECORD_KEYS,
    _duration_ms,
    _event_to_dict,
    _redact_secrets,
    _run_to_dict,
    _run_to_trace_dict,
)
from aitelier.storage import RunSpec, get_store

logger = logging.getLogger("aitelier")


# Correlation ID is set per-request by middleware.correlation_id_middleware
# and propagates through any logging done inside that request's task tree
# via contextvars. Imported from middleware.py since that's where it's
# mutated; this module only reads it (via the LogRecord factory).
from aitelier.middleware import _correlation_id_var  # noqa: E402

_AITELIER_LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
)
# uvicorn.access passes its data as positional args inside the message
# template ('%s - "%s %s HTTP/%s" %d'), not as named record attributes.
# Use %(message)s — getMessage() folds the args in before formatting.


class _JsonFormatter(logging.Formatter):
    """One-line-per-record JSON formatter for `[service] log_format = "json"`.

    Aggregator-friendly (Loki, Datadog, etc.). Includes correlation_id from
    the contextvar that the LogRecord factory stamped onto every record.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _install_correlation_logging() -> None:
    """Install a LogRecord factory that stamps every record with the current
    request's correlation_id, then align uvicorn's handler formatters so
    their output carries the same prefix as aitelier's own logs.
    """
    if getattr(_install_correlation_logging, "_installed", False):
        # Even if already installed, re-apply uvicorn formatters in case
        # uvicorn was re-imported (e.g., during dev reload).
        _retag_uvicorn_handlers()
        return

    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        record.correlation_id = _correlation_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _install_correlation_logging._installed = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(_active_formatter())
        root.addHandler(h)
        root.setLevel(logging.INFO)

    _retag_uvicorn_handlers()


def _active_formatter() -> logging.Formatter:
    """Pick the formatter based on [service] log_format in aitelier.toml.

    `json` → one-line JSON per record (aggregator-friendly).
    anything else → human-readable with [correlation_id] prefix.
    """
    if get_config().service.log_format.lower() == "json":
        return _JsonFormatter()
    return logging.Formatter(_AITELIER_LOG_FORMAT)


def _retag_uvicorn_handlers() -> None:
    """Override the formatters on uvicorn's loggers so access + error lines
    carry the same correlation_id prefix as aitelier's logs."""
    fmt = _active_formatter()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.setFormatter(fmt)


_install_correlation_logging()


async def _schedule_handler(entry: dict) -> None:
    """Run one fired schedule. The schedule's `task` dict is the same shape
    as `/v1/chat/completions` request body — we build a `ChatCompletionRequest`
    and route through the same execution helpers as a live HTTP call.
    """
    from starlette.datastructures import State

    task = dict(entry.get("task") or {})
    run_id = make_run_id()

    # Construct a minimal `Request`-like with the correlation_id the helpers
    # expect. Schedules don't have a real client correlation id; mint one.
    fake_req_state = State()
    fake_req_state.correlation_id = f"sched-{entry['id']}-{run_id}"

    class _FakeRequest:
        state = fake_req_state

    try:
        req = ChatCompletionRequest(**task)
        route, agent_backend, inner_llm = parse_model_route(req.model)
        await _validate_aitelier_opts(req, agent_path=(route == "agent"))
        if route == "agent":
            result = await _agent_chat_completion(
                req, _FakeRequest(),  # type: ignore[arg-type]
                agent_backend=agent_backend, inner_llm=inner_llm,
                run_id=run_id,
            )
        else:
            result = await _llm_chat_completion(
                req, _FakeRequest(),  # type: ignore[arg-type]
                run_id=run_id,
            )
    except Exception as exc:
        logger.exception("Scheduled run %s failed: %s", run_id, exc)
        # Persist a synthetic failed run row so /v1/runs and /v1/traces
        # surface this failure. Without this, schedule-side failures (bad
        # task body, model-route parse error, validator rejection) only
        # fire the webhook — there's no durable record. _finalize_terminal
        # tolerates a missing row; create_run is best-effort so a Postgres
        # outage during the schedule tick doesn't mask the original error.
        try:
            store = await get_store()
            # Classify via the same route parser the live path uses, not a
            # substring match — `"my-agent:thing"` is an LLM model, not an
            # agent backend. Defensive: the model may be missing/malformed
            # (that can be why this run failed), so fall back to "complete".
            try:
                sched_kind = (
                    "agent"
                    if parse_model_route(str(task.get("model", "")))[0] == "agent"
                    else "complete"
                )
            except Exception:
                sched_kind = "complete"
            try:
                await store.create_run(RunSpec(
                    run_id=run_id,
                    kind=sched_kind,
                    model=str(task.get("model", "")) or None,
                    correlation_id=fake_req_state.correlation_id,
                    metadata={
                        "schedule_id": entry.get("id"),
                        "schedule_name": entry.get("name"),
                    },
                    # Schedule's task IS the request body — capture it so a
                    # synthetic failed run still surfaces what was meant to
                    # run, useful for replay / debugging the schedule config.
                    request_body=task if isinstance(task, dict) else None,
                ))
            except Exception:
                pass  # row may already exist if the failure happened post-record_run
            await _finalize_terminal(
                store, run_id,
                status="error",
                error_type=classify_error(exc),
                error_msg=scrub_error_text(str(exc)),
                finish_reason="error", state="failed",
            )
        except Exception as persist_exc:
            logger.warning(
                "Schedule %s: failed to persist run row for %s: %s",
                entry.get("id"), run_id, persist_exc,
            )
        result = {
            "error": {"type": classify_error(exc), "message": scrub_error_text(str(exc))},
            "aitelier_run_id": run_id,
        }
    if entry.get("webhook_url"):
        await _enqueue_webhook(
            entry["webhook_url"],
            {
                "schedule_id": entry["id"],
                "run_id": run_id,
                "result": result,
            },
            run_id=run_id, schedule_id=entry["id"],
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-apply formatters to uvicorn loggers; they're configured by uvicorn
    # *after* this module is imported, so the import-time tag misses them.
    _retag_uvicorn_handlers()

    # Open the durable store (Postgres if [database] url is set, in-memory otherwise)
    store = await get_store()

    # Recovery sweep: any run still marked running/pending was owned by a
    # previous aitelier process. SA has no session-resume API today, so flip
    # them to `orphaned` rather than leaving them stuck in dashboards.
    orphaned_ids = await store.mark_orphaned_running_runs()
    if orphaned_ids:
        logger.warning(
            "Marked %d in-flight run(s) as orphaned on startup "
            "(previous process did not finalize them). Sample: %s",
            len(orphaned_ids), ", ".join(orphaned_ids[:10]) or "n/a",
        )
        # Async-mode callers registered a webhook_url and won't otherwise hear
        # back that their run died. Fire a terminal `orphaned` webhook so the
        # consumer can give up cleanly rather than poll forever. We iterate
        # exactly the rows this sweep flipped — prior-generation orphans
        # already fired their webhook and are no longer pending/running, so a
        # restart loop can't re-deliver stale orphan webhooks.
        for rid in orphaned_ids:
            run = await store.get_run(rid)
            if run is None:
                continue
            meta = run.metadata or {}
            webhook_url = meta.get("webhook_url") if isinstance(meta, dict) else None
            if not webhook_url:
                continue
            payload = {
                "error": {
                    "type": "Orphaned",
                    "message": (
                        "aitelier restarted while this run was in flight; "
                        "Sandbox Agent has no session-resume API, so the "
                        "run is unrecoverable."
                    ),
                },
                "aitelier_run_id": rid,
                "aitelier_state": "orphaned",
            }
            try:
                await store.enqueue_webhook(
                    webhook_url, payload, run_id=rid,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to enqueue orphan webhook for %s: %s",
                    rid, exc,
                )

    # Reconcile completion webhooks the previous process owed but never
    # enqueued — a crash between finalizing a run and enqueuing its webhook
    # would otherwise lose it silently (the async caller waits forever). These
    # runs are already terminal (not orphaned), so we deliver their stored
    # result. The payload is marked `aitelier_recovered` and carries the run's
    # persisted `result` rather than the live ChatCompletion envelope.
    try:
        # Bound to the webhook-retention window: a run that completed long ago
        # has already had its delivery row purged, so "no delivery row" no
        # longer proves it was never delivered — only recently-ended runs are
        # genuine crash-window candidates.
        wh_window = timedelta(days=get_config().purge.webhook_retention_days)
        awaiting = await store.runs_awaiting_webhook(
            since=datetime.now(UTC) - wh_window,
        )
    except Exception as exc:
        awaiting = []
        logger.exception("Webhook reconciliation query failed on startup: %s", exc)
    for run in awaiting:
        meta = run.metadata if isinstance(run.metadata, dict) else {}
        webhook_url = meta.get("webhook_url")
        if not webhook_url:
            continue
        payload = {
            "aitelier_run_id": run.run_id,
            "aitelier_recovered": True,
            "aitelier_state": run.state,
            "status": run.status,
            "result": run.result,
            "error_type": run.error_type,
            "error_msg": run.error_msg,
        }
        try:
            await store.enqueue_webhook(
                webhook_url, _redact_secrets(payload), run_id=run.run_id,
            )
            logger.info(
                "Reconciled missing completion webhook for run %s", run.run_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to enqueue reconciled webhook for %s: %s", run.run_id, exc,
            )

    # Purge old runs on startup
    run_retention = get_config().purge.run_retention_days
    deleted = await store.purge_old_runs(max_age_days=run_retention)
    if deleted:
        logger.info(
            "Purged %d runs older than %d days", deleted, run_retention,
        )

    # Start the persistent schedule tick loop
    from aitelier.schedules import start_tick_loop, stop_tick_loop
    start_tick_loop(_schedule_handler)

    # Start the durable webhook delivery worker
    from aitelier.webhook_worker import start_webhook_worker, stop_webhook_worker
    start_webhook_worker()

    # Start the background purge worker
    from aitelier.purge_worker import start_purge_worker, stop_purge_worker
    start_purge_worker()

    # Initialize OpenTelemetry GenAI export if [otel] enabled. No-op
    # when disabled (the default) — modules that aren't using OTel
    # never see the import cost.
    from aitelier.otel import init_tracer_provider, shutdown_tracer_provider
    init_tracer_provider()

    # Health check LiteLLM proxy. /health/liveness — no auth, no upstream
    # provider probing. /health would 5xx on transient backend issues (e.g.
    # OpenAI 429) and falsely log the proxy as unreachable on startup.
    cfg = get_config()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{cfg.litellm.base_url}/health/liveness")
            resp.raise_for_status()
            logger.info("LiteLLM proxy reachable at %s", cfg.litellm.base_url)
    except Exception as exc:
        logger.warning("LiteLLM proxy unreachable at %s: %s", cfg.litellm.base_url, exc)

    yield

    # Graceful shutdown: cancel any in-flight runs so SSE consumers see a
    # clean run.cancelled rather than a dropped connection. Best-effort —
    # we don't wait long for cleanup since the process is about to exit.
    if _active_runs:
        logger.info("Cancelling %d in-flight run(s) on shutdown", len(_active_runs))
        for task in list(_active_runs.values()):
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*_active_runs.values(), return_exceptions=True),
                timeout=2.0,
            )
        except TimeoutError:
            logger.warning("Some runs did not cleanly cancel within 2s")

    # Stop the schedule tick loop + webhook worker + purge worker.
    stop_tick_loop()
    stop_webhook_worker()
    stop_purge_worker()

    # Flush + shut down the OpenTelemetry tracer provider so the last
    # batch of spans reaches the collector. No-op when OTel wasn't
    # initialized.
    shutdown_tracer_provider()

    # Release the shared HTTP client pool.
    from aitelier.providers.llm import close_shared_client
    await close_shared_client()

    # Close the durable store.
    from aitelier.storage import close_store
    await close_store()


app = FastAPI(title="aitelier", version="0.1.0", lifespan=lifespan)


# Imported mid-file (after app setup); bound via attribute access so isort
# keeps this as one statement. Rate-limit state is re-exported for tests that
# reference it via `aitelier.server.*`.
import aitelier.middleware as _mw  # noqa: E402

_register_middleware = _mw.register_middleware
_RATE_LIMIT_BUCKET_CAP = _mw._RATE_LIMIT_BUCKET_CAP
_rate_limit_buckets = _mw._rate_limit_buckets

# Middleware stack (auth → correlation → body_size → rate_limit → handler)
# is defined in `middleware.py` and registered here on the FastAPI app.
# Rate-limit bucket state is also imported above from middleware.py — test
# code references it via `aitelier.server.*` for backward compat.
_register_middleware(app)


# Process boot time, captured at module import so `/v1/metrics` can
# report uptime without needing /proc on Linux or sysctl on macOS.
_PROCESS_STARTED_AT = time.monotonic()


# --- Request/Response models ---


# --- Primitive endpoints ---


# Imported mid-file (after app + helpers) to avoid an import cycle; bound via
# attribute access so isort keeps this as one stable statement. `_check_idempotency`
# is re-exported for the endpoints/ modules.
import aitelier.idempotency as _idem  # noqa: E402

_STREAM_IDEMPOTENCY_MAX_CHUNKS = _idem.STREAM_IDEMPOTENCY_MAX_CHUNKS
_IdempotencyContext = _idem.IdempotencyContext
_check_idempotency = _idem.check_idempotency
_record_idempotency = _idem.record_idempotency
_release_idempotency_ctx = _idem.release_idempotency_ctx



_KNOWN_LIMITATIONS = [
    "agent cost_usd is always null — only complete/embed track cost",
    "runs are purged on startup per [purge] run_retention_days "
    "(default 30); events and terminal webhook deliveries age out via "
    "the background purge worker",
]


@app.get("/v1/health")
async def health() -> dict:
    """Liveness probe. Cheap by design — k8s and load balancers hit
    this on a tight cadence.

    Surfaces a `dependencies` summary opportunistically from the
    discovery cache (no fresh probes here — `/v1/discovery` is the
    place to force a refresh, and is the readiness probe). When the
    cache is warm, `status` flips to `"degraded"` if any tracked dep
    is unreachable. When the cache is empty (cold-start, before
    discovery has been called), `dependencies` is omitted and
    `dependencies_probed` is `false` — so a consumer can tell "deps
    healthy" apart from "deps never checked" rather than reading the
    bare `"ok"` as a clean bill of health.
    """
    deps_summary: dict | None = None
    status = "ok"
    cached = _discovery_cache["value"]
    if isinstance(cached, dict):
        deps = cached.get("dependencies") or {}
        deps_summary = {
            name: {"reachable": bool(info.get("reachable"))}
            for name, info in deps.items()
            if isinstance(info, dict)
        }
        if any(not v["reachable"] for v in deps_summary.values()):
            status = "degraded"

    body: dict[str, Any] = {
        "status": status,
        "version": "0.1.0",
        "timestamp": _now_iso(),
        "dependencies_probed": deps_summary is not None,
        "known_limitations": _KNOWN_LIMITATIONS,
    }
    if deps_summary is not None:
        body["dependencies"] = deps_summary
    return body


@app.get("/v1/metrics")
async def metrics() -> dict:
    """Runtime counters for operators / monitoring agents.

    Intentionally narrow: things you'd want to see when investigating an
    aitelier-process anomaly (memory, CPU consumed, in-flight runs,
    pending webhook backlog, recent error rate). Dependency health lives
    on /v1/discovery; this endpoint never reaches downstream services.

    Macro picture is `process` (does aitelier itself look healthy) +
    `runs` (is there a backlog or error spike) + `webhooks` (is delivery
    keeping up).
    """
    uptime_seconds = round(time.monotonic() - _PROCESS_STARTED_AT, 3)
    rusage = resource.getrusage(resource.RUSAGE_SELF)

    store = await get_store()
    recent_since = datetime.now(UTC) - timedelta(minutes=5)
    # Aggregate store-side (COUNT/GROUP BY) rather than paging rows and
    # counting in Python — a 5-minute spike past any row cap would silently
    # under-report exactly when the operator most needs an accurate number.
    agg = await store.aggregate_runs(group_by="status", since=recent_since)
    by_status = {g["key"]: g["count"] for g in agg["groups"]}
    recent_total = agg["total"]["count"]

    webhook_pending = await _count_pending_webhooks(store)

    return {
        "uptime_seconds": uptime_seconds,
        "timestamp": _now_iso(),
        "process": {
            # ru_maxrss is bytes on macOS, kilobytes on Linux. Normalize to
            # MiB; operators care about order-of-magnitude, not precise units.
            "rss_mb": round(_normalize_maxrss(rusage.ru_maxrss) / (1024 * 1024), 2),
            "cpu_user_seconds": round(rusage.ru_utime, 3),
            "cpu_system_seconds": round(rusage.ru_stime, 3),
        },
        "runs": {
            "in_flight": len(_active_runs),
            "recent_5min": {
                "total": recent_total,
                "by_status": by_status,
            },
        },
        "webhooks": {"pending": webhook_pending},
    }


_UI_HTML = (Path(__file__).resolve().parent / "static" / "ui.html").read_text()


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui")


@app.get("/ui", include_in_schema=False)
async def ui() -> HTMLResponse:
    """Read-only browser over /v1/runs, run events, and trace aggregates.

    A single static page; all data is fetched client-side from the existing
    GET endpoints. Served public (see middleware `_PUBLIC_PATHS`) so the page
    loads without a token — the data calls it makes still honor auth when
    `[service] api_key` is set."""
    return HTMLResponse(_UI_HTML)


def _normalize_maxrss(raw: int) -> int:
    """Return ru_maxrss in bytes regardless of platform.

    `getrusage().ru_maxrss` is bytes on macOS/BSD, kilobytes on Linux —
    a glibc quirk dating to the early 90s. We branch on `sys.platform`.
    """
    import sys
    if sys.platform == "darwin":
        return int(raw)
    return int(raw) * 1024


async def _count_pending_webhooks(store) -> int:
    """Best-effort pending-webhook count for /v1/metrics. Failures yield 0
    rather than 5xx-ing a metrics endpoint."""
    try:
        return await store.count_pending_webhooks()
    except Exception:
        return 0


# --- Discovery ---


@functools.lru_cache(maxsize=1)
def _schemas_dir() -> Path:
    """Locate schemas/v1 dir. Prefer source-relative; fall back to cwd."""
    candidate = Path(__file__).resolve().parents[3] / "schemas" / "v1"
    if candidate.exists():
        return candidate
    return Path("schemas/v1").resolve()


@functools.lru_cache(maxsize=64)
def _load_schema(path: Path) -> dict:
    """Cached read+parse of a schema file. Schemas are immutable at runtime."""
    return json.loads(path.read_text())


def _list_endpoints() -> list[dict]:
    """Enumerate live HTTP endpoints from the FastAPI app — single source of truth.

    Newer FastAPI keeps `include_router` routes under an `_IncludedRouter`
    wrapper (`.original_router`) instead of flattening them into `app.routes`,
    so a flat `isinstance(APIRoute)` scan misses every router-registered
    endpoint. Recurse through those wrappers (applying any include prefix);
    older FastAPI (flattened) falls through the same code. Filtering to APIRoute
    keeps FastAPI's own /docs and /openapi.json out of the inventory.
    """
    from fastapi.routing import APIRoute

    def _walk(routes, prefix=""):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                ctx = getattr(route, "include_context", None)
                yield from _walk(
                    original.routes, prefix + (getattr(ctx, "prefix", "") or "")
                )
            elif isinstance(route, APIRoute):
                yield prefix + route.path, route.methods

    out: list[dict] = []
    for path, methods in _walk(app.routes):
        for method in (methods or set()) - {"HEAD", "OPTIONS"}:
            out.append({"method": method, "path": path})
    return sorted(out, key=lambda e: (e["path"], e["method"]))


def _list_schemas() -> dict[str, str]:
    """Map schema name → fetch URL. Empty dict if schemas dir is missing."""
    d = _schemas_dir()
    if not d.exists():
        return {}
    out: dict[str, str] = {}
    suffix = ".schema.json"
    for f in sorted(d.glob(f"*{suffix}")):
        name = f.name[: -len(suffix)]
        out[name] = f"/v1/schemas/{name}"
    return out


_DISCOVERY_TTL_SECONDS = 5.0
_discovery_cache: dict[str, Any] = {"at": 0.0, "value": None}
_discovery_lock = asyncio.Lock()


@app.get("/v1/discovery")
async def discovery() -> dict:
    """Capability + endpoint inventory for peer services.

    Probes dependencies live (parallel) with a short TTL cache so a polling
    peer doesn't repeatedly hit LiteLLM and Sandbox Agent. Keep /v1/health
    cheap for liveness; use this for self-discovery.
    """
    import time as _time

    now = _time.monotonic()
    if _discovery_cache["value"] is not None and \
       now - _discovery_cache["at"] < _DISCOVERY_TTL_SECONDS:
        return _discovery_cache["value"]

    async with _discovery_lock:
        # Re-check after lock — another request may have refreshed it
        now = _time.monotonic()
        if _discovery_cache["value"] is not None and \
           now - _discovery_cache["at"] < _DISCOVERY_TTL_SECONDS:
            return _discovery_cache["value"]

        cfg = get_config()

        litellm_info, sandbox_info, traces_info = await asyncio.gather(
            _probe_litellm(cfg),
            _probe_sandbox_agent(cfg),
            _probe_traces(),
        )

        litellm_ok = litellm_info["reachable"]
        sandbox_ok = sandbox_info["reachable"]

        def cap(ok: bool, reason: str) -> dict:
            return {"available": True} if ok else {"available": False, "reason": reason}

        # Per-model response_format support — same registry the request-time
        # normalizer consults, so consumers can fail fast at config time
        # rather than waiting for UnsupportedResponseFormat at first call.
        models = []
        for m in litellm_info.get("models") or []:
            entry: dict[str, Any] = {"name": m}
            supports = model_response_format_capabilities(m)
            if supports is not None:
                entry["response_format"] = supports
            models.append(entry)

        # Hosted-mode posture: scrub internal `base_url`s from the
        # dependency block. Error envelopes already scrub these via
        # `_scrub_sandbox_url`; surfacing them here would be inconsistent
        # — any authenticated caller could lift them and skip the abstraction.
        # `reachable` + `reason` + `agents`/`models` lists stay so the
        # endpoint is still useful for ops dashboards.
        if cfg.service.api_key:
            from aitelier.providers.acp_transport import _scrub_sandbox_url
            # A failed-probe `reason` carries `str(exc)`, which can embed the
            # internal base_url or a DB DSN — scrub it before dropping base_url
            # itself, or the URL/DSN leaks through the back door in hosted mode.
            # traces_info has no base_url, but its reason (a store exception)
            # can carry the Postgres DSN, so scrub_error_text still applies.
            for info in (litellm_info, sandbox_info, traces_info):
                if isinstance(info.get("reason"), str):
                    info["reason"] = scrub_error_text(
                        _scrub_sandbox_url(info["reason"], info.get("base_url"))
                    )
            litellm_info = {k: v for k, v in litellm_info.items() if k != "base_url"}
            sandbox_info = {k: v for k, v in sandbox_info.items() if k != "base_url"}

        result = {
            "service": "aitelier",
            "version": app.version,
            "api_version": "v1",
            "timestamp": _now_iso(),
            "endpoints": _list_endpoints(),
            "capabilities": {
                "chat_completions": cap(
                    litellm_ok or sandbox_ok,
                    "Neither LiteLLM nor Sandbox Agent reachable",
                ),
                "embeddings": cap(litellm_ok, "LiteLLM proxy unreachable"),
                "agent": cap(sandbox_ok, "Sandbox Agent unreachable"),
                "traces": traces_info,
            },
            "dependencies": {
                "litellm": litellm_info,
                "sandbox_agent": sandbox_info,
            },
            "models": models,
            "schemas": _list_schemas(),
            "known_limitations": _KNOWN_LIMITATIONS,
        }
        _discovery_cache["value"] = result
        _discovery_cache["at"] = _time.monotonic()
        return result


@app.get("/v1/schemas/{name}")
async def get_schema(name: str) -> dict:
    """Fetch a JSON Schema by name (without the `.schema.json` suffix)."""
    validate_path_component(name, "schema name")
    d = _schemas_dir().resolve()
    f = (d / f"{name}.schema.json").resolve()
    # Defense in depth: ensure resolved path stays inside schemas dir
    if not str(f).startswith(str(d) + "/") and f != d:
        raise HTTPException(status_code=400, detail="Invalid schema name")
    try:
        return _load_schema(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Schema not found: {name}") from None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_param(name: str, value: str | None) -> datetime | None:
    """Parse an ISO-8601 query param, raising a 400 (not an unhandled 500)
    on malformed input. Shared by the runs + traces endpoints so a bad
    `?since=yesterday` fails the same way everywhere."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{name} must be ISO-8601: {exc}",
        ) from None


# Mount endpoint sub-routers. Done at the bottom of the module so the
# helpers each router imports lazily are guaranteed to be defined by
# the time the first request fires. New endpoint surfaces should follow
# the same pattern (one APIRouter per resource, registered here).
from aitelier.endpoints.inference import router as _inference_router  # noqa: E402
from aitelier.endpoints.runs import router as _runs_router  # noqa: E402
from aitelier.endpoints.schedules import router as _schedules_router  # noqa: E402
from aitelier.endpoints.traces import router as _traces_router  # noqa: E402

app.include_router(_inference_router)
app.include_router(_runs_router)
app.include_router(_schedules_router)
app.include_router(_traces_router)
