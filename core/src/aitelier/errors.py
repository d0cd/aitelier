"""Error classification — maps Python exceptions to documented error types."""

from __future__ import annotations

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

    Known types: ProviderUnavailable, Timeout, SchemaViolation,
    RateLimited, AuthError, ProviderError, Cancelled.

    Falls through to the exception class name only as a last resort —
    the documented vocabulary should cover every observed failure mode
    on both the LLM and the agent (ACP) paths.
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
