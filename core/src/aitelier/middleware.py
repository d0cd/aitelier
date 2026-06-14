"""HTTP middleware stack for the aitelier FastAPI app.

Four middlewares, registered in the order needed for the stack to
execute correctly (FastAPI runs the LAST-registered first):

  1. auth          (registered last, runs first)  — Bearer-token gate
  2. correlation   (registered third)             — X-Correlation-Id echo/mint
  3. body_size     (registered second)            — Content-Length cap
  4. rate_limit    (registered first, runs last)  — per-caller token bucket

`register_middleware(app)` from server.py wires all four. Each one is
a plain coroutine that takes (request, call_next) — no decorator magic
in this module, so the order of registration is explicit at the call site.

State held here (module-level):
  - `_rate_limit_buckets`     — LRU dict mapping caller key → (tokens, last_refill_at)
  - `_correlation_id_var`     — contextvar stamped on log records
"""

from __future__ import annotations

import contextvars
import hmac
import re
import time
import uuid
from collections import OrderedDict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aitelier.config import get_config

# ---------------------------------------------------------------------------
# Correlation ID — contextvar that the LogRecord factory reads on every log
# emit so structured output carries the request's correlation id. Set by
# the correlation middleware; reset on response.
# ---------------------------------------------------------------------------


_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aitelier_correlation_id", default="-",
)

_CORRELATION_ID_CHARSET = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def current_correlation_id() -> str:
    """Return the active correlation id (or '-' outside a request)."""
    return _correlation_id_var.get()


# ---------------------------------------------------------------------------
# Rate-limit state — module-level OrderedDict so we get LRU eviction for free.
# ---------------------------------------------------------------------------


_RATE_LIMIT_EXEMPT_PATHS = {"/v1/health"}
_RATE_LIMIT_BUCKET_CAP = 10_000

_rate_limit_buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()


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


# ---------------------------------------------------------------------------
# Auth state
# ---------------------------------------------------------------------------


_AUTH_EXEMPT_PATHS = {"/v1/health"}


# ---------------------------------------------------------------------------
# Middleware callables — registered by register_middleware(app)
# ---------------------------------------------------------------------------


async def rate_limit_middleware(request: Request, call_next):
    """Per-caller token bucket. Returns 429 with Retry-After when the
    bucket is empty. 0 = disabled (default). Excludes /v1/health.

    Bucket capacity equals the per-minute budget; the bucket refills
    linearly at budget/60 tokens per second. The bucket map is LRU-
    capped at _RATE_LIMIT_BUCKET_CAP entries so a caller cycling Bearer
    values can't grow it without bound."""
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


async def body_size_middleware(request: Request, call_next):
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


async def correlation_id_middleware(request: Request, call_next):
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


async def auth_middleware(request: Request, call_next):
    """Gate every /v1/* endpoint on Authorization: Bearer <api_key> *if*
    service.api_key is configured. When unset (default), no auth is enforced
    — preserves the localhost-trust model.

    /v1/health is always public so liveness probes (k8s, load balancers)
    can hit it without a token.
    """
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


def register_middleware(app: FastAPI) -> None:
    """Register all four middlewares in the order FastAPI needs.

    FastAPI runs middlewares in REVERSE registration order, so to get
    the desired logical flow (auth → correlation → body_size →
    rate_limit → handler), we register them in the opposite order
    (rate_limit first, auth last). Same order the original inline
    decorators in server.py used.
    """
    app.middleware("http")(rate_limit_middleware)
    app.middleware("http")(body_size_middleware)
    app.middleware("http")(correlation_id_middleware)
    app.middleware("http")(auth_middleware)
