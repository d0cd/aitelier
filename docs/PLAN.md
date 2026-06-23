# aitelier — current state

Snapshot of what's built and what's deliberately out of scope. For
phase-by-phase history, read the code or `git log`.

## Built

### OpenAI-shape inference

- `POST /v1/chat/completions` — sync + streaming (`stream: true`).
  Routes by `model` prefix: `agent:*` → Sandbox Agent, anything else →
  LiteLLM. Hard-rejects `tools` / `tool_choice` / `n>1` / `top_p` on the
  agent path. `aitelier.*` namespace in `extra_body` carries
  agent-specific options (workspace, MCP servers, prepare, artifacts).
- `POST /v1/embeddings` — OpenAI passthrough via LiteLLM.
- `GET  /v1/models` — model list + per-model `response_format` capabilities.

### Durable run state (control plane)

- `POST /v1/runs` — async agent submission with webhook delivery.
- `GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/events`,
  `GET /v1/runs/{id}/events/stream`, `GET /v1/runs/active`,
  `POST /v1/runs/{id}/cancel`.
- State machine: `pending → running → {completed | failed | cancelled | orphaned}`.
- Append-only `run_events` timeline.
- `mark_orphaned_running_runs()` startup sweep — prevents ghost rows after a crash.
- `sandbox_url` / `sandbox_server_id` / `sandbox_backend` stamped on every agent run.
- **`request_body` + `rendered_messages` captured on every run** (migration v4).
  Pre-fold body as received + post-fold message list as it went on the
  wire. Foundation for Phase H replay, the `/ui` browser, bolt-on eval
  frameworks, and OpenTelemetry GenAI export. Projection-redacted at
  the HTTP boundary (same pattern as `environment` / `result`).

### Observability

- `GET /v1/traces`, `GET /v1/traces/{id}`, `GET /v1/traces/aggregates`.
- Correlation-ID middleware: echo header, body field, every SSE chunk, run.metadata.
- Structured logging (`[service] log_format = "json"`) — aggregator-friendly.
- **OpenTelemetry GenAI export** (opt-in, `[otel] enabled = true`).
  An OTLP span tree per run — root span tagged with the GenAI semantic
  conventions (`gen_ai.system`, `gen_ai.request.*`, `gen_ai.response.*`,
  `gen_ai.usage.*`) plus an `execute_tool` child span per agent tool call,
  reconstructed from `run_events` at finalize (off the hot path). The trace
  id IS the run id (32-hex W3C value), so a run is addressable by id in any
  backend. Optional install via `aitelier[otel]`; default install pays no
  import cost. Any OTLP backend (Jaeger, Tempo, Honeycomb, Datadog,
  Phoenix, Langfuse-via-OTel) ingests without adapter code. Content opt-in
  (`capture_content = true`) emits message bodies as span events; off by
  default.
- **Eval framework substrate** (migration v5).
  - `POST /v1/runs/{run_id}/scores` — write-back scoring sink. No
    uniqueness on (run, name, evaluator) so re-grading is a write.
  - `GET /v1/runs/{run_id}/scores` — history, oldest first.
  - `GET /v1/runs/export` — NDJSON stream of full Run rows (with the
    captured `request_body`) for backfill grading. Filters mirror
    `GET /v1/runs`.
  - SDKs surface `add_run_score` / `list_run_scores` / `export_runs`.

### Schedules + webhooks

- `GET/POST/DELETE /v1/schedules*` — recurring + one-shot.
- Schedule `task` shape is the chat-completions request body — same code
  path on fire.
- Durable webhook delivery (Postgres queue, exponential backoff 1s/5s/30s/5min/1hr).
- Optional Bearer auth — `Authorization: Bearer <secret>` when
  `service.webhook_secret` is set.

### SDKs

- Python (`aitelier_client`) and TypeScript (`aitelier`) — same surface.
- Inference via `Aitelier.openai()` — returns a preconfigured OpenAI client.
- Control plane methods on `Aitelier` directly.
- OpenAI SDK is an **optional peer dependency**; consumers using only the
  control plane install only aitelier.

### Discovery + capability surface

