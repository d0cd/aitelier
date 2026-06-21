"""Error classification — maps Python exceptions to documented error types."""

from __future__ import annotations

import collections
import math
import re

import httpx

# Maps Python exception class names to consumer-facing error types
_ERROR_MAP: dict[str, str] = {
    # Connect-side: server unreachable, refused, network down.
    "ConnectError": "ProviderUnavailable",
    "ConnectionError": "ProviderUnavailable",
    "ConnectTimeout": "ProviderUnavailable",
    "OSError": "ProviderUnavailable",
    "PoolTimeout": "ProviderUnavailable",
    # Mid-call connection failures (peer dropped the socket / protocol break).
    # Distinct from a plain timeout — the server was reachable but the
    # exchange didn't complete. Treat as transient/unavailable so SDK retry
    # policies can re-attempt.
    "RemoteProtocolError": "ProviderUnavailable",
    "LocalProtocolError": "ProviderUnavailable",
    # NetworkError leaves (httpx) — peer socket drop mid-read/write. Siblings of
    # ConnectError under TransportError; matched by their own class name since
    # _ERROR_MAP keys on the concrete type, not the base.
    "ReadError": "ProviderUnavailable",
    "WriteError": "ProviderUnavailable",
    "NetworkError": "ProviderUnavailable",
    "CloseError": "ProviderUnavailable",
    # Read / write timeout once the connection was established.
    "TimeoutException": "Timeout",
    "TimeoutError": "Timeout",
    "ReadTimeout": "Timeout",
    "WriteTimeout": "Timeout",
    # Body / params couldn't be parsed.
    "JSONDecodeError": "SchemaViolation",
    "ValidationError": "SchemaViolation",
    # Task-level cancellation (consumer disconnect, /v1/runs/{id}/cancel).
    "CancelledError": "Cancelled",
}


# Lowercased substrings that mark a wrapped-error message as a rate-limit
# signal. Necessary because the agent path stringifies the inner error
# multiple times (Anthropic → Claude SDK subprocess → ACP JSON-RPC
# `-32603 Internal error` → aitelier `RuntimeError`), losing the original
# exception type along the way. Pattern-match the surviving prose.
_RATE_LIMIT_MARKERS = (
    "rate limited",
    "rate_limit",
    "rate limit exceeded",
    "too many requests",
    "temporarily limiting requests",
    " 429 ",
    " 429:",
    " 429\n",
    "http 429",
    "status: 429",
    "status 429",
)


def _looks_like_rate_limit(message: str) -> bool:
    """True when an error message — regardless of the carrying exception
    type — smells like a rate-limit signal from a wrapped downstream."""
    if not message:
        return False
    lower = message.lower()
    return any(marker in lower for marker in _RATE_LIMIT_MARKERS)


# HTTP status codes embedded in tunneled error messages (ACP `-32603` or
# httpx wrappers strip the structured exception type). Map by status
# class to the documented vocabulary. We list the verbose forms because
# `_looks_like_*` matches case-insensitively against the raw message.
_HTTP_STATUS_MARKERS: tuple[tuple[str, str], ...] = (
    ("401 unauthorized", "AuthError"),
    ("403 forbidden", "AuthError"),
    (" 401 ", "AuthError"),
    (" 403 ", "AuthError"),
    ("502 bad gateway", "ProviderUnavailable"),
    ("503 service unavailable", "ProviderUnavailable"),
    ("504 gateway timeout", "Timeout"),
    (" 502 ", "ProviderUnavailable"),
    (" 503 ", "ProviderUnavailable"),
    (" 504 ", "Timeout"),
    # 4xx other than auth/rate-limit → caller-side problem upstream.
    ("400 bad request", "ProviderError"),
    ("404 not found", "ProviderError"),
    ("422 unprocessable", "ProviderError"),
    # Generic 5xx fallback (after the specific cases above).
    ("500 internal server error", "ProviderError"),
)


