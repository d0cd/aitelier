# Changelog

All notable changes to aitelier are tracked here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
is pre-1.0 — breaking changes are expected and called out in the
relevant section.

## Unreleased

### Added

- **Multi-agent workflows.** `parent_run_id` is a pure-passthrough field
  on `/v1/runs` and `/v1/chat/completions` (agent path). No FK, no
  cycle check, no cascade cancel — the orchestrator above aitelier owns
  hierarchy semantics. Filter `/v1/runs?parent_run_id=X` and
  `/v1/traces?parent_run_id=X` recover a workflow's subtree.
- **`aitelier-mcp` package** (`sdks/python-mcp/`). FastMCP server
  exposing the control plane as five MCP tools (`submit_run`,
  `get_run`, `list_runs`, `list_run_events`, `cancel_run`). Inner
  agents load it via `aitelier.mcp_servers` and gain typed
  subagent dispatch without hand-rolling HTTP.
- **`POST /v1/runs/{id}/wait`** — server-side poll endpoint. Returns
  the terminal Run on 200; 408 when the timeout elapses with the run
  still pending/running. SDK methods `wait_for_run` (Python) and
  `waitForRun` (TypeScript).
- **Webhook receiver helpers.** `verify_webhook_signature` (Python)
  and `verifyWebhookSignature` (TypeScript) implement constant-time
  HMAC-SHA256 verification so consumers don't roll their own.
- **`examples/` directory** with four runnable recipes: fan-out + merge,
  MCP orchestrator, scheduled audit, webhook receiver.
- **SDK READMEs** for `aitelier-client`, `aitelier-mcp`, and the
  TypeScript `aitelier` package.
- INTEGRATION.md gains a **Multi-agent workflows** section covering
  HTTP-loopback and `aitelier-mcp` approaches, lineage recovery, and
  explicit non-features (no fanout primitive, no DAG runner, no
  coordinator agent type, no auto cascade cancel).

## Phase 10 — OpenAI-shape inference + control plane convergence

Major restructuring: aitelier settles on `/v1/chat/completions`,
`/v1/embeddings`, `/v1/models` for inference, with model-prefix
routing (`agent:<backend>[/<inner-llm>]` → Sandbox Agent). The
legacy `/v1/complete`, `/v1/embed`, and agent-fanout endpoints are
removed.

### Changed

- Error classifier hardened. Tunneled exceptions from ACP / Claude SDK
  resolve to the documented vocabulary (RateLimited, Timeout,
  ProviderUnavailable, AuthError, ProviderError) instead of leaking
  raw Python class names.
- Streaming entry points refactored into composable helpers
  (`_producer_for_acp_stream`, `_stream_chunks_for_delta`,
  `_stream_chunk_for_done`, `_stream_error_payload`,
  `_stream_terminal_state`, `_translate_note`, `_build_done_event`).
  No behavior change; just legibility.