- `GET /v1/health` — cheap liveness.
- `GET /v1/discovery` — endpoint inventory + dependency probes + per-model
  `response_format` capabilities.

### Config

- TOML-only, layered: defaults → `aitelier.toml` → `aitelier.secrets.toml`
  → `runs/.session.toml` (start.sh-managed runtime overlay).
- No `os.environ` reads in app code — single principled load path.

### Tooling

- `make start/stop/restart/logs/status/doctor/reset/test/test-live`.
- `scripts/doctor.sh` — preflight checks (ports, tools, creds, docker).
- Live test suite (`core/tests/live/`) — gated on `AITELIER_LIVE_URL`.

## 1.0 readiness — packaging gaps

Separate from the directional roadmap: these are the mundane items that
gate "someone other than the maintainer can integrate with aitelier."
Each is small in code but real in process. None require an
architectural decision; they require a release cadence.

- **Published SDKs.** `aitelier-client` (PyPI), `aitelier` (npm),
  `aitelier-mcp` (PyPI). `release.sh` already does lockstep version
  bumps; missing piece is a CI publish job and a documented release
  procedure. Without this, every integration starts with "clone the
  monorepo," which is a hard no for most consumers.

- **`CHANGELOG.md` + 1.0 commitment.** Pre-`1.0` signals instability;
  consumers want a documented stability story before depending. Keep-a-
  Changelog format; cut `1.0.0` once the SDK publish flow is live. Pin
  the public API surface (`/v1/*` shapes + SDK method signatures) and
  document a deprecation policy for breaking changes.

- **Documented deployment shapes.** `make start` is dev-only. Producing
  one reference compose stack (Postgres + LiteLLM + aitelier + optional
  Ollama) under `docs/deploy/` lets a consumer run aitelier in 5
  minutes without reverse-engineering the Makefile. K8s is not required
  for the personal-scale ceiling; compose is enough.

- **Backup / restore runbook.** Postgres holds every run, event,
  schedule, webhook, and idempotency key. A 1-page doc under
  `docs/deploy/` covering `pg_dump`, point-in-time recovery, and
  migration between aitelier versions is the smallest viable answer.
  No tooling required; just the prose.

- **Multi-instance contract.** Either commit explicitly to
  single-instance (and document why — the `_active_runs` and idempotency
  lock dicts are process-local) OR document the sticky-routing pattern
  for horizontal scale. The DB layer already supports it via the
  `idempotency_keys` table's `ON CONFLICT` claim and the `orphaned`
  state for crash recovery; the missing piece is operator guidance.

## Directions worth exploring

Not commitments — forward-looking notes on where aitelier can compound
its existing investment. Ranked by alignment with the unique position
aitelier holds (OpenAI-shape inference + durable Postgres runs + ACP
agent dispatch + multi-agent via `parent_run_id` + personal-scale).

### Tier 1 — leans into aitelier's unique position

- **Agent trace observability + replay** (the "Phase H" idea, now
  unblocked by the request-body capture under "Built" above).
  Existing observability platforms (LangSmith, Langfuse, Phoenix)
  instrument from the application; aitelier intercepts at the HTTP
  edge and already stores rich per-run data including the captured
  request body. Two small additions left:
  - `POST /v1/runs/{id}/replay?model=X` — re-dispatch a finalized run
    with one field changed; new run linked via `parent_run_id`. The
    captured `request_body` IS the replay input.
  - Static web UI at `/ui` — read-only browser over `/v1/runs`,
    `/v1/runs/{id}/events`, `/v1/traces/aggregates`. No build step.
    Renders `rendered_messages` as the conversation the model saw.
  Pays off as both an observability tool *and* the foundation for
  evals (`trace_tag` + replay + aggregates cover the eval workflow
  pattern; no DSL needed).

- **Agent behavior graphs (multi-resolution).**
  Compute prefix trees / process-discovery graphs from `run_events`
  joined by `trace_tag` or `system_prompt_hash`. Multiple resolutions
  via deterministic canonicalization (tool-only, tool+args-hash,
  tool+full-args). Endpoint: `GET /v1/traces/graph?trace_tag=X&resolution=Y`
  returns node/edge JSON; UI renders. The killer use case isn't
  visualizing one trace — it's comparing distributions across `trace_tag`s
  (model A vs B, before/after a prompt change, pass vs fail subsets).
  Research-flavored but nobody is doing this for LLM agents at the
  gateway level today.

