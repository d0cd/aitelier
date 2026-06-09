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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aitelier.config import get_config
from aitelier.errors import classify_error
from aitelier.openai_compat import (
    AitelierAgentOpts,
    ChatCompletionRequest,
    EmbeddingsRequest,
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
from aitelier.runs import hash_system_prompt, record_run, start_run
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
    """One-line-per-record JSON formatter for AITELIER_LOG_FORMAT=json.

    Aggregator-friendly (Loki, Datadog, etc.). Includes correlation_id from
    the contextvar that the LogRecord factory stamped onto every record.
    """

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return _json.dumps(payload, default=str)


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
        result = {
            "error": {"type": classify_error(exc), "message": str(exc)},
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
            if body_len > cap:
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


# --- Primitive endpoints ---


def _merge_correlation(metadata: dict | None, cid: str) -> dict:
    out = dict(metadata or {})
    out["correlation_id"] = cid
    return out


_IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass
class _IdempotencyContext:
    """Carried between _check_idempotency and _record_idempotency. Cleaner
    than stashing fields on request.state where intervening middleware
    could shadow them."""
    key: str
    body_hash: str
    endpoint: str
    cached: dict | None


_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


async def _check_idempotency(
    request: Request, endpoint: str,
) -> _IdempotencyContext | None:
    """If the request carries `Idempotency-Key`, look up a prior response.

    Returns None if no header (no idempotency in play). Otherwise returns
    an `_IdempotencyContext`: `.cached` is the prior response on hit,
    None on miss/expiry. Raises 422 if the same key was used for a
    different body — almost always a consumer bug.

    Keys are length-capped and charset-restricted at the boundary so a
    misbehaving (or hostile) client can't flood the `idempotency_keys`
    table with megabyte rows or inject control characters into error
    messages that echo the key back.
    """
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None
    if not _IDEMPOTENCY_KEY_PATTERN.match(key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Idempotency-Key must be 1–200 chars of "
                "[A-Za-z0-9._:-]. UUIDs work; opaque tokens with that "
                "charset work; arbitrary user input does not."
            ),
        )
    body_bytes = await request.body()
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    rec = await (await get_store()).get_idempotent(key)
    if rec is None:
        return _IdempotencyContext(key, body_hash, endpoint, cached=None)
    if rec.body_hash != body_hash:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Idempotency-Key {key!r} was already used for a different "
                f"request body. Use a fresh UUID for distinct requests."
            ),
        )
    return _IdempotencyContext(key, body_hash, endpoint, cached=rec.response)


async def _record_idempotency(
    ctx: _IdempotencyContext | None, response: dict,
) -> None:
    """Persist the response under this Idempotency-Key. No-op if ctx is None.
    Best-effort: storage failure shouldn't fail the call — we already have
    the response in hand."""
    if ctx is None:
        return
    from aitelier.storage import IdempotencyRecord
    try:
        store = await get_store()
        await store.record_idempotent(IdempotencyRecord(
            key=ctx.key, body_hash=ctx.body_hash, endpoint=ctx.endpoint,
            status_code=200, response=response,
            run_id=response.get("run_id"),
            expires_at=datetime.now(UTC) + _IDEMPOTENCY_TTL,
        ))
    except Exception as exc:
        logger.warning("Failed to record idempotency key %s: %s", ctx.key, exc)


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
        # If enqueueing itself fails (e.g. DB down), fall back to inline POST
        # so the consumer at least *might* hear about completion.
        logger.warning("Webhook enqueue failed (%s); attempting inline POST", exc)
        try:
            from aitelier.providers.llm import get_shared_client
            client = await get_shared_client()
            await client.post(url, json=payload, timeout=10)
        except Exception as exc2:
            logger.warning("Inline webhook fallback to %s also failed: %s",
                            url, exc2)


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


