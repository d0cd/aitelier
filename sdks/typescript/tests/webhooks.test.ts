/**
 * Tests for the webhook-signature verification helper.
 */
import { createHmac } from "node:crypto";

import { describe, expect, it } from "vitest";

import { verifyWebhookSignature } from "../src/webhooks.js";

function sign(body: Buffer | string, secret: string): string {
  const bytes = typeof body === "string" ? Buffer.from(body, "utf8") : body;
  return "sha256=" + createHmac("sha256", secret).update(bytes).digest("hex");
}

describe("verifyWebhookSignature", () => {
  const secret = "supersecret";

  it("verifies a valid signature", () => {
    const body = '{"run_id":"r-1","status":"completed"}';
    expect(verifyWebhookSignature(body, sign(body, secret), secret)).toBe(true);
  });

  it("verifies Buffer bodies (skips utf-8 round-trip)", () => {
    const body = Buffer.from('{"hello":"world"}', "utf8");
    expect(verifyWebhookSignature(body, sign(body, secret), secret)).toBe(true);
  });

  it("rejects a tampered body", () => {
    const original = '{"run_id":"r-1","status":"completed"}';
    const tampered = '{"run_id":"r-1","status":"FAILED"}';
    expect(verifyWebhookSignature(tampered, sign(original, secret), secret)).toBe(false);
  });

  it("rejects the wrong secret", () => {
    const body = "{}";
    expect(verifyWebhookSignature(body, sign(body, "right"), "wrong")).toBe(false);
  });

  it("returns false (does not throw) on missing header", () => {
    expect(verifyWebhookSignature("anything", null, "s")).toBe(false);
    expect(verifyWebhookSignature("anything", undefined, "s")).toBe(false);
  });

  it("rejects an unknown signature scheme prefix", () => {
    // Forge an md5 prefix to test the scheme guard.
    const body = "{}";
    const forged = "md5=" + createHmac("md5", "x").update(body).digest("hex");
    expect(verifyWebhookSignature(body, forged, secret)).toBe(false);
  });

  it("handles a malformed hex signature without throwing", () => {
    // Length mismatch path: Buffer.from('ZZZZ', 'hex') yields empty.
    expect(verifyWebhookSignature("{}", "sha256=not-hex", secret)).toBe(false);
  });
});