- **Computer use / browser dispatch as a first-class model.**
  Sandbox Agent supports it; aitelier doesn't surface it as a routed
  model. Adding `agent:claude/computer-use` (or similar) and exposing
  the resulting artifacts (screenshots, DOM snapshots) via
  `aitelier.artifacts.fetch` would open a category the market is
  actively expanding (Browserbase, Stagehand, etc.).

- **Human-in-the-loop approval gates.** ACP's
  `session/request_permission` is auto-approved today
  (`acp_transport.py:191`). Surfacing it as a `POST
  /v1/runs/{id}/approvals?decision=…` API + a webhook fire to a
  consumer-configured URL would let an external app (Slack bot,
  approval dashboard, on-call paging) gate destructive tool calls
  without aitelier owning the UI. Backend primitive only: aitelier
  stores the pending decision in `run_events`, blocks the agent until
  the approval lands, and resumes — apps build whatever surface fits.
  Differentiation play: nobody else in the gateway space exposes this.

- **Anthropic Managed Agents worker mode.** Anthropic's `self_hosted`
  environment (public beta March 2026) is a poll-the-queue worker
  pattern: Claude.ai dispatches tool execution to your infrastructure,
  your worker spawns the sandboxed context, posts results back.
  Aitelier is structurally a near-perfect match — it already owns
  durable run state, ACP dispatch, and sandboxing. Implementing the
  worker loop turns aitelier into a self-hosted execution layer for
  Claude.ai itself. Consumers get Claude.ai as the frontend +
  aitelier's runs/traces/sandboxing on their own boxes.

- **Streaming replay from checkpoint.** Stream-idempotency today caches
  whole streams under an `Idempotency-Key` (capped at
  `STREAM_IDEMPOTENCY_MAX_CHUNKS = 2000`) for full replay on a
  duplicate request. Partial resume — client disconnects at chunk N,
  reconnects with `Idempotency-Key + Range: chunks=N-` and gets
  N+1 onward — is the missing piece. Same storage; one new endpoint
  shape. Closes the practical gap between "SSE" and "reliable SSE."

- **`group_by=score_name` aggregate.** Built scoring sink lets graders
  write back, but `/v1/traces/aggregates` doesn't yet group by score
  name. Adding it lets a dashboard show "avg helpfulness across runs
  in trace X" in one query. Pure SQL change; no new storage.

### Tier 2 — table stakes; ship when pain forces it

- **Response caching (exact + semantic).** Builds on `/v1/embeddings`
  for semantic match, Postgres for storage. Opt-in via
  `aitelier.cache: {mode, ttl}`. Real cost savings; commoditized
  elsewhere (Bifrost, Portkey, LiteLLM all have it).
- **`POST /v1/batches`.** OpenAI's batch API shape. Maps cleanly onto
  the existing run state machine: a batch is many runs sharing a
  submission, lower priority, no streaming. Natural extension for
  offline workloads (eval suites, embedding backfills).

- **Pre-flight cost projection.** `POST /v1/chat/completions/estimate`
  returns `{input_tokens, projected_cost_usd, model}` without
  dispatching. Lets apps surface "this will cost ~$0.04" before user
  confirms. Stateless; reuses LiteLLM's tokenizer + pricing tables.
  Commodity-grade; ship when consumer pain forces it.

- **Provider key rotation + multi-key brokering.** Multiple keys per
  provider in `[litellm]` config, hot-rotate without restart,
  round-robin (or quota-aware) selection per request. Lets a personal-
  scale operator multiplex across two free-tier keys without writing a
  separate proxy. Anthropic / OpenAI both punish per-key rate limits;
  this is the cheapest evasion that doesn't violate ToS.

