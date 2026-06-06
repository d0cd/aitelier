/**
 * Async HTTP client for the aitelier service.
 */

import type {
  ActiveRuns,
  CancelAck,
  CompleteOpts,
  CompleteStreamEvent,
  Discovery,
  EmbedOpts,
  HealthResponse,
  Result,
  RunAgentOpts,
  TaskSpec,
  TraceRecord,
} from "./_generated/types.js";

/** Convert camelCase to snake_case for the wire format. */
function toSnake(obj: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    if (value === undefined || value === null) continue;
    const snakeKey = key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
    result[snakeKey] = value;
  }
  return result;
}

/** Convert snake_case result from wire to camelCase Result. */
function fromWireResult(data: Record<string, unknown>): Result {
  return {
    kind: data.kind as Result["kind"],
    provider: data.provider as string,
    status: data.status as Result["status"],
    durationS: data.duration_s as number,
    runId: data.run_id as string,
    traceId: (data.trace_id as string) ?? undefined,
    content: (data.content as string | null) ?? undefined,
    parsed: data.parsed ?? undefined,
    usage: data.usage
      ? {
          inputTokens: (data.usage as any).input_tokens ?? 0,
          outputTokens: (data.usage as any).output_tokens ?? 0,
          totalTokens: (data.usage as any).total_tokens ?? 0,
        }
      : undefined,
    finishReason: (data.finish_reason as Result["finishReason"]) ?? undefined,
    costUsd: (data.cost_usd as number | null) ?? undefined,
    embeddings: (data.embeddings as number[][] | null) ?? undefined,
    dimensions: (data.dimensions as number | null) ?? undefined,
    toolCalls: data.tool_calls
      ? (data.tool_calls as any[]).map((tc) => ({
          server: tc.server,
          tool: tc.tool,
          input: tc.input,
          output: tc.output,
          elapsedMs: tc.elapsed_ms,
        }))
      : undefined,
    sessionId: (data.session_id as string | null) ?? undefined,
    filesChanged: (data.files_changed as string[] | null) ?? undefined,
    errorType: (data.error_type as string | null) ?? undefined,
    errorMsg: (data.error_msg as string | null) ?? undefined,
  };
}

function fromWireTrace(data: Record<string, unknown>): TraceRecord {
  return {
    traceId: data.trace_id as string,
    startedAt: data.started_at as string,
    endedAt: (data.ended_at as string) ?? undefined,
    model: (data.model as string) ?? undefined,
    kind: (data.kind as string) ?? undefined,
    finishReason: (data.finish_reason as string) ?? undefined,
    toolCallCount: (data.tool_call_count as number) ?? 0,
    inputTokens: (data.input_tokens as number) ?? 0,
    outputTokens: (data.output_tokens as number) ?? 0,
    totalTokens: (data.total_tokens as number) ?? 0,
    costUsd: (data.cost_usd as number) ?? undefined,
    systemPromptHash: (data.system_prompt_hash as string) ?? undefined,
    traceTag: (data.trace_tag as string) ?? undefined,
    status: (data.status as string) ?? undefined,
  };
}

export interface AtelierOptions {
  baseUrl?: string;
  timeout?: number;
  /** Optional default X-Correlation-Id sent with every request. */
  defaultCorrelationId?: string;
  /** API key for hosted-mode aitelier (sent as Authorization: Bearer …). */
  apiKey?: string;
}

/** Per-call request options (transport-level, not schema). */
export interface RequestOpts {
  /** Override the client's default correlation ID for this call. */
  correlationId?: string;
}

export class Aitelier {
  private baseUrl: string;
  private timeout: number;
  private defaultCorrelationId: string | undefined;
  private apiKey: string | undefined;

