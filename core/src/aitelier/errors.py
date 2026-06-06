"""Error classification — maps Python exceptions to documented error types."""

from __future__ import annotations

import httpx

# Maps Python exception class names to consumer-facing error types
_ERROR_MAP: dict[str, str] = {
    "ConnectError": "ProviderUnavailable",
    "ConnectionError": "ProviderUnavailable",
    "OSError": "ProviderUnavailable",
    "TimeoutException": "Timeout",
    "TimeoutError": "Timeout",
    "JSONDecodeError": "SchemaViolation",
    "ValidationError": "SchemaViolation",
    "CancelledError": "Cancelled",
}


def classify_error(exc: Exception) -> str:
    """Classify an exception into a documented error type.

    Known types: ProviderUnavailable, Timeout, SchemaViolation,
    RateLimited, AuthError, ProviderError.

    Unknown exceptions pass through as their class name.
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

    cls_name = type(exc).__name__
    return _ERROR_MAP.get(cls_name, cls_name)
