# Changelog

All notable changes to aitelier are tracked here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
is pre-1.0 — breaking changes are expected and called out in the
relevant section.

## Unreleased

_Nothing yet._

## 0.1.0 — 2026-06-23

First public release.

### Added — Phase L (eval framework substrate: scoring sink + bulk export)

Two small additions that turn aitelier into the runtime substrate any
eval framework (Langfuse, Phoenix, PromptFoo, custom) sits on. Sticking
strictly to primitives — no rubric DSL, no grader catalog, no scoring
UI. Aitelier owns durable state; the eval framework owns grading.

- **Storage migration v5**: new `run_scores` table —
  `(id, run_id, name, value, evaluator, comment, metadata, created_at)`.
  No uniqueness on `(run_id, name, evaluator)`: re-grading is a write,
  not an update. Indexed by `run_id` (write path) and `name` (aggregate
  path).
- **`POST /v1/runs/{run_id}/scores`** — write a score back against a
  run. Returns 201 with the persisted row (`id`, `created_at`
  populated). 404 when the run doesn't exist. `name` /  `evaluator` are
  charset-restricted so they're safe in log lines and downstream
  aggregation queries.
- **`GET /v1/runs/{run_id}/scores`** — list all scores against a run,
  oldest first. Empty `data` when no grader has scored it yet.
- **`GET /v1/runs/export`** — streams `application/x-ndjson`, one full
  `Run` per line including the captured `request_body` and
  `rendered_messages` from migration v4. Lets a grader load history
  without paging through 500-row windows. Filters mirror `GET /v1/runs`
  (`since`, `until`, `trace_tag`, `kind`, `state`). Default cap 10k,
  bumpable to 100k.
- **SDK lockstep**: Python `Aitelier.add_run_score()`,
  `list_run_scores()`, and `export_runs()` (async-iterator); TypeScript
  `Aitelier.addRunScore()`, `listRunScores()`, and `exportRuns()`
  (async-iterable). New `RunScore` dataclass exported from both SDKs.
- **Route ordering fix**: registered `/v1/runs/export` before
  `/v1/runs/{run_id}` so the literal path doesn't get matched as a
  `run_id` capture. (Same fix applies to any future literal-after-
  parameter route added under `/v1/runs/`.)
- **14 new tests** in `test_storage.py` (InMemoryStore round-trip,
  history, unknown-run rejection, empty-list contract; Postgres
  integration round-trip against migration v5) and `test_server.py`
  (POST happy path + 404 + charset rejection + extra-field rejection,
  GET history, NDJSON streaming + filter + ISO-8601 validation +
  request_body inclusion).

Net: 426 unit tests pass (was 412 at the end of Phase K). No new
configuration required; OFF-by-default behavior unchanged.

### Added — Phase K (OpenTelemetry GenAI semantic conventions export)

