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

### Observability

- `GET /v1/traces`, `GET /v1/traces/{id}`, `GET /v1/traces/aggregates`.
- Correlation-ID middleware: echo header, body field, every SSE chunk, run.metadata.
- Structured logging (`[service] log_format = "json"`) — aggregator-friendly.

### Schedules + webhooks

- `GET/POST/DELETE /v1/schedules*` — recurring + one-shot.
- Schedule `task` shape is the chat-completions request body — same code
  path on fire.
- Durable webhook delivery (Postgres queue, exponential backoff 1s/5s/30s/5min/1hr).
- Optional HMAC signing — `X-Aitelier-Signature: sha256=<hmac>` when
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

## Directions worth exploring

Not commitments — forward-looking notes on where aitelier can compound
its existing investment. Ranked by alignment with the unique position
aitelier holds (OpenAI-shape inference + durable Postgres runs + ACP
agent dispatch + multi-agent via `parent_run_id` + personal-scale).

### Tier 1 — leans into aitelier's unique position

- **Agent trace observability + replay** (the "Phase H" idea).
  Existing observability platforms (LangSmith, Langfuse, Phoenix)
  instrument from the application; aitelier intercepts at the HTTP edge
  and already stores rich per-run data. Three small additions unlock a
  trace-reading workflow no competing tool offers end-to-end:
  - Persist the actual system prompt + messages alongside each run
    (currently only `system_prompt_hash` survives the request).
  - `POST /v1/runs/{id}/replay?model=X` — re-dispatch a finalized run
    with one field changed; new run linked via `parent_run_id`.
  - Static web UI at `/ui` — read-only browser over `/v1/runs`,
    `/v1/runs/{id}/events`, `/v1/traces/aggregates`. No build step.
  Pays off as both an observability tool *and* the foundation for evals
  (`trace_tag` + replay + aggregates already cover the eval workflow
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

### Tier 2 — table stakes; ship when pain forces it

- **OpenTelemetry export.** Emit run/event/trace data via OTLP so the
  existing observability ecosystem (Grafana, Datadog, Jaeger,
  Honeycomb) can consume it. Standards-compliant alternative to
  building an aitelier-specific dashboard.
- **Response caching (exact + semantic).** Builds on `/v1/embeddings`
  for semantic match, Postgres for storage. Opt-in via
  `aitelier.cache: {mode, ttl}`. Real cost savings; commoditized
  elsewhere (Bifrost, Portkey, LiteLLM all have it).
- **`POST /v1/batches`.** OpenAI's batch API shape. Maps cleanly onto
  the existing run state machine: a batch is many runs sharing a
  submission, lower priority, no streaming. Natural extension for
  offline workloads (eval suites, embedding backfills).

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
  Personal-runtime positioning means the team-scale ceiling is the
  feature, not the bug.

### Structural defensibility

Three choices to never give up:

1. **Service shape (HTTP/SSE), not framework.** Any OpenAI client works.
2. **OpenAI-compat front door.** No SDK lock-in.
3. **ACP-based agent dispatch.** Backends are interchangeable; isolation
   is upstream (Rivet's Sandbox Agent), not in aitelier.

If any flip, aitelier becomes a worse version of something else.

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