- **ACP Registry awareness.** SA currently exposes a hardcoded backend
  list (`claude`, `codex`, `opencode`, …). Reading the ACP registry
  manifest at `cdn.agentclientprotocol.com` to discover new agent
  bridges (gemini-cli once SA adds it, future ones) would future-proof
  aitelier against ecosystem additions without code changes here.
  Mostly an SA-side concern, but surfacing the registry's capability
  flags in `GET /v1/models` is aitelier-side.

### Tier 3 — explicit refusals

These are recurring requests aitelier should keep refusing because
accepting them would change *what aitelier is*:

- General-purpose AI-gateway features. Bifrost owns the perf story
  (11μs overhead); Portkey owns guardrails. Stay thin at this layer
  and let LiteLLM keep doing the routing job.
- Observability for arbitrary frameworks. Langfuse / LangSmith / Phoenix
  own that scope. aitelier's UI should focus on what aitelier dispatched.
- Memory / threads / prompt registry / scoring DSL. Consumer concerns;
  refusing keeps the surface honest.
- Enterprise auth (RBAC, SSO, per-org budgets, audit retention).
  Single-user positioning means the team-scale ceiling is the
  feature, not the bug.

### Structural defensibility

Three choices to never give up:

1. **Service shape (HTTP/SSE), not framework.** Any OpenAI client works.
2. **OpenAI-compat front door.** No SDK lock-in.
3. **ACP-based agent dispatch.** Backends are interchangeable; isolation
   is upstream (Rivet's Sandbox Agent), not in aitelier.

If any flip, aitelier becomes a worse version of something else.

## Operational polish

Below the directional roadmap: items a tooling-savvy operator will
eventually need. Each is small in scope; they accumulate.

- **`/livez` vs `/readyz` split.** `/v1/health` today returns `ok` or
  `degraded`. Kubernetes-style probes want two distinct semantics:
  `livez` (process is alive — never fails unless aitelier itself is
  hung) and `readyz` (process can serve traffic — fails when LiteLLM
  or SA is unreachable, so the load balancer pulls the pod). One new
  cheap handler each.
- **Drain mode on SIGTERM.** Today SIGTERM cancels in-flight runs.
  Production rolling deploys want: stop accepting new runs, let
  in-flight finish, exit cleanly. `_active_runs` is the registry to
  drain against; add a `shutting_down` flag that `_reject_if_saturated`
  honors and a bounded grace period in the lifespan.
- **Audit log separate from `runs` / `run_events`.** Operator actions
  (API-key rotation, schedule create/delete, policy change) are not
  inference events. A separate `audit_log` table with a strict shape
  (`actor`, `action`, `resource`, `ts`, `correlation_id`) is mundane
  but valuable for any deployment that grows past one operator.
  Distinct from the RBAC stuff that's out of scope — this is just a
  durable trail of who did what.
- **Type-completeness pass.** Storage row converters still use `Any`
  in a few places; some return types are looser than they could be.
  Mostly polish.
- **CI on a representative cloud.** `make test` runs locally;
  `make test-live` requires a running stack. The missing piece is a CI
  job that exercises "fresh checkout, fresh Postgres, fresh LiteLLM,
  full e2e" on every PR — catches integration regressions that the
  unit suite misses by construction.
- **Multi-instance shared-state story.** If single-instance is the
  commitment, document that. If horizontal scale is worth supporting,
  the missing pieces are: sticky routing by `Idempotency-Key` (the DB
  claim already exists), or moving the lock dict + `_active_runs` to
  Redis. The decision is the scarce resource here, not the code.

## Deliberately out of scope

- **Multi-tenancy** — single-developer use; in-process run registry is fine.
- **Authentication / authorization beyond Bearer** — hosted mode is for
  trusted access; SSO/RBAC isn't justified.
- **Cost budgets / rate limiting per consumer** — let the LLM provider enforce.
- **Bridging inner-agent tool calls to consumer-side OpenAI tools** — the
  inner agent runs its own tools; consumers can't fulfill them via OpenAI's
  `tools` parameter.
- **Prompt registries, memory layers, agent state checkpointing,
  guardrails frameworks** — all consumer concerns; aitelier exposes
  primitives (system_prompt_hash, parent_run_id, idempotency, traces)
  that consumers compose into their own framework choices.