Opt-in OTLP span export per inference call, tagged with the [GenAI
semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
so any OTLP-speaking backend (Jaeger, Tempo, Honeycomb, Datadog,
Phoenix, Langfuse-via-OTel) can ingest aitelier traffic without
adapter code.

- **`[otel]` config section** in `aitelier.toml` — `enabled`,
  `endpoint`, `protocol` (`grpc` / `http`), `insecure`, `service_name`,
  `capture_content`. Off by default; zero request-path cost when
  disabled.
- **Optional install** via `pip install 'aitelier[otel]'`. The OTel
  SDK + OTLP exporters are *not* default dependencies — a vanilla
  install pays no import cost. If `[otel] enabled = true` but the
  SDK isn't installed, aitelier logs one WARN at startup and inference
  keeps working (instrumentation call sites are no-ops when the tracer
  was never set).
- **`aitelier.otel`** — pure attribute builders (`gen_ai_system_for_model`,
  `gen_ai_request_attrs`, `gen_ai_response_attrs`) plus
  `record_inference_span(operation, request_body, result, …)`. The
  builders are SDK-free and testable on a default install.
- **System mapping**: `claude-*` / `anthropic/*` → `anthropic`,
  `gpt-*` / `o1-*` / `o3-*` / `openai/*` → `openai`, `gemini-*` →
  `gemini`, `ollama/*` / `local` → `ollama`, agent backends →
  `aitelier.agent.<backend>` (custom namespace — OTel's registry has
  no canonical name for sandboxed agent runtimes), unknown → `_OTHER`.
- **Instrumented at all 4 inference call sites**:
  - `_llm_chat_completion` (sync LLM)
  - `_llm_chat_completion_stream` (streaming LLM — span fires after
    the stream terminates, with accumulated usage)
  - `_agent_chat_completion` (sync + streaming agent — both paths
    funnel through one call after the agent run completes)
  - `endpoints/inference.py:embeddings_endpoint` (operation =
    `embeddings`)
- **Error spans** carry `error.type` + `error.message` and a non-OK
  span status. Trace backends can filter for failed inference without
  parsing logs.
- **Content opt-in**. `[otel] capture_content = true` emits each
  message as a span event (`gen_ai.system.message`,
  `gen_ai.user.message`, `gen_ai.assistant.message`,
  `gen_ai.tool.message`) following the convention's content-as-events
  model. Defaults to off — message bodies routinely carry PII /
  secrets, and the destination collector's retention isn't aitelier's
  to assume.
- **One span per request, not per agent turn.** Agent-path runs
  collapse the entire run (turns + tool calls) into a single span.
  Per-turn detail still lives in `run_events` — OTel sees the outer
  shape.
- **38 new unit tests** in `test_otel.py` covering:
  - Pure builders: system mapping (Anthropic / OpenAI / Gemini / Ollama
    / `aitelier.agent.*` / `_OTHER`), request attribute extraction
    (model, max_tokens precedence, temperature / top_p / frequency /
    presence penalties, stop sequences, partial bodies), response
    attribute extraction (OpenAI shape, aitelier agent shape,
    finish-reason deduplication, empty-input tolerance).
  - Span emission against an in-memory exporter: full attribute set
    lands, span name format (`"chat <model>"` vs. operation-only),
    error path (`error.type` + non-OK status + error description),
    content-event opt-in (off by default, on emits per-role events,
    skipped for embeddings regardless).
  - Lifecycle: `init_tracer_provider` is a no-op when disabled and
    idempotent when already initialized; `shutdown_tracer_provider`
    is safe to call uninitialized.
  - `OtelConfig` TOML loading: explicit `[otel]` section hydrates all
    six fields; absent section keeps defaults (enabled = false).
  - Exporter selection: `protocol = "grpc"` returns the gRPC
    OTLPSpanExporter; `protocol = "http"` returns the HTTP exporter.
  - **End-to-end HTTP integration**: TestClient against the real
    FastAPI app proves the LLM (sync + streaming), agent (sync +
    streaming), and embeddings (`POST /v1/embeddings`) endpoints all
    invoke `record_inference_span` with the correct operation, model,
    system attribute, and token usage. Streaming-path spans verify the
    `finally`-block emission (LLM) and the detached `_finalize_stream_run`
    emission (agent) — both fire after the SSE stream terminates and
    carry the accumulated usage. The detached task is tracked in
    `_pending_finalize_tasks` so the agent-stream test joins
    deterministically (no polling, no flakes).
  - **Lifespan integration**: `with TestClient(app) as c:` triggers
    real startup/shutdown — verifies `init_tracer_provider` runs (sets
    `_tracer`) and `shutdown_tracer_provider` runs (clears it).
  - **SDK-missing graceful degradation**: stubs `opentelemetry.*`
    out of `sys.modules` and asserts `init_tracer_provider` logs the
    install-hint WARNING and returns without setting `_tracer`.
  - **Best-effort guard**: `record_inference_span` swallows SDK-side
    failures (bad attr, exporter bug) — a `start_as_current_span` that
    raises surfaces as a WARNING, never as a propagated exception.
  - **`BatchSpanProcessor` flushes on shutdown**: install a real batch
    processor + in-memory exporter, emit a span, call
    `shutdown_tracer_provider`, assert the buffered span landed.

  Robustness changes alongside the new tests:
  - `record_inference_span` now wraps its body in a best-effort
    try/except — a regression in the OTel SDK can't turn into a 500
    on the inference path.
  - `_finalize_stream_run` reorders durability before observability:
    storage finalize + idempotency cache write happen *before* the
    OTel span emission, so an observability failure can't leave the
    idem lock released without a cache row (which would cause a retry
    under the same key to re-execute the agent).
- **`docs/INTEGRATION.md`** gains an "Observability — OpenTelemetry
  export" section documenting the config, install, attribute mapping,
  content-event opt-in, and the one-span-per-request scope decision.

Net: 412 unit tests pass (was 374 at the end of Phase J). No request-
path cost when disabled. Default-install behavior unchanged.

### Added — Phase J (request body persistence — eval/replay/OTel foundation)

Storage migration v4 + plumbing to capture the actual request body
alongside each run. Foundation for the Phase H replay endpoint, the
static `/ui` browser, bolt-on eval frameworks, and OpenTelemetry GenAI
export — all of which need the captured input to function.

- **`runs` table gains two JSONB columns** (`004_persist_request_body.sql`):
  - `request_body_json` — caller's body as received (`ChatCompletionRequest`
    / `AsyncRunRequest` / `EmbeddingsRequest`), pre-fold, pre-translate.
  - `rendered_messages_json` — message list after aitelier's agent-path
    translations (system-prompt fold, response_format injection,
    `<aitelier_context>` block). What actually went on the wire.
