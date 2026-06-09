/**
 * Helpers for receiving aitelier webhooks.
 *
 * Aitelier signs every webhook delivery with:
 *
 *     X-Aitelier-Signature: sha256=<hex_hmac>
 *
 * …when `[service] webhook_secret` is set on the server. Receivers
 * verify by recomputing HMAC-SHA256 over the *raw request body bytes*
 * (not the parsed JSON — JSON re-serialization changes whitespace and
 * key order) and comparing to the header value in constant time.
 *
 * The function below is the entire contract on the receiver side. We
 * ship it so consumers don't reinvent it (and don't accidentally use
 * `===` instead of `crypto.timingSafeEqual`, which leaks timing
 * information).
 */
import { createHmac, timingSafeEqual } from "node:crypto";

const SIG_PREFIX = "sha256=";

/**
 * True iff `signatureHeader` matches HMAC-SHA256(body, secret).
 *
 * `body` must be the **raw request body bytes** as received over the
 * wire. Reading the body via a JSON parser and re-serializing breaks
 * the signature — Express needs `express.raw()` for the webhook
 * route; Fastify exposes `request.rawBody`; standalone HTTP servers
 * can buffer `req` chunks directly.
 *
 * Pass a `string` and we'll encode as UTF-8; pass a `Buffer` /
 * `Uint8Array` to skip the round-trip.
 *
 * `signatureHeader` is the raw `X-Aitelier-Signature` value
 * (e.g. `"sha256=abcd1234…"`). Pass `null`/`undefined` if the header
 * is absent — the function returns `false` rather than throwing.
 *
 * Uses `crypto.timingSafeEqual` for constant-time comparison so a
 * wall-clock attacker can't reconstruct the signature byte-by-byte
 * by measuring response time.
 */
export function verifyWebhookSignature(
  body: string | Buffer | Uint8Array,
  signatureHeader: string | null | undefined,
  secret: string,
): boolean {
  if (!signatureHeader || !signatureHeader.startsWith(SIG_PREFIX)) {
    return false;
  }
  const received = signatureHeader.slice(SIG_PREFIX.length);
  const bytes = typeof body === "string" ? Buffer.from(body, "utf8") : Buffer.from(body);
  const expected = createHmac("sha256", secret).update(bytes).digest("hex");
  // timingSafeEqual requires equal-length Buffers. Length mismatch is
  // already a non-match; convert both to Buffer of the same length to
  // avoid a thrown exception (which itself would be a side channel).
  const receivedBuf = Buffer.from(received, "hex");
  const expectedBuf = Buffer.from(expected, "hex");
  if (receivedBuf.length !== expectedBuf.length) {
    return false;
  }
  return timingSafeEqual(receivedBuf, expectedBuf);
}