def _validate_aitelier_opts(req: ChatCompletionRequest, *, agent_path: bool) -> None:
    """Refuse aitelier.* on the LLM path. The namespace is agent-specific."""
    if req.aitelier is not None and not agent_path:
        raise HTTPException(
            status_code=400,
            detail="`aitelier.*` options are only valid when model starts "
                   "with `agent:`.",
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
            "error_type": classify_error(exc), "error_msg": str(exc),
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
                          "error_msg": str(exc)})
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
        chunk["aitelier_trace_id"] = run_id
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
            )
            asyncio.create_task(_finalize_stream_run(
                run_id=run_id, final=final, state=state,
                idem=idem if should_cache else None,
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
    except Exception as exc:
        logger.warning(
            "background finalize for stream run %s failed: %s",
            run_id, exc,
        )


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
                "error_msg": str(exc),
                "_aitelier_http_status": 400,
            }
        except LLMError as exc:
            return {
                "status": "error",
                "error_type": exc.error_type,
                "error_msg": str(exc),
                "_aitelier_http_status": _http_status_for_llm_error(exc),
            }
        normalize_response_extras(body, resp)
        resp["aitelier_run_id"] = run_id
        resp["aitelier_trace_id"] = run_id
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
        "temperature", "max_tokens", "top_p", "n", "response_format",
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
                chunk["aitelier_trace_id"] = run_id
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
                    "aitelier_trace_id": run_id,
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
            final = {
                "kind": "complete", "provider": req.model, "status": "error",
                "error_type": err_type, "error_msg": str(exc),
                "finish_reason": "error",
            }
            yield _sse_event("", {
                "error": {"type": err_type, "message": str(exc)},
                "aitelier_run_id": run_id,
            })
        finally:
            if final is not None:
                state = "failed" if final.get("status") == "error" else "completed"
                store = await get_store()
                await store.finalize_run(run_id, final, state=state)

    return _sse_response(event_generator())


@app.post("/v1/chat/completions")
async def chat_completions_endpoint(req: ChatCompletionRequest, request: Request):
    """OpenAI-shape chat completions.

    Routing:
      - `model = "agent:<backend>[/<inner-llm>]"` → Sandbox Agent
      - any other `model` → LiteLLM passthrough

    `aitelier.*` options on the agent path; not accepted on the LLM path.
    """
    route, agent_backend, inner_llm = parse_model_route(req.model)
    _validate_aitelier_opts(req, agent_path=(route == "agent"))
    _reject_if_saturated()

    if route == "agent":
        idem = await _check_idempotency(request, "/v1/chat/completions")
        if idem and idem.cached is not None:
            cached = dict(idem.cached)
            if cached.get("_aitelier_stream"):
                return _replay_cached_stream(cached)
            return _render_chat_completion(cached)
        run_id = make_run_id("chat_agent")
        if req.stream:
            return await _agent_chat_completion_stream(
                req, request,
                agent_backend=agent_backend, inner_llm=inner_llm,
                run_id=run_id, idem=idem,
            )
        result = await _agent_chat_completion(
            req, request,
            agent_backend=agent_backend, inner_llm=inner_llm, run_id=run_id,
        )
        await _record_idempotency(idem, result)
        return _render_chat_completion(result)

    # LLM path
    run_id = make_run_id("chat_llm")
    if req.stream:
        return await _llm_chat_completion_stream(req, request, run_id=run_id)
    result = await _llm_chat_completion(req, request, run_id=run_id)
    return _render_chat_completion(result)


@app.post("/v1/embeddings")
async def embeddings_endpoint(req: EmbeddingsRequest, request: Request):
    """OpenAI-shape embeddings (LiteLLM passthrough)."""
    cid = request.state.correlation_id
    run_id = make_run_id("embed")

    body: dict[str, Any] = {"model": req.model, "input": req.input}
    if req.encoding_format is not None:
        body["encoding_format"] = req.encoding_format
    if req.dimensions is not None:
        body["dimensions"] = req.dimensions
    if req.user is not None:
        body["user"] = req.user

    spec = RunSpec(
        run_id=run_id, kind="embed", model=req.model,
        correlation_id=cid, metadata={"correlation_id": cid},
    )

    async def _do() -> dict:
        try:
            resp = await embeddings(body)
        except LLMError as exc:
            return {
                "status": "error",
                "error_type": exc.error_type, "error_msg": str(exc),
                "_aitelier_http_status": _http_status_for_llm_error(exc),
            }
        if req.encoding_format == "base64":
            _ensure_base64_embeddings(resp)
        resp["aitelier_run_id"] = run_id
        resp["aitelier_trace_id"] = run_id
        resp["correlation_id"] = cid
        return resp

    result = await record_run(spec, _do())
    if result.get("status") == "error":
        return _render_chat_completion(chat_completion_error_envelope(
            result, run_id=run_id, correlation_id=cid,
        ))
    return result


def _ensure_base64_embeddings(resp: dict) -> None:
    """Honor `encoding_format: "base64"` even when the upstream route
    (Ollama-via-LiteLLM, today) ignored the field and returned floats.
    OpenAI's contract is float32 little-endian packed bytes, base64-encoded.
    Mutates `resp` in place; no-op for entries already encoded."""
    import base64
    import struct
    for entry in resp.get("data") or []:
        emb = entry.get("embedding")
        if isinstance(emb, list) and emb and isinstance(emb[0], (int, float)):
            packed = struct.pack(f"<{len(emb)}f", *emb)
            entry["embedding"] = base64.b64encode(packed).decode("ascii")


@app.get("/v1/models")
async def list_models_endpoint() -> dict:
    """OpenAI-shape model list. Entries fall into two flavors:

    - **LLM**: standard OpenAI shape (`id`, `object: "model"`, `owned_by`).
      `response_format` annotates which `json_object`/`json_schema` modes
      the provider supports.
    - **Agent**: `id = "agent:<backend>"`, `aitelier_agent: true`. Lists
      `aitelier_inner_llms` (the LLM aliases the backend can drive) and
      `aitelier_capabilities` (a subset of Sandbox Agent's capability
      flags). Consumers can validate `agent:<backend>/<inner-llm>`
      strings upfront rather than after a failed run.
    """
    try:
        data = await list_models()
    except LLMError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from None
    cfg = get_config()
    agents = await _list_agent_models(cfg, llm_ids=[m["id"] for m in data])
    return {"object": "list", "data": data + agents}


async def _list_agent_models(cfg, *, llm_ids: list[str]) -> list[dict]:
    """Build agent-model entries by probing Sandbox Agent's /v1/agents.

    Returns an empty list when SA is unreachable — `/v1/models` shouldn't
    fail just because the sandbox is down; LLM models still work. Probe
    failures are logged at WARN so consumers seeing zero agent rows can
    diagnose without having to enable debug logging or read
    `/v1/discovery` — which already carries the structured reason via
    `_probe_sandbox_agent`.
    """
    try:
        from aitelier.providers.llm import get_shared_client
        client = await get_shared_client()
        headers = {}
        if cfg.sandbox_agent.token:
            headers["Authorization"] = f"Bearer {cfg.sandbox_agent.token}"
        resp = await client.get(
            f"{cfg.sandbox_agent.base_url}/v1/agents",
            headers=headers, timeout=3,
        )
        if resp.status_code != 200:
            logger.warning(
                "agent model enumeration: SA /v1/agents returned HTTP %s "
                "from %s — /v1/models will omit agent rows. Check "
                "/v1/discovery → dependencies.sandbox_agent for details.",
                resp.status_code, cfg.sandbox_agent.base_url,
            )
            return []
        raw = resp.json()
    except Exception as exc:
        logger.warning(
            "agent model enumeration: SA probe at %s failed (%s: %s) — "
            "/v1/models will omit agent rows. Check /v1/discovery → "
            "dependencies.sandbox_agent for details.",
            cfg.sandbox_agent.base_url, type(exc).__name__, exc,
        )
        return []

    agents_raw = raw if isinstance(raw, list) else raw.get("agents") or []
    out: list[dict] = []
    for a in agents_raw:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if not a.get("installed", True):
            # Don't advertise uninstalled backends; agent runs against them
            # would fail with NotInstalled. Consumers calling `/v1/discovery`
            # see the full advertised set.
            continue
        out.append({
            "id": f"agent:{a['id']}",
            "object": "model",
            "owned_by": "sandbox-agent",
            "aitelier_agent": True,
            # The backend can drive any chat-capable LLM LiteLLM advertises;
            # consumers can pair as `agent:<backend>/<llm_id>`. The raw
            # LiteLLM catalog includes TTS / image / embedding / moderation
            # routes that can't drive a code agent, so we filter those out
            # before exposing the inner-LLM picklist.
            "aitelier_inner_llms": _filter_chat_capable(llm_ids),
            "aitelier_capabilities": a.get("capabilities") or {},
            # Declarative request-field caps mirroring the agent-path gates
            # enforced by `_reject_agent_incompatible_fields`. Generic
            # consumers (model pickers, doctor probes) can pre-strip
            # request fields from the catalog instead of waiting for a 400.
            "aitelier_request_caps": {
                "tools": False,
                "tool_choice": False,
                "n_gt_1": False,
                "top_p": False,
                "streaming": True,
                "response_format": ["json_schema"],
            },
        })
    return sorted(out, key=lambda m: m["id"])


# Model-id substrings that mark a route as not a valid inner LLM for a
# code agent. Covers non-chat modalities (TTS, image, embeddings, audio
# transcription, moderation, video) plus chat models with constrained
# variants the agent harness can't drive (realtime voice / web-search
# preview endpoints that change the response shape).
_NON_CHAT_MODEL_MARKERS = (
    "tts", "whisper", "dall-e", "image", "embed", "moderation",
    "audio", "transcribe", "speech", "sora",
    "realtime", "-search-preview",
)


def _filter_chat_capable(model_ids: list[str]) -> list[str]:
    return [m for m in model_ids
            if not any(marker in m.lower() for marker in _NON_CHAT_MODEL_MARKERS)]


@app.post("/v1/runs")
async def submit_async_run(req: AsyncRunRequest, request: Request) -> dict:
    """Async agent run: returns immediately with a run_id; the final
    ChatCompletion (or error) is delivered via webhook when ready.

    LLM-path async isn't supported — LLM calls are short and stream-capable;
    use /v1/chat/completions. Async exists for long-running agent runs.
    """
    route, agent_backend, inner_llm = parse_model_route(req.model)
    if route != "agent":
        raise HTTPException(
            status_code=400,
            detail="/v1/runs is for async agent runs only — set model to "
                   "'agent:<backend>[/<inner-llm>]', or use "
                   "/v1/chat/completions for LLM calls.",
        )
    _reject_if_saturated()

    cid = request.state.correlation_id
    idem = await _check_idempotency(request, "/v1/runs")
    if idem and idem.cached is not None:
        return idem.cached

    if req.webhook_url:
        await _check_webhook_url_or_die(req.webhook_url)

    run_id = make_run_id("chat_agent_async")
    webhook_url = req.webhook_url
    inner_req = req  # ChatCompletionRequest fields are a subset

    async def _run_and_callback() -> None:
        try:
            result = await _agent_chat_completion(
                inner_req, request,
                agent_backend=agent_backend, inner_llm=inner_llm, run_id=run_id,
                webhook_url=webhook_url,
            )
        except Exception as exc:
            result = {
                "error": {
                    "type": classify_error(exc), "message": str(exc),
                },
                "aitelier_run_id": run_id,
            }
        # Strip the rendering hint before persistence/delivery — it's only
        # meaningful to a synchronous HTTP responder, not to webhook consumers.
        result.pop("aitelier_status_code", None)
        if webhook_url:
            await _enqueue_webhook(webhook_url, result, run_id=run_id)

    outer_task = asyncio.create_task(_run_and_callback())
    # Pre-register so an immediate POST /v1/runs/{id}/cancel doesn't 404.
    # `_agent_chat_completion` swaps this entry for the inner run task once
    # it starts; the outer task is the safe placeholder.
    _active_runs[run_id] = outer_task
    accepted = {
        "run_id": run_id,
        "status": "accepted",
        "correlation_id": cid,
        "webhook_url": webhook_url,
    }
    await _record_idempotency(idem, accepted)
    return accepted


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
        "result": run.result,
        "metadata": run.metadata,
    }


