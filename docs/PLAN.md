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

## Deliberately out of scope

- **Multi-tenancy** — single-developer use; in-process run registry is fine.
- **Authentication / authorization beyond Bearer** — hosted mode is for
  trusted access; SSO/RBAC isn't justified.
- **Cost budgets / rate limiting per consumer** — let the LLM provider enforce.
- **A web dashboard** — structured logs + `/v1/traces*` cover the current need.
- **Bridging inner-agent tool calls to consumer-side OpenAI tools** — the
  inner agent runs its own tools; consumers can't fulfill them via OpenAI's
  `tools` parameter.
