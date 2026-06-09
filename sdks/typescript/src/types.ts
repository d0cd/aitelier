/**
 * TS types for the aitelier control plane. Hand-maintained, kept in
 * lockstep with `schemas/v1/` but exposed in camelCase to match
 * idiomatic TypeScript. The wire is snake_case (matching the Python
 * SDK and HTTP API); the SDK's casing converter normalizes responses
 * before they reach the consumer, so these interfaces describe what
 * the consumer sees, not the wire shape.
 *
 * Inference shapes (ChatCompletion, Embedding, Model) come from the
 * `openai` package — those keep snake_case to match OpenAI's wire.
 *
 * When a schema gains a field, update the corresponding interface here.
 */

export interface HealthResponse {
  status: string;
  version: string;
  timestamp: string;
  knownLimitations?: string[];
}

// --- Discovery (GET /v1/discovery) ---

export interface EndpointInfo {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
}

export interface CapabilityInfo {
  available: boolean;
  reason?: string;
}

export interface LitellmDep {
  reachable: boolean;
  baseUrl: string;
  models?: string[];
  reason?: string;
}

export interface SandboxAgentDep {
  reachable: boolean;
  baseUrl: string;
  agents?: string[];
  reason?: string;
}

export interface Dependencies {
  litellm: LitellmDep;
  sandboxAgent: SandboxAgentDep;
}

export interface Discovery {
  service: "aitelier";
  version: string;
  apiVersion: string;
  timestamp: string;
  endpoints: EndpointInfo[];
  capabilities: Record<string, CapabilityInfo>;
  dependencies: Dependencies;
  schemas: Record<string, string>;
  knownLimitations: string[];
}

// --- Cancellation ---

export interface CancelAck {
  runId: string;
  cancelled: boolean;
}

export interface ActiveRuns {
  active: string[];
}

// --- Durable runs (GET /v1/runs, /v1/runs/{id}) ---

export type RunState =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "orphaned";

export interface Run {
  runId: string;
  traceId?: string;
  state: RunState;
  kind: "complete" | "embed" | "agent";
  agentId?: string | null;
  model?: string | null;
  startedAt?: string | null;
  endedAt?: string | null;
  traceTag?: string | null;
  correlationId?: string | null;
  parentRunId?: string | null;
  sandboxBackend?: string | null;
  sandboxUrl?: string | null;
  sandboxServerId?: string | null;
  workspace?: string | null;
  environment?: Record<string, unknown>;
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  costUsd?: number | null;
  finishReason?: string | null;
  toolCallCount?: number;
  systemPromptHash?: string | null;
  status?: "ok" | "error" | "cancelled" | null;
  errorType?: string | null;
  errorMsg?: string | null;
  result?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface RunEvent {
  eventId?: number | null;
  runId: string;
  seq: number;
  kind: string;
  ts?: string | null;
  payload?: Record<string, unknown>;
}

// --- Schedules (/v1/schedules) ---

export interface Schedule {
  id: string;
  name: string;
  task: Record<string, unknown>;
  intervalSeconds?: number | null;
  atIso?: string | null;
  webhookUrl?: string | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  createdAt?: string | null;
}

export interface CreateScheduleOpts {
  name?: string;
  task: Record<string, unknown>;
  intervalSeconds?: number;
  atIso?: string;
  webhookUrl?: string;
}

// --- Traces ---

export interface TraceRecord {
  traceId: string;
  startedAt: string;
  endedAt?: string;
  model?: string;
  kind?: string;
  finishReason?: string;
  toolCallCount: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  costUsd?: number;
  systemPromptHash?: string;
  traceTag?: string;
  parentRunId?: string | null;
  status?: string;
  errorType?: string;
  errorMsg?: string;
  metadata?: Record<string, unknown>;
}

export interface TracesAggregateBucket {
  key: string;
  count: number;
  totalTokens: number;
  costUsd: number;
  errorCount: number;
}

export interface TracesAggregateTotals {
  count: number;
  totalTokens: number;
  costUsd: number;
  errorCount: number;
}

export interface TracesAggregate {
  groupBy: string;
  groups: TracesAggregateBucket[];
  total: TracesAggregateTotals;
}