def _run_to_trace_dict(run) -> dict:
    """TraceRecord shape returned by /v1/traces.

    A narrower projection of `_run_to_dict` focused on observability fields
    (counts, tokens, cost, status). For full operational detail (state,
    sandbox info, environment), use /v1/runs.
    """
    full = _run_to_dict(run)
    return {k: full[k] for k in _TRACE_RECORD_KEYS if k in full}


@app.get("/v1/traces")
async def traces_endpoint(
    since: str | None = None,
    trace_tag: str | None = None,
    parent_run_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query recent runs as TraceRecord summaries (counts, tokens, cost).

    `parent_run_id` narrows to children of a specific parent — useful
    for rendering a multi-agent workflow's subtree as a flat trace list.
    """
    store = await get_store()
    since_dt = datetime.fromisoformat(since) if since else None
    flt = RunFilter(
        trace_tag=trace_tag, parent_run_id=parent_run_id,
        since=since_dt, limit=limit,
    )
    runs = await store.list_runs(flt)
    if status:
        runs = [r for r in runs if r.status == status]
    return [_run_to_trace_dict(r) for r in runs]


@app.get("/v1/traces/aggregates")
async def traces_aggregates_endpoint(
    group_by: str = "trace_tag",
    since: str | None = None,
    until: str | None = None,
    trace_tag: str | None = None,
) -> dict:
    """Roll up run stats.

    `group_by` ∈ {trace_tag, kind, model, agent_id, status, error_type, day}.
    """
    store = await get_store()
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    try:
        return await store.aggregate_runs(
            group_by=group_by, since=since_dt, until=until_dt, trace_tag=trace_tag,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/v1/traces/{trace_id}")
async def get_trace_endpoint(trace_id: str) -> dict:
    """Get a single trace by ID. Same data as /v1/runs/{id} in TraceRecord shape."""
    store = await get_store()
    run = await store.get_run(trace_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
    return _run_to_trace_dict(run)


# _validate_path_component is imported at top from aitelier.security and
# aliased for the many existing call sites in this module.


def _event_to_dict(event) -> dict:
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "seq": event.seq,
        "kind": event.kind,
        "ts": event.ts.isoformat() if event.ts else None,
        "payload": event.payload,
    }


@app.get("/v1/runs")
async def list_runs_endpoint(
    state: str | None = None,
    kind: str | None = None,
    agent_id: str | None = None,
    trace_tag: str | None = None,
    correlation_id: str | None = None,
    parent_run_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List runs from the durable store with optional filters.

    `state` ∈ {pending, running, completed, failed, cancelled, orphaned}.
    `parent_run_id` filters to children of a specific parent — the
    primary way to reconstruct a multi-agent workflow's tree.
    """
    store = await get_store()
    since_dt = datetime.fromisoformat(since) if since else None
    runs = await store.list_runs(RunFilter(
        state=state, kind=kind, agent_id=agent_id,
        trace_tag=trace_tag, correlation_id=correlation_id,
        parent_run_id=parent_run_id,
        since=since_dt, limit=limit,
    ))
    return [_run_to_dict(r) for r in runs]


@app.get("/v1/runs/{run_id}/events")
async def list_run_events_endpoint(
    run_id: str, since_seq: int = 0, limit: int = 1000,
) -> list[dict]:
    """Paginated event timeline for a single run."""
    _validate_path_component(run_id, "run_id")
    store = await get_store()
    events = await store.list_events(run_id, since_seq=since_seq, limit=limit)
    return [_event_to_dict(e) for e in events]


@app.get("/v1/runs/{run_id}/events/stream")
async def stream_run_events_endpoint(run_id: str, request: Request):
    """SSE: live event feed for one run.

    Tails the run_events table — useful for dashboards rendering an active
    agent's progress. Streams every event as it's appended; for already-
    completed runs, simply yields the full backlog then closes.
    """
    _validate_path_component(run_id, "run_id")
    store = await get_store()
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    async def event_generator():
        last_seq = 0
        # Poll-based tail: cheap at our scale; LISTEN/NOTIFY is a Phase-9.5
        # upgrade for Postgres specifically. InMemoryStore polling works fine.
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            new = await store.list_events(run_id, since_seq=last_seq, limit=500)
            if new:
                for ev in new:
                    yield _sse_event(f"run.{ev.kind}", _event_to_dict(ev))
                    last_seq = max(last_seq, ev.seq)
                idle_ticks = 0
            else:
                idle_ticks += 1
            # If the run is terminal AND we've drained, close the stream.
            current = await store.get_run(run_id)
            if current and current.state in (
                "completed", "failed", "cancelled", "orphaned",
            ) and not new:
                break
            await asyncio.sleep(0.5)

    return _sse_response(event_generator())


@app.get("/v1/runs/active")
async def list_active_runs() -> dict:
    """List run_ids currently in-flight in this server process."""
    return {"active": sorted(_active_runs.keys())}


_TERMINAL_STATES_FOR_WAIT = frozenset({"completed", "failed", "cancelled", "orphaned"})


@app.post("/v1/runs/{run_id}/wait")
async def wait_for_run(
    run_id: str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> dict:
    """Block until a run reaches a terminal state, then return it.

    Convenience over manual polling: a consumer that submits an async
    run via `POST /v1/runs` and doesn't want to set up a webhook
    receiver can call this and get the final Run row back when it
    settles.

    Polls the store every `poll_interval` seconds (default 0.5s) up to
    `timeout` seconds (default 60s, max 600s). Returns the Run as
    soon as state ∈ {completed, failed, cancelled, orphaned}. Returns
    HTTP 408 if the run is still pending/running at deadline — the
    consumer can call again to keep waiting.

    Returns 404 if the run id doesn't exist. Returns the same Run
    shape as `GET /v1/runs/{id}` (no on-disk artifacts folded in;
    fetch separately if needed).
    """
    _validate_path_component(run_id, "run_id")
    if timeout <= 0 or timeout > 600:
        raise HTTPException(
            status_code=400,
            detail="timeout must be in (0, 600] seconds",
        )
    if poll_interval <= 0 or poll_interval > 10:
        raise HTTPException(
            status_code=400,
            detail="poll_interval must be in (0, 10] seconds",
        )

    store = await get_store()
    deadline = time.monotonic() + timeout
    while True:
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        if run.state in _TERMINAL_STATES_FOR_WAIT:
            return _run_to_dict(run)
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=408,
                detail=(
                    f"Run {run_id} still in state={run.state} after "
                    f"{timeout}s. Call again to keep waiting."
                ),
            )
        await asyncio.sleep(poll_interval)


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    """Signal cancellation for an in-flight run.

    Returns 404 if the run isn't currently active (already finished or
    never existed). The owning request will receive a result with
    `status: "error"`, `error_type: "Cancelled"`.
    """
    _validate_path_component(run_id, "run_id")
    task = _active_runs.get(run_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Run not active: {run_id}")
    task.cancel()
    return {"run_id": run_id, "cancelled": True}


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    """Fetch one run from the durable store. Same shape as `/v1/runs` list
    entries, plus on-disk artifacts (prompt, manifest) folded in when the
    run dir exists (agent runs with prepare/artifacts).
    """
    _validate_path_component(run_id, "run_id")
    store = await get_store()
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    body = _run_to_dict(run)

    # Best-effort: fold in on-disk artifacts (prompt.txt, manifest.json) if
    # the agent path wrote them. Defense-in-depth on the path so a crafted
    # run_id can't escape the runs/ root. `os.sep` suffix forces the prefix
    # check to span a directory boundary so `runs_evil/` can't match `runs/`.
    runs_base = Path(get_config().runs_dir).resolve()
    run_dir = (runs_base / run_id).resolve()
    base_prefix = str(runs_base) + os.sep
    if (str(run_dir) == str(runs_base) or
            str(run_dir).startswith(base_prefix)) and run_dir.exists():
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                body["manifest"] = json.loads(manifest_path.read_text())
            except json.JSONDecodeError:
                pass
        prompt_path = run_dir / "prompt.txt"
        if prompt_path.exists():
            body["prompt"] = prompt_path.read_text()

    return body


_KNOWN_LIMITATIONS = [
    "agent cost_usd is always null — only complete/embed track cost",
    "runs are purged on startup per [purge] run_retention_days "
    "(default 30); events and terminal webhook deliveries age out via "
    "the background purge worker",
]


@app.get("/v1/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "timestamp": _now_iso(),
        "known_limitations": _KNOWN_LIMITATIONS,
    }


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
    """`getrusage().ru_maxrss` is bytes on macOS/BSD, kilobytes on Linux.
    Best-effort detection by magnitude: any process actually using ≥1 GiB
    of RSS would report a kB-units number larger than the bytes-units
    threshold, but in practice aitelier sits in the tens-of-MB range so
    the units differ by 1024×. Heuristic: if raw < 10 MiB-of-kB
    (≈10 GiB), treat it as kB and multiply; else treat it as bytes."""
    import sys
    if sys.platform == "darwin":
        return int(raw)
    # Linux + most BSDs: kilobytes.
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
            agents = sorted(
                a["id"] if isinstance(a, dict) else a
                for a in raw
                if (isinstance(a, dict) and a.get("id")) or isinstance(a, str)
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


@app.get("/v1/schedules")
async def list_schedules_endpoint() -> list[dict]:
    """List persisted schedules."""
    from aitelier.schedules import list_schedules
    return await list_schedules()


@app.post("/v1/schedules")
async def create_schedule_endpoint(req: ScheduleRequest) -> dict:
    """Register a recurring or one-shot scheduled task."""
    from aitelier.schedules import create_schedule
    if req.webhook_url:
        await _check_webhook_url_or_die(req.webhook_url)
    try:
        return await create_schedule(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/v1/schedules/{schedule_id}")
async def get_schedule_endpoint(schedule_id: str) -> dict:
    _validate_path_component(schedule_id, "schedule_id")
    from aitelier.schedules import get_schedule
    entry = await get_schedule(schedule_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
    return entry


@app.delete("/v1/schedules/{schedule_id}")
async def delete_schedule_endpoint(schedule_id: str) -> dict:
    _validate_path_component(schedule_id, "schedule_id")
    from aitelier.schedules import delete_schedule
    if not await delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
    return {"id": schedule_id, "deleted": True}




# Workflow helpers (run_prepare, stop_sidecars, fetch_artifacts,
# prepare_failed_result) live in aitelier.sandbox_proxy and are imported
# at the top of this file.


# Sandbox-agent workflow helpers (run_prepare, stop_sidecars, fetch_artifacts,
# prepare_failed_result) are imported at the top of this file from
# aitelier.sandbox_proxy.


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
