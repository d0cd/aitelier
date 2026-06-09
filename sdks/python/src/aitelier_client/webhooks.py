"""Helpers for receiving aitelier webhooks.

Aitelier signs every webhook delivery with:

    X-Aitelier-Signature: sha256=<hex_hmac>

…when `[service] webhook_secret` is set on the server. Receivers verify
by recomputing HMAC-SHA256 over the *raw request body bytes* (not the
parsed JSON — JSON re-serialization changes whitespace and key order)
and comparing to the header value in constant time.

The function below is the entire contract on the receiver side. It's
~20 lines of stdlib code; we ship it so consumers don't reinvent it
(and don't accidentally use `==` instead of `hmac.compare_digest`,
which leaks timing information).
"""

from __future__ import annotations

import hashlib
import hmac

_SIG_PREFIX = "sha256="


def verify_webhook_signature(
    body: bytes, signature_header: str | None, secret: str,
) -> bool:
    """True iff `signature_header` matches HMAC-SHA256(body, secret).

    `body` must be the **raw request body bytes** as received over the
    wire. Reading the request body via a JSON parser and re-serializing
    breaks the signature — every webhook framework exposes a raw-body
    accessor (FastAPI: `await request.body()`; Flask: `request.data`;
    Express: `raw-body` middleware).

    `signature_header` is the raw `X-Aitelier-Signature` header value
    (e.g. `"sha256=abcd1234…"`). Pass `None` if the header is absent —
    the function returns False rather than raising, so receivers can
    branch on the return.

    Uses `hmac.compare_digest` for timing-safe comparison: a wall-clock
    attacker can't reconstruct the signature byte-by-byte by measuring
    response time.
    """
    if not signature_header or not signature_header.startswith(_SIG_PREFIX):
        return False
    received = signature_header[len(_SIG_PREFIX):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)
