"""FastAPI HTTP service for aitelier."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import resource
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aitelier.config import get_config
from aitelier.errors import classify_error, scrub_error_text
from aitelier.openai_compat import (
    AitelierAgentOpts,
    ChatCompletionRequest,
    EmbeddingsRequest,
    ScheduleRequest,
    agent_error_to_chat_completion_error,
    agent_result_to_chat_completion,
    agent_usage_to_openai,
    chat_completion_chunk,
    chat_completion_error_envelope,
    normalize_response_extras,
    parse_model_route,
    stream_final_extras,
    summarize_tool_calls,
)
from aitelier.providers.llm import (
    LLMError,
    UnsupportedResponseFormat,
    chat_completion,
    chat_completion_stream,
    embeddings,
    list_models,
    model_response_format_capabilities,
)
from aitelier.runner import make_run_id
from aitelier.runs import _finalize_terminal, hash_system_prompt, record_run, start_run
from aitelier.sandbox_proxy import (
    fetch_artifacts as _fetch_artifacts,
)
from aitelier.sandbox_proxy import (
    prepare_failed_result as _prepare_failed_result,
)
from aitelier.sandbox_proxy import (
    run_prepare as _run_prepare,
)
from aitelier.sandbox_proxy import (
    stop_sidecars as _stop_sidecars,
)
from aitelier.security import is_public_url
from aitelier.security import validate_path_component as _validate_path_component
from aitelier.storage import RunFilter, RunSpec, get_store

logger = logging.getLogger("aitelier")


# Correlation ID is set per-request by middleware and propagates through
# any logging done inside that request's task tree via contextvars.
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-",
)


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
    from aitelier.config import get_config
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
    run_id = make_run_id(entry.get("name", "scheduled"))

    # Construct a minimal `Request`-like with the correlation_id the helpers
    # expect. Schedules don't have a real client correlation id; mint one.
    fake_req_state = State()
    fake_req_state.correlation_id = f"sched-{entry['id']}-{run_id[-12:]}"

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
        logger.warning("Scheduled run %s failed: %s", run_id, exc)
        # Persist a synthetic failed run row so /v1/runs and /v1/traces
        # surface this failure. Without this, schedule-side failures (bad
        # task body, model-route parse error, validator rejection) only
        # fire the webhook — there's no durable record. _finalize_terminal
        # tolerates a missing row; create_run is best-effort so a Postgres
        # outage during the schedule tick doesn't mask the original error.
        try:
            store = await get_store()
            try:
                await store.create_run(RunSpec(
                    run_id=run_id,
                    kind="agent" if "agent:" in str(task.get("model", "")) else "complete",
                    model=str(task.get("model", "")) or None,
                    correlation_id=fake_req_state.correlation_id,
                    metadata={
                        "schedule_id": entry.get("id"),
                        "schedule_name": entry.get("name"),
                    },
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
    orphaned = await store.mark_orphaned_running_runs()
    if orphaned:
        # Surface a sample of IDs so operators can investigate the cause
        # without trawling Postgres.
        sample = await store.list_runs(RunFilter(state="orphaned", limit=100))
        logger.warning(
            "Marked %d in-flight run(s) as orphaned on startup "
            "(previous process did not finalize them). Sample: %s",
            orphaned, ", ".join(r.run_id for r in sample[:10]) or "n/a",
        )
        # Async-mode callers registered a webhook_url and won't otherwise hear
        # back that their run died. Fire a terminal `orphaned` webhook so the
        # consumer can give up cleanly rather than poll forever.
        for r in sample:
            meta = r.metadata or {}
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
                "aitelier_run_id": r.run_id,
                "aitelier_state": "orphaned",
            }
            try:
                await store.enqueue_webhook(
                    webhook_url, payload, run_id=r.run_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to enqueue orphan webhook for %s: %s",
                    r.run_id, exc,
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

    # Release the shared HTTP client pool.
    from aitelier.providers.llm import close_shared_client
    await close_shared_client()

    # Close the durable store.
    from aitelier.storage import close_store
    await close_store()


app = FastAPI(title="aitelier", version="0.1.0", lifespan=lifespan)


_AUTH_EXEMPT_PATHS = {"/v1/health"}


# Rate limit state — in-process token bucket keyed by (api_key or remote_addr).
# Single-process assumption; horizontal scaling would move this to Redis.
# OrderedDict so the LRU eviction below has O(1) `popitem(last=False)`.
_rate_limit_buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
_RATE_LIMIT_EXEMPT_PATHS = {"/v1/health"}
_RATE_LIMIT_BUCKET_CAP = 10_000


def _rate_limit_key(request: Request) -> str:
    """Identify the caller for rate-limiting. Bearer token if present (so
    a single key shared by N clients is one bucket), else remote IP.
    No X-Forwarded-For parsing: behind a reverse proxy every external
    caller shares one IP bucket — a hosted-mode deployment should either
    set per-key budgets via api_key + rate_limit_per_minute, or run the
    rate limit in the proxy itself."""
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return f"bearer:{auth[7:]}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


# Registered FIRST so it executes LAST in the middleware stack — auth runs
# before rate-limit, so unauthenticated traffic can't fill the bucket map.
@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    """Per-caller token bucket. Returns 429 with Retry-After when the
    bucket is empty. 0 = disabled (default). Excludes /v1/health.

    Bucket capacity equals the per-minute budget; the bucket refills
    linearly at budget/60 tokens per second. The bucket map is LRU-
    capped at _RATE_LIMIT_BUCKET_CAP entries so a caller cycling Bearer
    values can't grow it without bound."""
    from fastapi.responses import JSONResponse

    budget = get_config().service.rate_limit_per_minute
    if budget <= 0 or request.url.path in _RATE_LIMIT_EXEMPT_PATHS:
        return await call_next(request)

    now = time.monotonic()
    refill_rate = budget / 60.0
    key = _rate_limit_key(request)
    tokens, last = _rate_limit_buckets.get(key, (float(budget), now))
    tokens = min(float(budget), tokens + (now - last) * refill_rate)
    if tokens < 1.0:
        retry_after = max(1, int((1.0 - tokens) / refill_rate))
        _rate_limit_buckets[key] = (tokens, now)
        _rate_limit_buckets.move_to_end(key)
        return JSONResponse(
            {"detail": "Rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    _rate_limit_buckets[key] = (tokens - 1.0, now)
    _rate_limit_buckets.move_to_end(key)
    while len(_rate_limit_buckets) > _RATE_LIMIT_BUCKET_CAP:
        _rate_limit_buckets.popitem(last=False)
    return await call_next(request)


@app.middleware("http")
async def _body_size_middleware(request: Request, call_next):
    """Reject requests whose Content-Length exceeds the configured cap
    with 413, before any handler runs.

    Blocks the trivial memory-exhaustion vector where a hostile caller
    POSTs gigabytes into idempotency hashing or JSON parsing. Honors
    `service.max_request_body_bytes`; 0 disables the check.

    Notes:
      - Header-only check: clients that omit Content-Length (chunked
        transfer-encoding) are not blocked here. Put a reverse proxy
        in front of hosted aitelier if you need a hard cap.
      - /v1/health is exempt — k8s probes shouldn't bounce off this.
    """
    from fastapi.responses import JSONResponse

    if request.url.path == "/v1/health":
        return await call_next(request)

    cap = get_config().service.max_request_body_bytes
    if cap:
        raw_len = request.headers.get("Content-Length")
        if raw_len:
            try:
                body_len = int(raw_len)
            except ValueError:
                body_len = 0
            if body_len < 0 or body_len > cap:
                return JSONResponse(
                    {"detail": (
                        f"Request body {body_len} bytes exceeds cap "
                        f"{cap} bytes. Adjust service.max_request_body_bytes."
                    )},
                    status_code=413,
                )
    return await call_next(request)


_CORRELATION_ID_CHARSET = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


@app.middleware("http")
async def _correlation_id_middleware(request: Request, call_next):
    """Echo or generate X-Correlation-Id so consumers can tie their logs
    to ours. Untrusted input — length-cap and charset-restrict to keep
    log lines parseable and to block log-injection / terminal-escape
    vectors when the CID is rendered into structured log output."""
    raw = request.headers.get("X-Correlation-Id")
    if raw and _CORRELATION_ID_CHARSET.match(raw):
        cid = raw
    else:
        cid = str(uuid.uuid4())
    request.state.correlation_id = cid
    token = _correlation_id_var.set(cid)
    try:
        response = await call_next(request)
    finally:
        _correlation_id_var.reset(token)
    response.headers["X-Correlation-Id"] = cid
    return response


# Registered LAST so it executes FIRST — every other middleware runs only
# for authenticated callers (in hosted mode).
@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Gate every /v1/* endpoint on Authorization: Bearer <api_key> *if*
    service.api_key is configured. When unset (default), no auth is enforced
    — preserves the localhost-trust model.

    /v1/health is always public so liveness probes (k8s, load balancers)
    can hit it without a token.
    """
    from fastapi.responses import JSONResponse

    if request.url.path not in _AUTH_EXEMPT_PATHS:
        configured = get_config().service.api_key
        if configured:
            auth = request.headers.get("Authorization") or ""
            # Constant-time compare so an attacker can't reconstruct the
            # key byte-by-byte via response timing.
            if not auth.startswith("Bearer ") or not hmac.compare_digest(
                auth[7:], configured,
            ):
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401,
                )
    return await call_next(request)


# Per-process registry of in-flight runs, for cancellation.
# Single-process assumption; if aitelier ever scales horizontally this
# moves to a shared store.
_active_runs: dict[str, asyncio.Task] = {}


def _reject_if_saturated() -> None:
    """Cap concurrent inference. Beyond `service.max_in_flight_runs`,
    return 503 typed as `ProviderUnavailable` so SDK retry policies
    treat overload as a transient failure rather than crashing the
    consumer. Cap of 0 disables the check (single-tenant dev)."""
    cap = get_config().service.max_in_flight_runs
    if cap and len(_active_runs) >= cap:
        raise HTTPException(
            status_code=503,
            detail=(
                f"aitelier is at capacity ({len(_active_runs)} in-flight "
                f"runs, cap={cap}). Retry after current runs drain."
            ),
        )

# SSE comment cadence during silent agent-planning phases. SSE clients
# ignore lines starting with `:`; the frame keeps reverse proxies and
# consumer read timeouts from tearing down a connection mid-run.
_SSE_KEEPALIVE_SECONDS = 25.0

# Process boot time, captured at module import so `/v1/metrics` can
# report uptime without needing /proc on Linux or sysctl on macOS.
_PROCESS_STARTED_AT = time.monotonic()


def _cancelled_result(run_id: str, kind: str) -> dict:
    """Result shape returned when a run is cancelled mid-flight."""
    return {
        "kind": kind,
        "provider": "",
        "status": "error",
        "duration_s": 0.0,
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "cancelled",
        "cost_usd": None,
        "error_type": "Cancelled",
        "error_msg": "run cancelled",
    }


# --- Request/Response models ---


# --- Primitive endpoints ---


from aitelier.idempotency import (
    STREAM_IDEMPOTENCY_MAX_CHUNKS as _STREAM_IDEMPOTENCY_MAX_CHUNKS,
    IdempotencyContext as _IdempotencyContext,
    check_idempotency as _check_idempotency,
    record_idempotency as _record_idempotency,
    release_idempotency_ctx as _release_idempotency_ctx,
)


async def _check_webhook_url_or_die(url: str) -> None:
    """SSRF guard on aitelier-initiated outbound URLs.

    Always on unless the operator explicitly opts in to loopback
    callbacks via `service.allow_loopback_webhooks = true`. Previously
    this guard was gated on `service.api_key` (hosted mode); the result
    was that a localhost caller in dev mode could POST `webhook_url`
    values pointing at AWS IMDS (`169.254.169.254`) or arbitrary RFC1918
    targets and the durable worker would dutifully fire at them. Default
    is now "deny private/loopback"; the legacy convenience is a
    deliberate opt-in.
    """
    if get_config().service.allow_loopback_webhooks:
        return
    if not await is_public_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "webhook_url must resolve to a public, non-loopback host. "
                "For dev workflows where localhost callbacks are needed, "
                "set [service] allow_loopback_webhooks = true in aitelier.toml."
            ),
        )


async def _enqueue_webhook(
    url: str, payload: dict, *, run_id: str | None = None,
    schedule_id: str | None = None,
) -> None:
    """Enqueue a webhook for durable delivery by the background worker.

    Replaces the previous fire-and-forget inline POST. The worker retries
    with exponential backoff (1s/5s/30s/5min/1hr) up to 5 attempts.
    """
    try:
        store = await get_store()
        await store.enqueue_webhook(url, payload,
                                     run_id=run_id, schedule_id=schedule_id)
    except Exception as exc:
        # Enqueue is the only delivery path. A previous fallback POSTed
        # inline when Postgres was down, but it skipped HMAC signing AND
        # the delivery-time SSRF re-check — both of which the worker
        # applies on the durable path. Better to log + lose this single
        # delivery than to emit an unsigned, un-SSRF-checked webhook.
        logger.warning(
            "Webhook enqueue failed (%s); delivery dropped. run_id=%s schedule_id=%s",
            exc, run_id, schedule_id,
        )


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
    if not response_format or response_format.get("type") != "json_schema":
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


def _reject_agent_incompatible_fields(req: ChatCompletionRequest) -> None:
    """Hard-reject OpenAI fields that have no honest mapping to the agent
    path. Silent drops are the bug class we explicitly fight.

    Consumers whose transport can't suppress `tools` / `tool_choice`
    per-profile can opt into a silent drop via
    `aitelier.allow_tool_drop = true`. The drop is documented and audited;
    that's the difference from a silent bug.
    """
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
    if req.top_p is not None:
        raise HTTPException(
            status_code=400,
            detail="`top_p` is not supported on the agent path — the inner "
                   "agent controls sampling.",
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
    from aitelier.providers.sandbox_agent import call_via_sandbox

    cid = request.state.correlation_id
    _reject_agent_incompatible_fields(req)
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
    )

    async def _do() -> dict:
        return await call_via_sandbox(
            agent_backend, initial_message,
            workspace=opts.workspace,
            system_prompt=system_prompt,
            mcp_servers=opts.mcp_servers,
            tool_allowlist=opts.tool_allowlist,
            response_format=req.response_format,
            max_turns=opts.max_turns,
            plan_mode=opts.plan_mode,
            agent_model=inner_llm,
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

    if result.get("status") == "error":
        status, body = agent_error_to_chat_completion_error(result)
        body["aitelier_status_code"] = status
        body["correlation_id"] = cid
        return body

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


_STREAM_QUEUE_SENTINEL: dict = {"_eof": True}


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
    from aitelier.providers.sandbox_agent import call_via_sandbox_stream
    try:
        async for event in call_via_sandbox_stream(
            agent_backend, initial_message,
            workspace=opts.workspace,
            system_prompt=system_prompt,
            mcp_servers=opts.mcp_servers,
            tool_allowlist=opts.tool_allowlist,
            response_format=req.response_format,
            max_turns=opts.max_turns,
            plan_mode=opts.plan_mode,
            agent_model=inner_llm,
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
    event: dict, *, model: str, run_id: str, stamp,
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
    c = stamp(chat_completion_chunk(
        request_model=model, run_id=run_id,
        delta={}, finish_reason="stop", usage=chunk_usage,
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
    state = (
        "cancelled" if final.get("error_type") == "Cancelled"
        else "failed" if final.get("status") == "error"
        else "completed"
    )
    return final, state


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
    _reject_agent_incompatible_fields(req)
    opts = req.aitelier or AitelierAgentOpts()

    system_prompt, initial_message = _translate_messages(req.messages)
    system_prompt = _fold_examples(system_prompt, opts.examples)
    system_prompt = _fold_response_format(system_prompt, req.response_format)

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
                # tool_call / tool_result intentionally dropped — see docstring.
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
            asyncio.create_task(_finalize_stream_run(
                run_id=run_id, final=final, state=state,
                idem=idem,
                recorded_chunks=recorded_chunks if should_cache else None,
            ))

    return _sse_response(event_generator())


async def _finalize_stream_run(
    *, run_id: str, final: dict, state: str,
    idem: _IdempotencyContext | None, recorded_chunks: list[dict] | None,
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
    spec = RunSpec(
        run_id=run_id, kind="complete", model=req.model,
        trace_tag=None, correlation_id=cid,
        system_prompt_hash=sp_hash,
        metadata={"correlation_id": cid},
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

    result = await record_run(spec, _do())
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

    await start_run(RunSpec(
        run_id=run_id, kind="complete", model=req.model,
        correlation_id=cid, system_prompt_hash=sp_hash,
        metadata={"correlation_id": cid},
    ))

    body = _llm_body_from_request(req)

    async def event_generator():
        final: dict | None = None
        accumulated: list[str] = []
        reasoning_accumulated: list[str] = []
        tool_call_seen = False
        usage: dict | None = None
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
                "finish_reason": "stop",
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
            if final is not None:
                state = "failed" if final.get("status") == "error" else "completed"
                store = await get_store()
                await store.finalize_run(run_id, final, state=state)

    return _sse_response(event_generator())


_REDACTED = "[redacted]"


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
            if k in ("headers", "env") and isinstance(v, list):
                out[k] = [_REDACTED for _ in v]
            elif k in ("api_key", "token", "secret", "authorization"):
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
    "trace_id", "started_at", "ended_at", "model", "kind", "finish_reason",
    "tool_call_count", "input_tokens", "output_tokens", "total_tokens",
    "cost_usd", "system_prompt_hash", "trace_tag", "parent_run_id", "status",
    "error_type", "error_msg", "metadata",
})


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
        "cost_usd": run.cost_usd,
        "finish_reason": run.finish_reason,
        "tool_call_count": run.tool_call_count,
        "system_prompt_hash": run.system_prompt_hash,
        "status": run.status,
        "error_type": run.error_type,
        "error_msg": run.error_msg,
        "result": _redact_secrets(run.result),
        "metadata": _redact_secrets(run.metadata),
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
    place to force a refresh). When the cache is warm, `status` flips
    to `"degraded"` if any tracked dep is unreachable. When the cache
    is empty (cold-start, before discovery has been called),
    `dependencies` is omitted entirely and `status` stays `"ok"`.
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
    recent = await store.list_runs(RunFilter(since=recent_since, limit=1000))
    by_status: dict[str, int] = {}
    for r in recent:
        by_status[r.status or "<none>"] = by_status.get(r.status or "<none>", 0) + 1

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
                "total": len(recent),
                "by_status": by_status,
            },
        },
        "webhooks": {"pending": webhook_pending},
    }


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
    """Enumerate live HTTP endpoints from the FastAPI app — single source of truth."""
    from fastapi.routing import APIRoute
    out: list[dict] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            out.append({"method": method, "path": route.path})
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


async def _probe_litellm(cfg) -> dict:
    """Live probe: LiteLLM /v1/models. Returns reachability + model list."""
    try:
        from aitelier.providers.llm import get_shared_client
        client = await get_shared_client()
        resp = await client.get(
            f"{cfg.litellm.base_url}/v1/models",
            headers={"Authorization": f"Bearer {cfg.litellm.api_key}"},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")
            )
            return {"reachable": True, "base_url": cfg.litellm.base_url, "models": models}
        return {
            "reachable": False,
            "base_url": cfg.litellm.base_url,
            "reason": f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "reachable": False,
            "base_url": cfg.litellm.base_url,
            "reason": f"{type(exc).__name__}: {exc}",
        }


async def _probe_traces() -> dict:
    """Live probe: durable store queryable."""
    try:
        store = await get_store()
        await store.list_runs(RunFilter(limit=1))
        return {"available": True}
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


async def _probe_sandbox_agent(cfg) -> dict:
    """Live probe: Sandbox Agent reachability + available agent backends.

    Hits GET /v1/agents on the sandbox-agent server (Rivet). Returns the list
    of agent IDs the sandbox advertises (claude-code, codex, opencode, ...).
    """
    try:
        from aitelier.providers.llm import get_shared_client
        headers = {}
        if cfg.sandbox_agent.token:
            headers["Authorization"] = f"Bearer {cfg.sandbox_agent.token}"
        client = await get_shared_client()
        resp = await client.get(
            f"{cfg.sandbox_agent.base_url}/v1/agents",
            headers=headers,
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            # /v1/agents returns either a list or {"agents": [...]} — accept both
            raw = data if isinstance(data, list) else data.get("agents") or []
            # `mock` is filtered: the SA mock backend doesn't return a
            # sessionId on session/new, so any consumer who picks it up
            # as a test target gets a confusing handshake error. The
            # backend is still reachable via direct SA URL for SA-level
            # tests; aitelier just doesn't advertise it.
            agents = sorted(
                a["id"] if isinstance(a, dict) else a
                for a in raw
                if ((isinstance(a, dict) and a.get("id") and a["id"] != "mock")
                    or (isinstance(a, str) and a != "mock"))
            )
            return {
                "reachable": True,
                "base_url": cfg.sandbox_agent.base_url,
                "agents": agents,
            }
        return {
            "reachable": False,
            "base_url": cfg.sandbox_agent.base_url,
            "reason": f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "reachable": False,
            "base_url": cfg.sandbox_agent.base_url,
            "reason": f"{type(exc).__name__}: {exc}",
        }


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

    from aitelier.config import get_config

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

        litellm_info, sandbox_info = await asyncio.gather(
            _probe_litellm(cfg),
            _probe_sandbox_agent(cfg),
        )
        traces_info = await _probe_traces()

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
    _validate_path_component(name, "schema name")
    d = _schemas_dir().resolve()
    f = (d / f"{name}.schema.json").resolve()
    # Defense in depth: ensure resolved path stays inside schemas dir
    if not str(f).startswith(str(d) + "/") and f != d:
        raise HTTPException(status_code=400, detail="Invalid schema name")
    try:
        return _load_schema(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Schema not found: {name}") from None


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Disable nginx response buffering; otherwise events stall until the
    # consumer connection idles and a buffer flush is forced.
    "X-Accel-Buffering": "no",
}


def _sse_response(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