def _classify_by_http_status(message: str) -> str | None:
    """If the message text contains a recognizable HTTP status, return
    the corresponding error_type. None means no match."""
    if not message:
        return None
    lower = message.lower()
    for marker, kind in _HTTP_STATUS_MARKERS:
        if marker in lower:
            return kind
    return None


# JSON-RPC error class from `providers.sandbox_agent.AcpError`. When the
# inner agent's exception tunnels through ACP, only the message survives;
# pattern-matching above handles the common cases, but a bare AcpError
# without recognizable text should still resolve to a documented type.
_RPC_ERROR_CLASS_NAMES = {"AcpError"}


def classify_error(exc: Exception) -> str:
    """Classify an exception into a documented error type.

    Types resolved here: ProviderUnavailable, Timeout, SchemaViolation,
    RateLimited, AuthError, ProviderError, Cancelled.

    Other documented types are set directly at their call sites, not by
    this function: PrepareFailed (prepare phase), UnsupportedResponseFormat
    (response_format gate), Orphaned (startup orphan sweep). The full
    consumer-facing table lives in docs/INTEGRATION.md → "Error handling".

    Falls through to the exception class name only as a last resort.
    """
    # HTTP status errors get special handling by status code
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return "RateLimited"
        elif code in (401, 403):
            return "AuthError"
        else:
            return "ProviderError"

    message = str(exc)

    # Message-level rate-limit detection for tunneled errors.
    if _looks_like_rate_limit(message):
        return "RateLimited"

    # Message-level HTTP status detection — same problem class as rate
    # limits: the original exception type is lost through ACP / SDK
    # wrappers, but the prose retains the status code.
    by_status = _classify_by_http_status(message)
    if by_status is not None:
        return by_status

    cls_name = type(exc).__name__

    # Last resort: known wrapper classes that don't reveal an HTTP code.
    # AcpError is a generic JSON-RPC failure — treat as ProviderError so
    # consumers don't see the raw class name.
    if cls_name in _RPC_ERROR_CLASS_NAMES:
        return "ProviderError"

    return _ERROR_MAP.get(cls_name, cls_name)


# Patterns that match credentials embedded in free-form error text.
# Order matters: more specific patterns first so the generic
# `Bearer <token>` match doesn't accidentally swallow a structured one.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # `Authorization: Bearer <token>` (header echoed back in errors)
    (
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._\-+/=]+"),
        r"\1[redacted]",
    ),
    # Bare `Bearer <token>` (Anthropic/OpenAI error envelopes often quote
    # the Authorization header value without the key).
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{16,}"),
        "Bearer [redacted]",
    ),
    # Query-string credentials (`?api_key=...`, `&token=...`, `&password=...`)
    # — URLs sometimes appear in upstream error envelopes.
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret|password|auth)=)[^&\s\"']+"
        ),
        r"\1[redacted]",
    ),
    # JSON-ish field=value pairs that name a credential explicitly
    # (e.g., `'token': 'sk-...'` inside a stringified dict).
    (
        re.compile(
            r"(?i)(['\"](?:api[_-]?key|access[_-]?token|token|secret|password)['\"]\s*[:=]\s*['\"])[^'\"]+(['\"])"
        ),
        r"\1[redacted]\2",
    ),
    # Basic-auth URLs embedded in error prose: `scheme://user:password@host/...`
    # — upstream proxies and database driver errors echo full DSNs
    # (`postgresql://`, `redis://`, `https://`). Keep scheme + host for
    # context; redact userinfo.
    (
        re.compile(
            r"(?i)([a-z][a-z0-9+.\-]*://)([^:/?#\s\"']+):([^@/?#\s\"']+)(@)"
        ),
        r"\1[redacted]:[redacted]\4",
    ),
)


