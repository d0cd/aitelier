"""Helpers for receiving aitelier webhooks.

Aitelier authenticates every webhook delivery with a pre-shared bearer token:

    Authorization: Bearer <webhook_secret>

…when `[service] webhook_secret` is set on the server. Receivers verify by
comparing the presented token to their own copy of the secret in constant time.

The function below is the entire contract on the receiver side. We ship it so
consumers don't reinvent it (and don't accidentally use `==` instead of
`hmac.compare_digest`, which leaks timing information).
"""

from __future__ import annotations

import hmac

_BEARER_PREFIX = "Bearer "


def verify_webhook_bearer(authorization_header: str | None, secret: str) -> bool:
    """True iff `authorization_header` is exactly `Bearer <secret>`.

    `authorization_header` is the raw `Authorization` header value as received
    over the wire (e.g. `"Bearer s3cr3t"`). Pass `None` if the header is absent —
    the function returns False rather than raising, so receivers can branch on
    the return.

    Uses `hmac.compare_digest` for a timing-safe comparison: a wall-clock
    attacker can't reconstruct the secret byte-by-byte by measuring response
    time. Bearer (not an HMAC body signature) is aitelier's delivery-auth
    mechanism — see `docs/INTEGRATION.md` and the server's `webhook_worker`.
    """
    if not authorization_header or not authorization_header.startswith(_BEARER_PREFIX):
        return False
    token = authorization_header[len(_BEARER_PREFIX):]
    return hmac.compare_digest(token, secret)
