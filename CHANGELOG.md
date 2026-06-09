# Changelog

All notable changes to aitelier are tracked here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
is pre-1.0 — breaking changes are expected and called out in the
relevant section.

## Unreleased

### Added / changed — Phase H (audit-3 fixes)

- **Secret redaction extended to `metadata`, `result`, and
  `run_events.payload`.** Phase F covered `environment`; the adjacent
  surfaces (`/v1/runs/{id}.metadata`, `.result`, and
  `/v1/runs/{id}/events[/stream]` payloads — which include raw
  `tool_call.input` arguments and `tool_result.output` content) were
  not. They are now run through `_redact_secrets` at the projection
  boundary in `_run_to_dict` and `_event_to_dict`. Stored rows keep
  the originals for operator debugging.
- **`/v1/traces/{trace_id}` validates the path component** via
  `security.validate_path_component`, matching every other path-
  segment route. 404 detail no longer reflects the raw value.
- **List-endpoint `limit` is bounded** at the route layer:
  `/v1/traces` and `/v1/runs` cap at 500, `/v1/runs/{id}/events` caps
  at 5000. Stops a single authenticated caller from forcing a 10M-row
  SELECT.
- **Inline webhook fallback removed.** When Postgres enqueue fails
  the previous code POSTed inline, skipping HMAC signing AND the
  delivery-time SSRF re-check. Better to log + lose the single
  delivery than emit an unsigned, un-SSRF-checked webhook. Consumers
  retry / poll on their side.
- **Stream idempotency replay buffer capped.** Stream runs emitting
  more than 2000 chunks skip the idempotency cache (the run still
  succeeds; only the 24h replay path is best-effort).
- **LLM transport-error message scrubbed.** Connect / read-timeout
  / DNS failures used to surface `str(exc)` to consumers, leaking
  upstream URLs/hosts. New `_safe_connect_message` returns only the
  exception class + a generic phrase; the full string is logged.