  constructor(options: AtelierOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://localhost:7777").replace(
      /\/$/,
      ""
    );
    this.timeout = options.timeout ?? 600_000;
    this.defaultCorrelationId = options.defaultCorrelationId;
    this.apiKey = options.apiKey;
  }

  private cidHeader(correlationId: string | undefined): Record<string, string> {
    const cid = correlationId ?? this.defaultCorrelationId;
    const headers: Record<string, string> = {};
    if (cid) headers["X-Correlation-Id"] = cid;
    if (this.apiKey) headers["Authorization"] = `Bearer ${this.apiKey}`;
    return headers;
  }

  /** Auth header for endpoints that don't go through cidHeader (GET methods). */
  private authHeader(): Record<string, string> {
    return this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {};
  }

  // --- Primitives (deepread contract) ---

  async complete(opts: CompleteOpts, reqOpts?: RequestOpts): Promise<Result> {
    const body = toSnake({
      model: opts.model,
      systemPrompt: opts.systemPrompt,
      messages: opts.messages,
      temperature: opts.temperature,
      maxTokens: opts.maxTokens,
      responseFormat: opts.responseFormat,
      timeout: opts.timeoutMs ? Math.ceil(opts.timeoutMs / 1000) : undefined,
      traceTag: opts.traceTag,
    });
    const resp = await fetch(`${this.baseUrl}/v1/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.cidHeader(reqOpts?.correlationId) },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`complete failed: ${resp.status}`);
    return fromWireResult(await resp.json());
  }

  /**
   * Streaming chat completion (SSE).
   * Yields events `complete.delta`, `complete.done`, or `complete.error`.
   */
  async *completeStream(
    opts: CompleteOpts,
    reqOpts?: RequestOpts,
  ): AsyncIterable<{ type: string; data: Record<string, unknown> }> {
    const body = toSnake({
      model: opts.model,
      systemPrompt: opts.systemPrompt,
      messages: opts.messages,
      temperature: opts.temperature,
      maxTokens: opts.maxTokens,
      responseFormat: opts.responseFormat,
      timeout: opts.timeoutMs ? Math.ceil(opts.timeoutMs / 1000) : undefined,
      traceTag: opts.traceTag,
    });
    const resp = await fetch(`${this.baseUrl}/v1/complete/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.cidHeader(reqOpts?.correlationId) },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`completeStream failed: ${resp.status}`);
    if (!resp.body) throw new Error("No response body");

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventType = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith("data: ") && eventType) {
          try {
            yield { type: eventType, data: JSON.parse(line.slice(6)) };
          } catch {
            // skip malformed
          }
          eventType = "";
        }
      }
    }
  }

  async embed(opts: EmbedOpts, reqOpts?: RequestOpts): Promise<Result> {
    const body = toSnake({
      texts: opts.texts,
      model: opts.model,
      timeout: opts.timeoutMs ? Math.ceil(opts.timeoutMs / 1000) : undefined,
    });
    const resp = await fetch(`${this.baseUrl}/v1/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.cidHeader(reqOpts?.correlationId) },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`embed failed: ${resp.status}`);
    return fromWireResult(await resp.json());
  }

  async runAgent(opts: RunAgentOpts, reqOpts?: RequestOpts): Promise<Result> {
    const body = toSnake({
      model: opts.model,
      systemPrompt: opts.systemPrompt,
      initialMessage: opts.initialMessage,
      examples: opts.examples,
      mcpServers: opts.mcpServers?.map((s) => toSnake(s as any)),
      toolAllowlist: opts.toolAllowlist,
      responseFormat: opts.responseFormat,
      maxTurns: opts.maxTurns,
      timeout: opts.timeoutMs ? Math.ceil(opts.timeoutMs / 1000) : undefined,
      workspace: opts.workspace,
      workspaceMode: opts.workspaceMode,
      traceTag: opts.traceTag,
      metadata: opts.metadata,
    });
    const resp = await fetch(`${this.baseUrl}/v1/agent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.cidHeader(reqOpts?.correlationId) },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`runAgent failed: ${resp.status}`);
    return fromWireResult(await resp.json());
  }

  async recentTraces(filter?: {
    since?: string;
    traceTag?: string;
    status?: string;
    limit?: number;
  }): Promise<TraceRecord[]> {
    const params = new URLSearchParams();
    if (filter?.since) params.set("since", filter.since);
    if (filter?.traceTag) params.set("trace_tag", filter.traceTag);
    if (filter?.status) params.set("status", filter.status);
    if (filter?.limit) params.set("limit", String(filter.limit));

    const url = `${this.baseUrl}/v1/traces${params.toString() ? "?" + params : ""}`;
    const resp = await fetch(url, {
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`recentTraces failed: ${resp.status}`);
    const data = await resp.json();
    return (data as Record<string, unknown>[]).map(fromWireTrace);
  }

  // --- Task runner endpoints (legacy/fan-out) ---

  async execute(task: TaskSpec): Promise<Result> {
    const resp = await fetch(`${this.baseUrl}/v1/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(toSnake(task as any)),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`execute failed: ${resp.status}`);
    return fromWireResult(await resp.json());
  }

  async *executeStream(
    task: TaskSpec
  ): AsyncIterable<{ type: string; data: Record<string, unknown> }> {
    const resp = await fetch(`${this.baseUrl}/v1/execute/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(toSnake(task as any)),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`executeStream failed: ${resp.status}`);
    if (!resp.body) throw new Error("No response body");

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      let eventType = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith("data: ") && eventType) {
          try {
            const data = JSON.parse(line.slice(6));
            yield { type: eventType, data };
          } catch {
            // skip malformed
          }
          eventType = "";
        }
      }
    }
  }

  async fanOut(
    task: TaskSpec,
    options: { providers: string[]; maxConcurrent?: number }
  ): Promise<Result[]> {
    const body = {
      task: toSnake(task as any),
      providers: options.providers,
      max_concurrent: options.maxConcurrent ?? 4,
    };
    const resp = await fetch(`${this.baseUrl}/v1/fanout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`fanOut failed: ${resp.status}`);
    return ((await resp.json()) as Record<string, unknown>[]).map(
      fromWireResult
    );
  }

  async getRun(runId: string): Promise<Record<string, unknown>> {
    const resp = await fetch(`${this.baseUrl}/v1/runs/${runId}`, {
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`getRun failed: ${resp.status}`);
    return resp.json();
  }

  async health(): Promise<HealthResponse> {
    const resp = await fetch(`${this.baseUrl}/v1/health`, {
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`health failed: ${resp.status}`);
    const data = await resp.json();
    return {
      status: data.status,
      version: data.version,
      timestamp: data.timestamp,
    };
  }

  async aggregateTraces(opts: {
    groupBy?: "trace_tag" | "kind" | "model" | "status" | "error_type" | "day";
    since?: string;
    until?: string;
    traceTag?: string;
  } = {}): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (opts.groupBy) params.set("group_by", opts.groupBy);
    if (opts.since) params.set("since", opts.since);
    if (opts.until) params.set("until", opts.until);
    if (opts.traceTag) params.set("trace_tag", opts.traceTag);
    const url = `${this.baseUrl}/v1/traces/aggregates${params.toString() ? "?" + params : ""}`;
    const resp = await fetch(url, {
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`aggregateTraces failed: ${resp.status}`);
    return resp.json();
  }

  // --- Cancellation ---

  async listActiveRuns(): Promise<ActiveRuns> {
    const resp = await fetch(`${this.baseUrl}/v1/runs/active`, {
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`listActiveRuns failed: ${resp.status}`);
    const data = (await resp.json()) as { active?: string[] };
    return { active: data.active ?? [] };
  }

  async cancelRun(runId: string): Promise<CancelAck> {
    const resp = await fetch(`${this.baseUrl}/v1/runs/${runId}/cancel`, {
      method: "POST",
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`cancelRun failed: ${resp.status}`);
    const data = (await resp.json()) as { run_id: string; cancelled: boolean };
    return { runId: data.run_id, cancelled: data.cancelled };
  }

  /**
   * Streaming agent run (SSE).
   * Yields events `agent.delta` / `agent.tool_call` / `agent.tool_result` /
   * `agent.done` / `agent.error`.
   */
  async *runAgentStream(
    opts: RunAgentOpts,
    reqOpts?: RequestOpts,
  ): AsyncIterable<{ type: string; data: Record<string, unknown> }> {
    const body = toSnake({
      model: opts.model,
      systemPrompt: opts.systemPrompt,
      initialMessage: opts.initialMessage,
      examples: opts.examples,
      mcpServers: opts.mcpServers?.map((s) => toSnake(s as any)),
      toolAllowlist: opts.toolAllowlist,
      responseFormat: opts.responseFormat,
      maxTurns: opts.maxTurns,
      timeout: opts.timeoutMs ? Math.ceil(opts.timeoutMs / 1000) : undefined,
      workspace: opts.workspace,
      workspaceMode: opts.workspaceMode,
      traceTag: opts.traceTag,
      metadata: opts.metadata,
    });
    const resp = await fetch(`${this.baseUrl}/v1/agent/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.cidHeader(reqOpts?.correlationId) },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`runAgentStream failed: ${resp.status}`);
    if (!resp.body) throw new Error("No response body");

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventType = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith("data: ") && eventType) {
          try {
            yield { type: eventType, data: JSON.parse(line.slice(6)) };
          } catch {
            // skip malformed
          }
          eventType = "";
        }
      }
    }
  }

  // --- Agent preview ---

  async agentPreview(opts: {
    mcpServers?: unknown[];
    toolAllowlist?: string[];
  }): Promise<Record<string, unknown>> {
    const body = toSnake({
      mcpServers: opts.mcpServers,
      toolAllowlist: opts.toolAllowlist,
    });
    const resp = await fetch(`${this.baseUrl}/v1/agent/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`agentPreview failed: ${resp.status}`);
    return resp.json();
  }

  // --- Discovery ---

  async discovery(): Promise<Discovery> {
    const resp = await fetch(`${this.baseUrl}/v1/discovery`, {
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`discovery failed: ${resp.status}`);
    const data = (await resp.json()) as Record<string, unknown>;
    // Wire is snake_case; surface camelCase for nested deps and top-level
    const deps = data.dependencies as any;
    return {
      service: data.service as "aitelier",
      version: data.version as string,
      apiVersion: data.api_version as string,
      timestamp: data.timestamp as string,
      endpoints: data.endpoints as Discovery["endpoints"],
      capabilities: data.capabilities as Discovery["capabilities"],
      dependencies: {
        litellm: {
          reachable: deps?.litellm?.reachable,
          baseUrl: deps?.litellm?.base_url,
          models: deps?.litellm?.models,
          reason: deps?.litellm?.reason,
        },
        sandboxAgent: {
          reachable: deps?.sandbox_agent?.reachable,
          baseUrl: deps?.sandbox_agent?.base_url,
          agents: deps?.sandbox_agent?.agents,
          reason: deps?.sandbox_agent?.reason,
        },
      },
      schemas: data.schemas as Record<string, string>,
      knownLimitations: data.known_limitations as string[],
    };
  }

  async getSchema(name: string): Promise<Record<string, unknown>> {
    const resp = await fetch(`${this.baseUrl}/v1/schemas/${name}`, {
      headers: this.authHeader(),
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!resp.ok) throw new Error(`getSchema failed: ${resp.status}`);
    return resp.json();
  }
}
