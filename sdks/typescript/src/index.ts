/**
 * aitelier TypeScript SDK
 */

export { Aitelier } from "./client.js";
export type { AtelierOptions, RequestOpts } from "./client.js";
export { streamEvents } from "./streaming.js";
export type {
  // Core
  TaskSpec,
  Result,
  AtelierEvent,
  FanoutRequest,
  HealthResponse,
  CompleteOpts,
  EmbedOpts,
  RunAgentOpts,
  TraceRecord,
  Message,
  McpServer,
  Usage,
  ToolCall,
  // Discovery
  Discovery,
  EndpointInfo,
  CapabilityInfo,
  Dependencies,
  LitellmDep,
  SandboxAgentDep,
  // Cancellation
  ActiveRuns,
  CancelAck,
  // Streaming events
  CompleteStreamEvent,
  CompleteStreamDelta,
  CompleteStreamDone,
  CompleteStreamError,
} from "./_generated/types.js";
