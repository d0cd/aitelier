/**
 * Helpers for receiving aitelier webhooks.
 *
 * Aitelier authenticates every webhook delivery with a pre-shared bearer
 * token:
 *
 *     Authorization: Bearer <webhook_secret>
 *
 * …when `[service] webhook_secret` is set on the server. Receivers verify by
 * comparing the presented token to their own copy of the secret in constant
 * time.
 *
 * We ship this so consumers don't reinvent it (and don't accidentally use
 * `===` instead of `crypto.timingSafeEqual`, which leaks timing information).
 */
import { timingSafeEqual } from "node:crypto";

const BEARER_PREFIX = "Bearer ";

/**
 * True iff `authorizationHeader` is exactly `Bearer <secret>`.
 *
 * `authorizationHeader` is the raw `Authorization` header value as received
 * (e.g. `"Bearer s3cr3t"`). Pass `null`/`undefined` if the header is absent —
 * the function returns `false` rather than throwing.
 *
 * Uses `crypto.timingSafeEqual` for constant-time comparison so a wall-clock
 * attacker can't reconstruct the secret byte-by-byte by measuring response
 * time. Bearer (not an HMAC body signature) is aitelier's delivery-auth
 * mechanism — see `docs/INTEGRATION.md`.
 */
export function verifyWebhookBearer(
  authorizationHeader: string | null | undefined,
  secret: string,
): boolean {
  if (!authorizationHeader || !authorizationHeader.startsWith(BEARER_PREFIX)) {
    return false;
  }
  const token = Buffer.from(authorizationHeader.slice(BEARER_PREFIX.length), "utf8");
  const expected = Buffer.from(secret, "utf8");
  // timingSafeEqual requires equal-length Buffers. A length mismatch is
  // already a non-match; short-circuit to avoid a thrown exception (which
  // would itself be a side channel).
  if (token.length !== expected.length) {
    return false;
  }
  return timingSafeEqual(token, expected);
}