def scrub_error_text(message: str) -> str:
    """Redact credential-shaped substrings from free-form error text.

    `error_msg` is captured from `str(exc)` and persisted in
    `runs.error_msg` + surfaced in API responses and webhook payloads.
    When the underlying exception is a wrapped upstream error
    (Anthropic 401, ACP `-32603 Internal error`, httpx response body),
    the message can carry the OAuth bearer or API key that triggered
    the failure. Structured redaction (`_redact_secrets` on dict/list)
    doesn't help here because the secret is inside a string.

    Conservative: only redacts shapes that are unambiguously credentials
    (`Bearer <jwt>`, `?api_key=…`, `'token': '…'`). Doesn't try to
    pattern-match prefixed keys (`sk-…`, `eyJ…`) — too aggressive, and
    would mangle legitimate non-secret content.
    """
    if not message:
        return message
    out = message
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


# ---------------------------------------------------------------------------
# Aggressive recall net for UPSTREAM PROVIDER bodies (scrub_upstream_body).
#
# scrub_error_text (above) is deliberately conservative — it only masks
# *named* credentials, to avoid mangling general error text (paths, SKUs,
# tracebacks). Upstream provider error bodies are a narrower, riskier case:
# they're surfaced to the consumer + persisted to runs.error_msg, and a cloud
# provider's body can carry an unprefixed/unenumerated key fragment, org id,
# or session token in free prose that the named patterns miss. So for those
# bodies specifically we add a token-shape + Shannon-entropy recall net,
# adapted from brig's warden_addons/_common.py. It is heuristic — imperfect
# recall, some over-redaction — and is meant to be monitored (the full body
# is still logged at WARNING for operators) and tuned over time.
# ---------------------------------------------------------------------------

_ENTROPY_MIN_LEN = 16
_ENTROPY_MIN_BITS = 4.0

# A token-ish run in free text: starts alnum, ≥12 chars of credential charset.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-+/=.]{11,}")

# Audit ids — high-cardinality but NOT secret; keep them so a consumer can
# still read the trace/request id out of an error. Critically includes
# aitelier's own run_id/trace_id (32 lowercase hex) — redacting that would
# defeat the whole point of surfacing the error.
_AUDIT_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                         r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_AUDIT_RUN_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_ALL_DIGITS = re.compile(r"\A[0-9]+\Z")

# Secret shapes (whole-run): long hex, mixed-alnum token (key/JWT-segment).
_LONG_HEX = re.compile(r"\A[0-9a-fA-F]{16,}\Z")
_MIXED_TOKEN = re.compile(r"\A(?=[^.]*[A-Za-z])(?=[^.]*[0-9])[A-Za-z0-9_\-]{20,}\Z")


def _shannon_entropy(s: str) -> float:
    n = len(s)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n)
                for c in collections.Counter(s).values())


def _looks_secret(token: str) -> bool:
    """Heuristic: does this free-text token look like a credential? Keeps
    audit ids (numeric / uuid / 32-hex run_id); redacts long hex, mixed-alnum
    tokens, and high-entropy strings."""
    if _ALL_DIGITS.match(token) or _AUDIT_UUID.match(token) or _AUDIT_RUN_ID.match(token):
        return False
    if _LONG_HEX.match(token) or _MIXED_TOKEN.match(token):
        return True
    return (len(token) >= _ENTROPY_MIN_LEN
            and _shannon_entropy(token) >= _ENTROPY_MIN_BITS)


def scrub_upstream_body(body: str) -> str:
    """Scrub an upstream provider error body for surfacing to consumers +
    persisting to `runs.error_msg`. Runs the conservative named-credential
    patterns first, then the token-shape/entropy recall net for unenumerated
    secrets. Heuristic by design — monitor the surfaced output and tune
    `_looks_secret` over time. The unredacted body stays in the WARNING log
    for operator review."""
    if not body:
        return body
    out = scrub_error_text(body)
    return _TOKEN_RUN.sub(
        lambda m: "[redacted]" if _looks_secret(m.group(0)) else m.group(0),
        out,
    )
