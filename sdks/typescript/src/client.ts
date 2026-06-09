/**
 * aitelier TypeScript SDK.
 *
 * aitelier speaks OpenAI shape for inference and an aitelier-native control
 * plane for runs/traces/schedules/discovery. The client splits along that
 * line:
 *
 *   - `client.openai()` → a preconfigured `OpenAI` instance (dynamic import;
 *     install `openai` as a peer dependency). Use it for chat completions,
 *     embeddings, models, streaming, structured outputs, retries.
 *
 *   - everything else on `Aitelier` is the control plane: submitRun (async
 *     agents), cancelRun, listRuns, listTraces, discovery, schedules.
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import type {
  ActiveRuns,
  CancelAck,
  CreateScheduleOpts,
  Discovery,
  HealthResponse,
  Run,
  RunEvent,
  Schedule,
  TraceRecord,
  TracesAggregate,
} from "./types.js";

// `openai` is an optional peer dependency. We `import type` only so consumers
// who don't install it still get a clean compile of the rest of the SDK.
import type { OpenAI } from "openai";

const DEFAULT_BASE_URL = "http://localhost:7777";

/**
 * Best-effort lookup of `[service] host`/`port` in ~/.config/aitelier/config.toml.
 * Returns undefined if the file doesn't exist, can't be read, or doesn't
 * declare a usable host+port. No env-var reads.
 */