- **`RunSpec` + `Run` dataclasses** carry `request_body: dict | None`
  and `rendered_messages: list[dict] | None`. Both NULL for backward
  compatibility — historical runs and synthetic schedule-side failures
  surface `null` rather than `{}`, so consumers can distinguish "no
  record" from "empty body sent."
- **Captured at all 5 RunSpec construction sites**:
  - `_agent_chat_completion` (sync agent) and `_agent_chat_completion_stream`
    (streaming agent) — rendered messages collapse to system + last user.
  - `_llm_chat_completion` (sync LLM) and `_llm_chat_completion_stream`
    (streaming LLM) — `rendered_messages = body["messages"]` post
    `_llm_body_from_request`.
  - `endpoints/inference.py:embeddings_endpoint` — `rendered_messages`
    stays `None` (no messages on the embed path); `request_body`
    captures input list + encoding_format.
  - `_schedule_handler` synthetic failure path — `request_body = task`
    so a debug-able record of the failed schedule survives.
- **HTTP projection redacts at the boundary** via `_redact_secrets` —
  same pattern as `environment` / `result` / `metadata`. Stored row
  keeps originals; `Authorization: Bearer …` headers inside
  `aitelier.mcp_servers[*].headers` and credential-named fields scrub
  to `[redacted]` before reaching API consumers. `None` passes through
  unchanged.
- **SDK Run types updated** — Python (`request_body: dict | None`,
  `rendered_messages: list[dict] | None`) and TypeScript (`requestBody`,
  `renderedMessages` with `null` preserved). TS adds `request_body` and
  `rendered_messages` to `PRESERVE_VALUE_KEYS` so the OpenAI-shape
  snake_case keys inside the captured body don't get mangled to
  camelCase on read.
- **`run.schema.json` declares the two new fields** — JSON Schema
  source of truth stays in sync.
- **5 new unit tests**:
  - `test_storage.py` — round-trip for both fields (NULL + populated).
  - `test_server.py` — `_run_to_dict` redaction of MCP-header shapes
    inside `request_body`; NULL preservation for pre-v4 runs.
- **`docs/INTEGRATION.md`** gains a "Captured request body + rendered
  messages" subsection under "Run state machine" documenting the
  semantics, the redaction guarantee, and the four downstream surfaces
  this unblocks.

