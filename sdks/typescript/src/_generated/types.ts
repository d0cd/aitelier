/**
 * Generated types from schemas/v1/ — do not hand-edit.
 *
 * To regenerate: ./scripts/generate-types.sh
 */

export interface Message {
  role: "user" | "assistant";
  content: string;
}

export interface McpServer {
  name: string;
  transport: "http" | "stdio";
  url?: string;
  command?: string;
  args?: string[];
}

export interface TaskSpec {
  name: string;
  kind: "complete" | "embed" | "agent";
  model?: string;
  systemPrompt?: string;
  messages?: Message[];
  prompt?: string;
  temperature?: number;
  maxTokens?: number;
  responseFormat?:
    | { type: "text" }
    | { type: "json_object" }
    | { type: "json_schema"; schema: object; strict?: boolean };
  texts?: string[];
  mcpServers?: McpServer[];
  toolAllowlist?: string[];
  maxTurns?: number;
  workspace?: string;
  workspaceMode?: "copy" | "in_place";
  preferredProviders?: string[];
  timeout?: number;
  traceTag?: string;
  metadata?: Record<string, unknown>;
}

export interface Usage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
}

export interface ToolCall {
  server?: string;
  tool?: string;
  input?: unknown;
  output?: unknown;
  elapsedMs?: number;
}

export interface Result {
  kind: "complete" | "embed" | "agent";
  provider: string;
  status: "ok" | "error";
  durationS: number;
  runId: string;
  traceId?: string;
  content?: string | null;
  parsed?: unknown;
  usage?: Usage | null;
  finishReason?:
    | "stop"
    | "length"
    | "content_filter"
    | "tool_use"
    | "completed"
    | "max_turns"
    | "timeout"
    | "error";
  costUsd?: number | null;
  embeddings?: number[][] | null;
  dimensions?: number | null;
  toolCalls?: ToolCall[] | null;
  sessionId?: string | null;
  filesChanged?: string[] | null;
  errorType?: string | null;
  errorMsg?: string | null;
}

export interface CompleteOpts {
  model: string;
  systemPrompt?: string;
  messages: Message[];
  temperature?: number;
  maxTokens?: number;
  responseFormat?:
    | { type: "text" }
    | { type: "json_object" }
    | { type: "json_schema"; schema: object; strict?: boolean };
  timeoutMs?: number;
  traceTag?: string;
}

export interface EmbedOpts {
  texts: string[];
  model?: string;
  timeoutMs?: number;
}

export interface RunAgentOpts {
  model: string;
  systemPrompt?: string;
  initialMessage?: string;
  /** Few-shot examples ({user, assistant} pairs); folded into systemPrompt server-side. */
  examples?: Array<{ user: string; assistant: string }>;
  mcpServers?: McpServer[];
  toolAllowlist?: string[];
  responseFormat?:
    | { type: "json_object" }
    | { type: "json_schema"; schema: object; strict?: boolean };
  maxTurns?: number;
  timeoutMs?: number;
  workspace?: string;
  workspaceMode?: "copy" | "in_place";
  traceTag?: string;
  metadata?: Record<string, unknown>;
}

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
  status?: string;
}

export interface AtelierEvent {
  type:
    | "run.started"
    | "run.completed"
    | "run.error"
    | "provider.started"
    | "provider.completed"
    | "provider.error"
    | "item.delta"
    | "item.done";
  timestamp: string;
  runId?: string;
  provider?: string;
  data?: Record<string, unknown>;
}

export interface FanoutRequest {
  task: TaskSpec;
  providers: string[];
  maxConcurrent?: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  timestamp: string;
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

// --- Streaming /v1/complete/stream events (tagged union on `type`) ---

export interface CompleteStreamDelta {
  type: "delta";
  content: string;
  correlationId: string;
}

export interface CompleteStreamDone {
  type: "done";
  content: string;
  usage: Usage;
  finishReason: string;
  costUsd?: number | null;
  traceId?: string;
  runId?: string;
  correlationId: string;
}

export interface CompleteStreamError {
  type: "error";
  errorType: string;
  errorMsg: string;
  correlationId: string;
}

export type CompleteStreamEvent =
  | CompleteStreamDelta
  | CompleteStreamDone
  | CompleteStreamError;
