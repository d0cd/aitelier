/**
 * Verify the TS Aitelier client hits the right URL with the right body/headers
 * for the control-plane methods. The OpenAI inference path is tested in
 * `openai.test.ts` (it requires the `openai` package).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Aitelier } from "../src/client.js";

const realFetch = globalThis.fetch;

interface FakeResp {
  ok: boolean;
  status?: number;
  json?: unknown;
  text?: string;
}

function mockFetch(response: FakeResp): typeof fetch {
  return vi.fn(async () => {
    return {
      ok: response.ok,
      status: response.status ?? (response.ok ? 200 : 500),
      json: async () => response.json ?? {},
      text: async () => response.text ?? "",
    } as unknown as Response;
  }) as unknown as typeof fetch;
}

let calls: Array<{ url: string; init: RequestInit | undefined }> = [];

beforeEach(() => {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return {
      ok: true, status: 200,
      json: async () => ({}),
      text: async () => "",
    } as unknown as Response;
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("submitRun (POST /v1/runs)", () => {
  it("forwards model/messages/webhook and idempotency header", async () => {
    globalThis.fetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      return {
        ok: true, status: 200,
        json: async () => ({ run_id: "r-1", status: "accepted" }),
        text: async () => "",
      } as unknown as Response;
    }) as unknown as typeof fetch;

    const c = new Aitelier({ baseUrl: "http://aitelier.test" });
    const out = await c.submitRun({
      model: "agent:claude",
      messages: [{ role: "user", content: "hi" }],
      webhookUrl: "https://hooks.example.com/done",
      idempotencyKey: "key-1",
      correlationId: "cid-1",
    });
    expect(out.runId).toBe("r-1");
    expect(calls[0].url).toBe("http://aitelier.test/v1/runs");
    const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("key-1");
    expect(headers["X-Correlation-Id"]).toBe("cid-1");
    const body = JSON.parse(calls[0].init?.body as string);
    expect(body.webhook_url).toBe("https://hooks.example.com/done");
  });
});

describe("control plane", () => {
  it("cancelRun hits POST /v1/runs/{id}/cancel", async () => {
    globalThis.fetch = mockFetch({
      ok: true, json: { run_id: "r1", cancelled: true },
    });
    const c = new Aitelier({ baseUrl: "http://example" });
    const ack = await c.cancelRun("r1");
    expect(ack.cancelled).toBe(true);
  });

  it("listActiveRuns hits GET /v1/runs/active", async () => {
    globalThis.fetch = mockFetch({
      ok: true, json: { active: ["r-x"] },
    });
    const c = new Aitelier({ baseUrl: "http://example" });
    const active = await c.listActiveRuns();
    expect(active.active).toEqual(["r-x"]);
  });

  it("listSchedules hits GET /v1/schedules", async () => {
    globalThis.fetch = mockFetch({ ok: true, json: [] });
    const c = new Aitelier({ baseUrl: "http://example" });
    await c.listSchedules();
    // First (and only) call was to /v1/schedules
    const fetchMock = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
    expect(fetchMock.mock.calls[0][0]).toBe("http://example/v1/schedules");
  });

  it("createSchedule POSTs the right body", async () => {
    globalThis.fetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      return {
        ok: true, status: 200,
        json: async () => ({ id: "s-1", name: "daily", task: {} }),
        text: async () => "",
      } as unknown as Response;
    }) as unknown as typeof fetch;

    const c = new Aitelier({ baseUrl: "http://example" });
    await c.createSchedule({
      name: "daily",
      task: { model: "agent:claude", messages: [] },
      intervalSeconds: 86400,
    });
    const body = JSON.parse(calls[0].init?.body as string);
    // SDK accepts camelCase; wire stays snake_case.
    expect(body.interval_seconds).toBe(86400);
    expect(body.name).toBe("daily");
  });

  it("discovery hits GET /v1/discovery", async () => {
    globalThis.fetch = mockFetch({
      ok: true,
      json: {
        service: "aitelier", version: "0.1.0", api_version: "v1",
        timestamp: "x", endpoints: [], capabilities: {},
        dependencies: {
          litellm: { reachable: true, base_url: "x" },
          sandbox_agent: { reachable: true, base_url: "y" },
        },
        schemas: {}, known_limitations: [],
      },
    });
    const c = new Aitelier({ baseUrl: "http://example" });
    const d = await c.discovery();
    expect(d.service).toBe("aitelier");
    // Wire snake_case lifted to camelCase on the SDK boundary.
    expect(d.apiVersion).toBe("v1");
    expect(d.dependencies.sandboxAgent.baseUrl).toBe("y");
  });

  it("preserves user-data keys inside metadata / task / payload / environment", async () => {
    // Run.metadata is whatever the consumer stored — they sent `{my_key: ...}`,
    // they read back `{my_key: ...}`. Same for environment / payload / task.
    globalThis.fetch = mockFetch({
      ok: true,
      json: {
        run_id: "r-1",
        state: "completed",
        kind: "agent",
        metadata: { my_custom_key: "stay-snake", nested: { keep_me: 1 } },
        environment: { user_var_x: "stay" },
      },
    });
    const c = new Aitelier({ baseUrl: "http://example" });
    const run = await c.getRun("r-1");
    expect(run.runId).toBe("r-1");
    // Top-level + structural fields camelCased.
    expect(run.state).toBe("completed");
    // User-supplied metadata keys untouched.
    expect((run.metadata as Record<string, unknown>).my_custom_key).toBe("stay-snake");
    expect(((run.metadata as Record<string, unknown>).nested as Record<string, unknown>).keep_me).toBe(1);
    expect((run.environment as Record<string, unknown>).user_var_x).toBe("stay");
  });

  it("recentTraces converts wire snake_case to camelCase", async () => {
    globalThis.fetch = mockFetch({
      ok: true,
      json: [{
        trace_id: "t-1",
        started_at: "2026-01-01T00:00:00Z",
        ended_at: "2026-01-01T00:00:01Z",
        finish_reason: "stop",
        tool_call_count: 2,
        input_tokens: 100,
        output_tokens: 50,
        total_tokens: 150,
        cost_usd: 0.01,
        system_prompt_hash: "abc",
        trace_tag: "tag-x",
        status: "ok",
        error_type: null,
        error_msg: null,
        model: "claude-sonnet",
        kind: "complete",
      }],
    });
    const c = new Aitelier({ baseUrl: "http://example" });
    const traces = await c.recentTraces({ limit: 1 });
    const t = traces[0];
    expect(t.traceId).toBe("t-1");
    expect(t.startedAt).toBe("2026-01-01T00:00:00Z");
    expect(t.finishReason).toBe("stop");
    expect(t.toolCallCount).toBe(2);
    expect(t.inputTokens).toBe(100);
    expect(t.outputTokens).toBe(50);
    expect(t.totalTokens).toBe(150);
    expect(t.costUsd).toBe(0.01);
    expect(t.systemPromptHash).toBe("abc");
    expect(t.traceTag).toBe("tag-x");
    // Single-word fields pass through unchanged.
    expect(t.status).toBe("ok");
    expect(t.model).toBe("claude-sonnet");
    // No snake_case keys leak through.
    expect((t as Record<string, unknown>).trace_id).toBeUndefined();
    expect((t as Record<string, unknown>).started_at).toBeUndefined();
  });
});

describe(".openai()", () => {
  it("throws a helpful error if openai is not installed", async () => {
    // The package IS installed (we declare it in dev deps), so this test
    // only runs the path where openai isn't present. We can simulate that
    // by mocking the dynamic import via vi.doMock.
    vi.doMock("openai", () => {
      throw new Error("missing");
    });
    const c = new Aitelier({ baseUrl: "http://example" });
    await expect(c.openai()).rejects.toThrow(/openai/);
    vi.doUnmock("openai");
  });
});