- **`_fold_response_format` accepts the OpenAI nested
  `{type: "json_schema", json_schema: {schema}}` shape** in addition
  to the flat `{type: "json_schema", schema}` form, and caps the
  rendered schema at 32 KiB (over-cap schemas still travel via ACP
  but aren't folded into the system prompt).
- **`security.validate_path_component` gains a 256-byte length cap**
  and the 400 detail no longer echoes the offending value.
- **`/v1/discovery.models` declared on schema + SDKs.** The field
  has been on the wire since Phase 10; the schema and both SDK
  Discovery types lagged.
- **Body-size middleware guards negative `Content-Length`** — a
  hostile `-1` previously short-circuited the 413 check.
- **Dead code removed**: `_merge_correlation` (server.py),
  `_task_for_dispatch` (schedules.py).
- **Stale comments cleaned up**: `AITELIER_LOG_FORMAT` env-var
  reference in the JSON formatter docstring, `_normalize_maxrss`
  docstring describing a heuristic the code didn't implement, two
  redundant "Workflow helpers" block comments, dated incident
  references in test docstrings.
- **CHANGELOG.md Phase F entry corrected**: the `AtelierOptions`
  alias was *removed* in Phase G, not kept as `@deprecated`.

### Added / changed — Phase G

- **`agent:mock` no longer leaks `KeyError`.** `_open_acp_session`
  defensively looks up `sessionId` and raises a classified `AcpError`
  when missing. The `mock` SA backend is filtered from
  `/v1/discovery.dependencies.sandbox_agent.agents` so consumers
  don't pick it up as a test target.
- **`response_format` on agent path is best-effort enforced.**
  `_fold_response_format` renders the JSON Schema into the system
  prompt as text alongside the existing ACP `responseFormat`
  pass-through, so backends that ignore the ACP param (claude-code
  et al) still see the contract.
- **`aitelier_trace_id` removed.** Was always identical to
  `aitelier_run_id`; deleted outright (no slow-deprecation per the
  no-tech-debt rule). `/v1/traces/{id}` still keyed by `run_id`.
- **TS SDK `AtelierOptions` typo alias removed**, leaving only
  `AitelierOptions`.
- Multi-turn history folding is already documented at
  INTEGRATION.md "Multi-turn history" — confirmed no doc change
  needed.

### Added — Phase F (audit-2 fixes)

- **Secret redaction** on `/v1/runs/{id}` and `/v1/schedules*`.
  `environment.mcp_servers[*].headers` (Bearer/PAT tokens for third-
  party MCP servers) and `prepare.commands[*].env` values are returned
  as `[redacted]` instead of verbatim. The stored row keeps the real
  values; only the HTTP projection is redacted, so the Sandbox Agent
  still receives real values at dispatch time.
- **`POST /v1/schedules` rejects schedule names with invalid charset.**
  `name` is now `[A-Za-z0-9_\-\.]{1,64}` — blocks stored prompt-
  injection through the `<aitelier_context>` system-prompt block.
- **Rate limit runs after auth.** Middleware registration order
  reversed so unauthenticated 401s don't fill the bucket map.
- **Rate-limit bucket map is LRU-capped at 10 000 entries.** A caller
  cycling Bearer values can no longer grow memory without bound.
- **`[purge] run_retention_days`** (default 30) replaces the literal
  `30` in the startup runs purge. `_KNOWN_LIMITATIONS` updated.
- **`Store.count_pending_webhooks`** — `/v1/metrics.webhooks.pending`
  now reports a real count on Postgres (previously always 0).
- **TS SDK gains `streamRunEvents`** (SSE iterator); brings the TS
  control plane to parity with Python.
- **TS SDK renames the constructor-options type to `AitelierOptions`**
  (was `AtelierOptions` — a typo). Phase G removed the typo'd alias
  outright per the no-tech-debt rule.
- **`TraceRecord` gains `parent_run_id`, `error_type`, `error_msg`,
  `metadata`** in both SDKs — fields the server has always emitted
  but the typed clients dropped.
- **`schemas/v1/discovery.schema.json` declares `models`** (the server
  has emitted it since Phase 10; the schema lagged).

### Changed — Phase F

- **`make_run_id` appends 4 hex chars of entropy.** Microsecond
  timestamps alone can collide inside one event-loop tick under tight
  async fan-outs; runs are now PK-collision-proof.
- **Ollama bypass path runs through `_safe_upstream_message`.** Phase C
  scrubbed LiteLLM errors but the direct Ollama route was missed.
- **`purge_worker` re-reads `interval_seconds` on every tick.** Operator
  config edits take effect without a restart; setting to 0 pauses the
  worker.
- **`ScheduleRequest` and `EmbeddingsRequest`** use
  `model_config = ConfigDict(extra="forbid")` for parity with
  `ChatCompletionRequest` / `AitelierAgentOpts`.
- **Stale `/v1/agent` comments cleaned up** across `server.py`,
  `sandbox_proxy.py`, `providers/sandbox_agent.py`, migration 002,
  and `core/tests/live/README.md`. The endpoint has been
  `/v1/chat/completions` (model-prefix routed) since Phase 10.
- **INTEGRATION.md documents `POST /v1/runs/{id}/wait`** and corrects
  the SSRF guard description (always on, not hosted-mode-only).

### Added — Phase E

- `sandbox_proxy.py` extracts `sa_proxy`, `run_prepare`, `stop_sidecars`,
  `fetch_artifacts`, `prepare_failed_result` from `server.py` (~180
  lines).
- `security.validate_path_component` lifted out of `server.py` so the
  whitelist regex is module-level rather than re-compiled per call.
- `runs.start_run(spec)` packages `create_run` + `update_run_state` for
  the two streaming paths.

### Added — Phase D

- `ChatCompletionRequest` gains `extra="forbid"` + declared fields for
  `stream_options`, `seed`, `frequency_penalty`, `presence_penalty`,
  `stop`, `logprobs`, `top_logprobs` (previously dropped silently).
- Defense-in-depth `os.sep` suffix on the runs-directory prefix check.
- `runs_dir` honored from config (was a hardcoded `Path("runs")`).
- Stale `DATABASE_URL` references replaced with `[database] url`.
- Dead `sdks/python/.../streaming.py` removed.
- `make test-py` now includes `sdks/python-mcp/tests/`.

### Added — Phase C

- `service.max_request_body_bytes` (default 4 MiB) + body-size
  middleware (413 before any handler runs).
- `service.rate_limit_per_minute` (default 0 = off) + token-bucket
  middleware (429 with `Retry-After`).
- Background `purge_worker` (`[purge] interval_seconds`,
  `webhook_retention_days`, `event_retention_days`) — calls
  `purge_expired_idempotency_keys`, `purge_old_webhook_deliveries`
  (new), `purge_old_run_events` (new).
- LLM upstream error body scrubbed via `_safe_upstream_message`
  (canonical phrase + status code; raw body logged server-side).
- INTEGRATION.md "Hosted-mode deployment envelope" section: single-key
  semantics, schedule task visibility, recommended TOML.

### Added — Phase B

- `system_prompt_hash` and `result` on `schemas/v1/run.schema.json`.
- TS `Run` gains `parentRunId`, `systemPromptHash`, `result`.
- Python Run gains `system_prompt_hash` (was on TraceRecord only).
- `aitelier.toml.example` documents `service.max_in_flight_runs`,
  `service.allow_loopback_webhooks`, `ollama.default_model`.
- SDK tests for `wait_for_run`, `list_runs(parent_run_id=…)`.

### Added — Phase A

- **`AITELIER_RUN_ID` injection** into every stdio MCP server's env
  (`_adapt_mcp_servers`) and the parent agent's system prompt via an
  `<aitelier_context>` block (`_open_acp_session`). The inner agent
  can now learn its own run_id and dispatch children with the right
  `parent_run_id`.
- **`aitelier-mcp` gains `get_my_run_id` tool** reading
  `AITELIER_RUN_ID` from the stdio env.
- `Run.result` now surfaced by `_run_to_dict` (was persisted but
  silently dropped); Python SDK Run model gains the field.
- INTEGRATION.md documents the self-identification mechanism (replaces
  the broken `$AITELIER_RUN_ID` shell-var docs).
- Examples 01 and 02 fixed: `await ait.openai()` → `ait.openai()`
  (Python is sync); `run.result.get("content")` works because the field
  is now returned; example 02 uses `get_my_run_id` instead of an
  un-implementable prompt placeholder.

### Earlier in unreleased — pre-Phase-A polish

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