Net: 374 unit tests pass (was 363 at the end of Phase I). No backward
compat breaks — pre-v4 rows surface `null` for both fields. New
queries against `/v1/runs/{id}` see populated values for runs created
after the migration.

### Added / changed — Phase I (decomposition arc + security hardening)

This phase moved ~700 LOC out of `server.py` (2700+ → 1843) into
focused modules without changing behavior. All endpoint surfaces
preserved; tests grew from 325 → 363.

#### Module decomposition

- **`endpoints/` package** — one APIRouter per resource. Handlers
  lazy-import shared helpers from `server.py` to break the module-load
  cycle that would otherwise result from a circular import.
  - `endpoints/inference.py` — `/v1/chat/completions`, `/v1/embeddings`,
    `/v1/models` (plus `_filter_chat_capable`, `_list_agent_models`,
    `_ensure_base64_embeddings`).
  - `endpoints/runs.py` — `/v1/runs`, `/v1/runs/{id}`,
    `/v1/runs/{id}/events*`, `/v1/runs/active`, `/wait`, `/cancel`.
  - `endpoints/schedules.py` — `/v1/schedules/*` CRUD.
  - `endpoints/traces.py` — `/v1/traces/*` projections.
- **`middleware.py`** — auth → correlation → body_size → rate_limit
  stack, mounted via `register_middleware(app)`. State (`_rate_limit_buckets`,
  `_correlation_id_var`, `_AUTH_EXEMPT_PATHS`) lives here; `server.py`
  re-exports for backward-compat with existing tests.
- **`idempotency.py`** — `check_idempotency` / `record_idempotency` /
  `release_idempotency_ctx` + per-key locks + `IdempotencyContext`,
  extracted from `server.py`. `server.py` re-exports under the prior
  underscored names so endpoint modules' lazy imports stay terse.
- **`providers/acp_transport.py`** — ACP-over-HTTP wire layer
  (`AcpClient`, `AcpError`, `_is_local_url`, `_scrub_sandbox_url`,
  `_warn_remote_misconfig`, `_persist_sandbox_server_id`,
  `ACP_PROTOCOL_VERSION`). `sandbox_agent.py` keeps the high-level
  session orchestration.
- **`providers/ollama.py`** — direct `/api/chat` bypass for `local` /
  `ollama/*` extracted from `llm.py` (LiteLLM's Ollama adapter drops
  `message.thinking`). `get_shared_client` lookup goes via the `llm`
  module attribute so existing test patches still bind.
- **`storage/{postgres,inmemory}.py`** — the two Store implementations
  split out of the 1019-LOC `_store.py` monolith. Protocol + factory
  stay in `_store.py`; row→dataclass helpers live with PostgresStore.
  `AGGREGATE_GROUP_KEYS` moved to `storage/models.py` so both impls
  import it without circular dependency.

#### Backend primitives

- **`aitelier.reasoning_effort` / `aitelier.approval_mode`** — agent
  session config driven by what each backend advertises at `session/new`:
  inner model via `session/set_model`, reasoning via
  `session/set_config_option` (`thought_level`), approval/sandbox preset
  via `session/set_mode` (`mode`). Values validated against the advertised
  set; unknown values fail fast. (Supersedes the earlier inert `plan_mode`
  field, which was removed.)
- **claude-acp config via `session/new._meta`** — `maxTurns`, `model`,
  `allowedTools`, `systemPrompt` now flow via
  `_meta.claudeCode.options` instead of the silently-dropped
  `session/set_config_option` notify. Verified against bridge 0.36.1
  (`dist/acp-agent.js:1371-1450`). Previously every option was
  swallowed and claude ran unbounded turns to natural completion;
  `max_turns=1` cuts response time from 119s → 2s on a simple ack.