function discoverBaseUrl(): string | undefined {
  try {
    const home = homedir();
    if (!home) return undefined;
    const cfgPath = join(home, ".config", "aitelier", "config.toml");
    if (!existsSync(cfgPath)) return undefined;
    const text = readFileSync(cfgPath, "utf8");
    const blockMatch = text.match(/\[service\]([\s\S]*?)(?:\n\[|\s*$)/);
    if (!blockMatch) return undefined;
    const block = blockMatch[1];
    const hostMatch = block.match(/^\s*host\s*=\s*["']([^"']+)["']/m);
    const portMatch = block.match(/^\s*port\s*=\s*(\d+)/m);
    if (hostMatch && portMatch) {
      return `http://${hostMatch[1]}:${portMatch[1]}`;
    }
  } catch {
    // node:fs unavailable (browser), permission denied, malformed TOML —
    // all roads lead to "use the default and let the consumer override".
  }
  return undefined;
}

export interface AitelierOptions {
  baseUrl?: string;
  apiKey?: string;
  /** Per-request timeout in milliseconds. Default 60s. */
  timeoutMs?: number;
}

/** @deprecated Use AitelierOptions. Kept as an alias for the 0.1.x typo. */
export type AtelierOptions = AitelierOptions;

export interface SubmitRunOpts {
  model: string;                       // must start with `agent:`
  messages: Array<Record<string, unknown>>;
  webhookUrl?: string;
  aitelier?: Record<string, unknown>;  // workspace, mcpServers, prepare, ...
  timeout?: number;                    // seconds — server-side limit
  idempotencyKey?: string;
  correlationId?: string;
}

export class Aitelier {
  readonly baseUrl: string;
  readonly apiKey?: string;
  readonly timeoutMs: number;
  // Cached OpenAI client; lazy and dynamic so consumers without `openai`
  // installed can still use the control plane.
  private _openai?: OpenAI;

  constructor(opts: AitelierOptions = {}) {
    this.baseUrl = (
      opts.baseUrl ?? discoverBaseUrl() ?? DEFAULT_BASE_URL
    ).replace(/\/$/, "");
    this.apiKey = opts.apiKey;
    this.timeoutMs = opts.timeoutMs ?? 60_000;
  }

  // --- OpenAI client for inference ----------------------------------------

  /**
   * Return a preconfigured OpenAI client pointed at this aitelier service.
   *
   * Requires the `openai` peer dependency. Use it for `chat.completions.create`,
   * `embeddings.create`, `models.list`, streaming, structured outputs,
   * tool-call semantics. Aitelier-specific options (`workspace`, MCP servers,
   * `prepare`, `artifacts`) ride in `extra_body.aitelier.*` and are accepted
   * only when `model` starts with `agent:`.
   */
  async openai(): Promise<OpenAI> {
    if (this._openai) return this._openai;
    let Ctor: new (init: Record<string, unknown>) => OpenAI;
    try {
      const mod = await import("openai");
      // openai v4+ exports OpenAI as named; older versions used default.
      const candidate = (mod as { OpenAI?: unknown; default?: unknown }).OpenAI
        ?? (mod as { default?: unknown }).default;
      if (typeof candidate !== "function") {
        throw new Error("Imported `openai` module did not export a constructor.");
      }
      Ctor = candidate as new (init: Record<string, unknown>) => OpenAI;
    } catch (err) {
      // ImportError vs malformed-package both surface here.
      if (err instanceof Error && err.message.includes("did not export")) {
        throw err;
      }
      throw new Error(
        "The `openai` package is required for Aitelier.openai(). " +
          "Install with `pnpm add openai` (or npm/yarn equivalent).",
      );
    }
    this._openai = new Ctor({
      baseURL: `${this.baseUrl}/v1`,
      apiKey: this.apiKey ?? "no-auth",
      timeout: this.timeoutMs,
    });
    return this._openai;
  }

  // --- HTTP helpers --------------------------------------------------------

  private authHeader(): Record<string, string> {
    return this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {};
  }

  private cidHeader(cid?: string): Record<string, string> {
    return cid ? { "X-Correlation-Id": cid } : {};
  }

  // --- Async agent runs ----------------------------------------------------

  /**
   * Submit an async agent run via POST /v1/runs.
   *
   * Returns immediately with `{run_id, status: "accepted"}`. The final
   * ChatCompletion (or error body) is delivered to `webhookUrl` when ready,
   * if provided; otherwise consumers poll `getRun(run_id)` or
   * `listRunEvents(run_id)`.
   */
  async submitRun(opts: SubmitRunOpts): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = {
      model: opts.model, messages: opts.messages,
    };
    if (opts.webhookUrl !== undefined) body.webhook_url = opts.webhookUrl;
    if (opts.aitelier !== undefined) body.aitelier = opts.aitelier;
    if (opts.timeout !== undefined) body.timeout = opts.timeout;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...this.authHeader(),
      ...this.cidHeader(opts.correlationId),
    };
    if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
    const resp = await fetch(`${this.baseUrl}/v1/runs`, {
      method: "POST",
      headers, body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`submitRun failed: ${resp.status} ${await resp.text()}`);
    return snakeToCamelDeep(await resp.json()) as Record<string, unknown>;
  }

  // --- Control plane: runs + events ----------------------------------------

  async getRun(runId: string): Promise<Run> {
    return this.getJson<Run>(`/v1/runs/${runId}`);
  }

  async listRuns(opts: {
    traceTag?: string; state?: string; correlationId?: string;
    parentRunId?: string; limit?: number;
  } = {}): Promise<Run[]> {
    const params = new URLSearchParams();
    params.set("limit", String(opts.limit ?? 50));
    if (opts.traceTag) params.set("trace_tag", opts.traceTag);
    if (opts.state) params.set("state", opts.state);
    if (opts.correlationId) params.set("correlation_id", opts.correlationId);
    if (opts.parentRunId) params.set("parent_run_id", opts.parentRunId);
    return this.getJson<Run[]>(`/v1/runs?${params}`);
  }

  /**
   * Block until `runId` reaches a terminal state; return the Run.
   *
   * Server-side polling — convenience over rolling your own loop when
   * you want submit-and-await without a webhook receiver. Throws on a
   * 408 response (run still pending/running at timeout — call again to
   * keep waiting) or 404 (unknown run id).
   */
  async waitForRun(runId: string, opts: {
    timeoutSeconds?: number; pollIntervalSeconds?: number;
  } = {}): Promise<Run> {
    const timeout = opts.timeoutSeconds ?? 60;
    const params = new URLSearchParams({
      timeout: String(timeout),
      poll_interval: String(opts.pollIntervalSeconds ?? 0.5),
    });
    const resp = await fetch(`${this.baseUrl}/v1/runs/${runId}/wait?${params}`, {
      method: "POST",
      headers: { ...this.authHeader() },
      signal: AbortSignal.timeout((timeout + 10) * 1000),
    });
    if (!resp.ok) {
      throw new Error(`waitForRun failed: ${resp.status} ${await resp.text()}`);
    }
    return snakeToCamelDeep(await resp.json()) as Run;
  }

  async listRunEvents(runId: string): Promise<RunEvent[]> {
    return this.getJson<RunEvent[]>(`/v1/runs/${runId}/events`);
  }

  /**
   * Stream run events as Server-Sent Events. Yields parsed
   * `{type, data}` records and closes when the server closes the
   * stream (terminal run drains the backlog, then closes; in-flight
   * runs stream until the run is terminal).
   *
   * Uses native `fetch` streaming. Cancellation: break out of the
   * async iteration and the underlying reader is released.
   */
  async *streamRunEvents(runId: string): AsyncIterator<{ type: string; data: unknown }> {
    const resp = await fetch(`${this.baseUrl}/v1/runs/${runId}/events/stream`, {
      headers: { ...this.authHeader(), Accept: "text/event-stream" },
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`streamRunEvents failed: ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) return;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let event = "message";
          let data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (data) {
            try { yield { type: event, data: JSON.parse(data) }; }
            catch { /* skip malformed frame */ }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  async listActiveRuns(): Promise<ActiveRuns> {
    return this.getJson<ActiveRuns>("/v1/runs/active");
  }

  async cancelRun(runId: string): Promise<CancelAck> {
    const resp = await fetch(`${this.baseUrl}/v1/runs/${runId}/cancel`, {
      method: "POST",
      headers: { ...this.authHeader() },
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`cancelRun failed: ${resp.status}`);
    return snakeToCamelDeep(await resp.json()) as CancelAck;
  }

  // --- Control plane: traces ----------------------------------------------

  async recentTraces(opts: {
    traceTag?: string; status?: string; since?: string; limit?: number;
  } = {}): Promise<TraceRecord[]> {
    const params = new URLSearchParams();
    params.set("limit", String(opts.limit ?? 50));
    if (opts.traceTag) params.set("trace_tag", opts.traceTag);
    if (opts.status) params.set("status", opts.status);
    if (opts.since) params.set("since", opts.since);
    return this.getJson<TraceRecord[]>(`/v1/traces?${params}`);
  }

  async getTrace(traceId: string): Promise<TraceRecord> {
    return this.getJson<TraceRecord>(`/v1/traces/${traceId}`);
  }

  async aggregateTraces(opts: {
    groupBy?: string; since?: string; traceTag?: string; limit?: number;
  } = {}): Promise<TracesAggregate> {
    const params = new URLSearchParams();
    params.set("group_by", opts.groupBy ?? "model");
    if (opts.since) params.set("since", opts.since);
    if (opts.traceTag) params.set("trace_tag", opts.traceTag);
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    return this.getJson<TracesAggregate>(`/v1/traces/aggregates?${params}`);
  }

  // --- Control plane: schedules -------------------------------------------

  async listSchedules(): Promise<Schedule[]> {
    return this.getJson<Schedule[]>("/v1/schedules");
  }

  async createSchedule(opts: CreateScheduleOpts): Promise<Schedule> {
    const resp = await fetch(`${this.baseUrl}/v1/schedules`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.authHeader() },
      body: JSON.stringify({
        name: opts.name,
        task: opts.task,
        interval_seconds: opts.intervalSeconds,
        at_iso: opts.atIso,
        webhook_url: opts.webhookUrl,
      }),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`createSchedule failed: ${resp.status}`);
    return snakeToCamelDeep(await resp.json()) as Schedule;
  }

  async getSchedule(scheduleId: string): Promise<Schedule> {
    return this.getJson<Schedule>(`/v1/schedules/${scheduleId}`);
  }

  async deleteSchedule(scheduleId: string): Promise<Record<string, unknown>> {
    const resp = await fetch(`${this.baseUrl}/v1/schedules/${scheduleId}`, {
      method: "DELETE",
      headers: { ...this.authHeader() },
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`deleteSchedule failed: ${resp.status}`);
    return snakeToCamelDeep(await resp.json()) as Record<string, unknown>;
  }

  // --- Discovery / meta ----------------------------------------------------

  async discovery(): Promise<Discovery> {
    return this.getJson<Discovery>("/v1/discovery");
  }

  async health(): Promise<HealthResponse> {
    return this.getJson<HealthResponse>("/v1/health");
  }

  async getSchema(name: string): Promise<Record<string, unknown>> {
    return this.getJson<Record<string, unknown>>(`/v1/schemas/${name}`);
  }

  // ------------------------------------------------------------------------

  private async getJson<T>(path: string): Promise<T> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      headers: { ...this.authHeader() },
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`GET ${path} failed: ${resp.status}`);
    return snakeToCamelDeep(await resp.json()) as T;
  }
}

// Wire ↔ SDK casing converter. Aitelier's HTTP responses are snake_case
// (lockstep with the Python SDK + JSON Schemas); the TS SDK exposes
// camelCase to match idiomatic JS. Conversion is keys-only and recursive
// — except inside user-data fields like `metadata`, `environment`,
// `payload`, and `task`, whose contents are opaque and must round-trip
// byte-for-byte. Without that carve-out a consumer who submits
// `{my_key: ...}` into metadata would read back `{myKey: ...}` and the
// SDK would have silently mutated their data.
const PRESERVE_VALUE_KEYS = new Set([
  "metadata",
  "environment",
  "payload",
  "task",
]);

function snakeToCamelDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(snakeToCamelDeep);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      const ck = snakeToCamelKey(k);
      out[ck] = PRESERVE_VALUE_KEYS.has(k) ? v : snakeToCamelDeep(v);
    }
    return out;
  }
  return value;
}

function snakeToCamelKey(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
}