- Streaming idempotency: SSE chunk replay under `Idempotency-Key`,
  with cancellation cleanup that survives consumer disconnect
  (background finalize task so the storage write isn't lost).
- ACP session close on every exit path (cancellation, prompt error,
  successful completion). Switched from notify → call so the close
  acknowledges before connection teardown. Closes the leaked-Claude-
  subprocess class.
- Anthropic prompt-caching passthrough: `cache_control` markers detected;
  `anthropic-beta: prompt-caching-2024-07-31` auto-attached on
  `claude*` / `anthropic/*` routes.
- Token-accounting invariant: `total_tokens == prompt + completion` on
  both LLM and agent paths; inner-agent overhead surfaces as
  `usage.aitelier_inner_tokens`.
- SSRF guard on by default in both localhost-trust and hosted modes.
  Loopback callbacks require explicit opt-in
  (`service.allow_loopback_webhooks`). Webhook worker re-validates the
  URL at delivery time.
- Internal URL scrubbing: sandbox-agent base_url no longer leaks into
  error envelopes, run-event payloads, or `/v1/discovery` in hosted mode.
- Input validation tightened: `Idempotency-Key` ≤200 chars (restricted
  charset), `X-Correlation-Id` ≤128 chars, in-flight run semaphore
  (typed 503 ProviderUnavailable when saturated), `AitelierAgentOpts`
  uses `extra="forbid"` so unknown `aitelier.*` fields fail fast.
- Embeddings: honors `encoding_format: "base64"` even when the
  upstream (Ollama via LiteLLM) returns floats — encodes to OpenAI's
  float32 little-endian packed bytes server-side.
- TS SDK: snake_case wire → camelCase consumer-facing types via a
  `PRESERVE_VALUE_KEYS`-aware converter that leaves user-supplied
  metadata / payload / task / environment blocks unchanged.

### Added

- `/v1/metrics` endpoint: uptime, RSS, CPU time, in-flight runs, recent
  status breakdown, webhook backlog.
- Per-route capability flags on `/v1/models`: `aitelier_request_caps`
  declares which OpenAI request fields each route honors. Inner LLMs
  filtered to chat-capable models (drops TTS/whisper/image/realtime).

### Removed

- Legacy `/v1/complete`, `/v1/embed`, and agent-fanout endpoints.

## Earlier phases (chronological)

### `/v1/agent` workflow consolidation

One HTTP call now orchestrates install → commands → file seed →
sidecars → ACP agent run → artifact fetch (`aitelier.prepare` +
`aitelier.artifacts`). Earlier pass-through `/v1/sandbox/*` endpoints
were dropped — they leaked SA's API under aitelier's namespace.
Sidecars torn down on every path, including agent error. Edge cases
beyond this workflow reach Sandbox Agent directly via the URL in
`/v1/discovery`.

### Model selection

LiteLLM pass-through wildcards (`anthropic/*`, `openai/*`,
`ollama/*`). `RunAgentRequest.agent_model` overrides the backend's
default inner LLM. `tool_allowlist` and `max_turns` now plumbed
through (no longer silent-dropped). `GET /v1/litellm/models` and
`GET /v1/sandbox/agents/{agent}` for discovery.

### Phase 9 — Durable storage

- **Chunk 1:** `core/src/aitelier/storage/` package with `Store`
  Protocol, `PostgresStore` (asyncpg), and `InMemoryStore`. State-
  machine validation on `Run` transitions. SQL migrations runner.
  Postgres added to docker-compose with healthcheck.
- **Chunk 2:** All trace/schedule operations move through the store.
  Legacy SQLite trace store and file-backed schedule registry deleted.
  ACP permission-handshake fix (auto-approve `session/request_permission`,
  polite `-32601` reject for unimplemented `fs/*` and `terminal/*`).
- **Chunk 3:** `/v1/runs` read API with full filter set
  (state, kind, agent_id, trace_tag, correlation_id, since). Event
  timeline endpoints (`/v1/runs/{id}/events`, `/events/stream`).
  `run_events` ingestion from the sandbox-agent provider.
- **Chunk 4:** Durable webhook delivery worker. Webhooks land in
  `webhook_deliveries`, retried with exponential backoff
  (1s / 5s / 30s / 5min / 1hr, 5 attempts).

### Phase 8 — Hosted mode

- `service.api_key` config gates every `/v1/*` endpoint behind
  `Authorization: Bearer <key>`. `/v1/health` exempt for liveness
  probes. Unset → localhost-trust mode.
- Both SDKs accept `api_key` and inject Authorization automatically.
- Hosted-deployment Dockerfile.

### Phase 7 — Remote sandbox-agent

`SANDBOX_AGENT_BASE_URL` pointing at a remote host skips the local
binary install; health-checks the remote with optional `SANDBOX_TOKEN`
via Authorization header. Lets long agent runs survive laptop sleep.

### Phase 6 — Background-agent primitives

- `schedules.py`: file-backed registry, interval / one-shot triggers,
  10s async tick loop.
- `POST/GET/DELETE /v1/schedules`.
- `RunAgentRequest.mode="async"` returns `{run_id, status: "accepted"}`
  immediately and POSTs the result to `webhook_url` on completion.

### Phase 5 — Observability

- `tool_calls` persisted to trace metadata (was: count only).
- `aggregate_traces()` + `GET /v1/traces/aggregates` rollups by
  trace_tag / kind / model / status / error_type / day.
- `AITELIER_LOG_FORMAT=json` for Loki/Datadog-friendly logs.
- Langfuse trace wiring for agent paths.

### Initial — Phases 0–4

OpenAI-compatible service skeleton (`/v1/complete`, `/v1/embed`,
`/v1/agent`), LiteLLM proxy integration, Sandbox Agent (Rivet)
integration via ACP, MCP server support, CLI tools, Python + TS SDKs,
CI workflow, initial documentation.