- **Ollama `reasoning_effort` → `think` mapping.** OpenAI's canonical
  enum (`minimal | low | medium | high`) maps to Ollama's binary
  `think` toggle. `minimal` disables thinking; `low|medium|high`
  enable it; omitted leaves the model default. Fixes a downstream
  consumer's production incident where `qwen3:8b` returned `content=""` with
  `finish_reason=length` because thinking consumed the full
  `num_predict` budget.
- **`POST /v1/runs/{id}/wait`** — block until a run reaches a terminal
  state. Convenience over manual polling for consumers that don't want
  to set up a webhook receiver. Returns `408` with retry guidance when
  the timeout elapses before terminal.
- **`GET /v1/metrics`** — runtime counters for operators.

#### Security

- **`errors.scrub_error_text()`** — regex-based credential redaction
  for free-form error text. Applied at every `str(exc) → error_msg`
  site in `server.py` (5), `endpoints/runs.py` (1, was missed in
  initial extraction and caught in audit), `providers/sandbox_agent.py`
  (2), and `runs.py` `_finalize_terminal` (1). Covers:
  - `Authorization: Bearer <token>` header echoes
  - bare `Bearer <jwt>` (≥16 chars to skip false-positive matches)
  - `?api_key=…` / `&token=…` / `&password=…` URL query params
  - JSON-ish `'token': '…'` field=value pairs
  - `https://user:password@host` basic-auth URLs (added in audit 4)
- **Scheduled-run failure persistence.** `_schedule_handler` previously
  swallowed pre-`record_run` exceptions (validation, route parse) —
  the webhook fired but `/v1/runs` had no record. Now persists a
  synthetic failed row via `_finalize_terminal` so the control plane
  surfaces every fired schedule.
- **`tools` field capped at 256 entries.** Bounds parse cost when a
  hostile caller maxes out the body-size limit with trivially-small
  tool entries. Above any provider's practical limit.
- **`runs.state` vs `runs.status` invariant documented** at the
  dataclass field level (`storage/models.py`). State is the lifecycle
  position; status is the outcome category; they diverge only on
  user-initiated cancellation.

#### Tests

- **+38 unit tests** across the new modules:
  - `test_acp_transport.py` (new, 10 tests) — `_is_local_url`,
    `_scrub_sandbox_url`, `_persist_sandbox_server_id` (local vs
    remote classification, error swallowing, no-op on empty run_id).
  - `test_sandbox_proxy.py` (new, 12 tests) — `run_prepare`,
    `stop_sidecars`, `fetch_artifacts`, `prepare_failed_result`
    coverage that was previously only exercised via end-to-end tests.
  - `test_errors.py` (+10 tests) — every scrub pattern + edge cases
    (false positives on "Bearer of bad news", empty/None inputs,
    case-insensitive `reasoning_effort`).
  - `test_llm.py` (+6 tests) — direct unit tests for
    `chat_completion_via_ollama{,_stream}` (transport error,
    upstream 5xx, NDJSON happy path, stream error on open).

#### Brig deployment

- **SA-only cell** — aitelier runs on the host, talks to SA in brig
  via the ingress reverse proxy. Brig isolates what needs isolating
  (the agent processes) without conflating with aitelier's runtime.
- **`trust_warden_ca: true`** (brig 0.3.0 default) replaces the
  pre-0.3.0 manual `warden-ca-cert` secret + entrypoint stitching.
  Brig stages a combined system+warden CA bundle and auto-exports
  `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` /
  `NODE_EXTRA_CA_CERTS`. Three implementation bugs we caught and
  brig fixed mid-session.
- **`tls_passthrough`** for `chatgpt.com` + `auth.openai.com` — brig
  shipped exactly the principled fix we sketched, unblocking codex
  agent runs through brig (Cloudflare-fronted strict TLS that
  mitmproxy can't relay).
- **`platform.claude.com` added to allow list** — Anthropic's
  Agent SDK / extra-usage rollout (April 2026) added a new OAuth
  quota endpoint that claude-code hits on every session/prompt.
  Without this the bridge hangs indefinitely on a blocked CONNECT.

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
