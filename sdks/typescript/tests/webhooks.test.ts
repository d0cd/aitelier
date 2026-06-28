/**
 * Tests for the webhook bearer-token verification helper.
 */
import { describe, expect, it } from "vitest";

import { verifyWebhookBearer } from "../src/webhooks.js";

function authHeader(secret: string): string {
  return `Bearer ${secret}`;
}

describe("verifyWebhookBearer", () => {
  const secret = "supersecret";

  it("verifies a valid bearer token", () => {
    expect(verifyWebhookBearer(authHeader(secret), secret)).toBe(true);
  });

  it("rejects the wrong secret", () => {
    expect(verifyWebhookBearer(authHeader("right"), "wrong")).toBe(false);
  });

  it("returns false (does not throw) on a missing header", () => {
    expect(verifyWebhookBearer(null, "s")).toBe(false);
    expect(verifyWebhookBearer(undefined, "s")).toBe(false);
  });

  it("rejects a non-Bearer scheme", () => {
    expect(verifyWebhookBearer("Basic c3VwZXJzZWNyZXQ=", secret)).toBe(false);
  });

  it("rejects the bare token without the Bearer prefix", () => {
    expect(verifyWebhookBearer(secret, secret)).toBe(false);
  });
});
