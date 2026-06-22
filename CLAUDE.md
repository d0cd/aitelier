# aitelier

Personal AI runtime — an OpenAI-compatible HTTP service plus an aitelier-native
control plane for durable run state, traces, schedules, and async agent runs.

## API surface

aitelier speaks **OpenAI shape for inference** and an **aitelier-native control plane**
for everything else. Two clean layers.

### Inference — OpenAI shape

- `POST /v1/chat/completions` — sync + streaming (`stream: true`)
- `POST /v1/embeddings`
- `GET  /v1/models`

Model routing is by the `model` field:

| `model` value | Path |
|---|---|
| `claude-sonnet`, `nomic-embed-text`, `local`, … | LiteLLM (curated alias) |
| `anthropic/*`, `openai/*`, `ollama/*` | LiteLLM (passthrough wildcard) |
| `agent:<backend>/<inner-llm>` | Sandbox Agent + explicit inner LLM (inner model **required**) |

A bare `agent:<backend>` (no inner model) is rejected with a 400 — the inner
model must be named so the run records the exact model it used and its cost can
be estimated.

Agent-specific options ride in `extra_body.aitelier.*`:

```json
{
  "model": "agent:claude/claude-sonnet-4-5",
  "messages": [{"role": "user", "content": "audit this repo"}],
  "aitelier": {
    "workspace": "/path/to/repo",
    "mcp_servers": [...],
    "tool_allowlist": [...],
    "max_turns": 25,
    "reasoning_effort": "high",
    "approval_mode": "auto",
    "prepare":  { "commands": [...], "files": [...], "sidecars": [...] },
    "artifacts": { "fetch": ["/workspace/out.json"] },
    "trace_tag": "audit-run-2026-05"
  }
}
```

(`prepare`/`artifacts` run only on the non-streaming path — with `stream: true`
they're rejected; `tool_allowlist`/`max_turns` are claude-only. Streaming works
for plain `agent:<backend>/<model>` requests without those options.)

Inner-agent session config is driven by what each backend advertises at
`session/new` (probed + surfaced in `/v1/models`): `agent:<backend>/<model>`
selects the model (backend-native id, e.g. `gpt-5.4`, not `openai/*`);
`aitelier.reasoning_effort` → the backend's `thought_level` option;
`aitelier.approval_mode` → its sandbox/approval `mode`. Values are validated
against the advertised set and fail fast. `system_prompt`/`max_turns`/
`tool_allowlist` only work on `claude` (Claude Agent SDK via `_meta`); a system
prompt is folded into the prompt for other backends, but `max_turns`/
`tool_allowlist` are rejected there (use `approval_mode` for tool access).

The agent path **hard-rejects** OpenAI fields it can't honestly map:
`tools`, `tool_choice`, `n>1`, `top_p`. Silent drops are an anti-pattern.
(`tools`/`tool_choice` have one opt-in escape hatch: set
`aitelier.allow_tool_drop = true` to drop them server-side instead — for
transports that emit a global toolset they can't suppress per-request.
`n>1` and `top_p` are always rejected.)

### Control plane — aitelier-native

- `POST /v1/runs` — submit a long-running async agent; webhook on completion
- `GET  /v1/runs[/{id}[/events[/stream]]]` — durable runs + append-only event timeline
- `GET  /v1/runs/active`, `POST /v1/runs/{id}/cancel` — in-flight registry + cancel
- `POST /v1/runs/{id}/wait` — block until a run reaches a terminal state
- `POST /v1/runs/{id}/scores`, `GET /v1/runs/{id}/scores` — eval-framework scoring sink
- `GET  /v1/runs/export` — NDJSON stream of full runs (with captured request_body) for backfill grading
- `GET  /v1/traces[/{id}|/aggregates]` — trace queries + aggregates
- `GET/POST/DELETE /v1/schedules*` — recurring or one-shot jobs
- `GET  /v1/health`, `GET /v1/discovery`, `GET /v1/metrics` — liveness + endpoint inventory + dependency probes + runtime counters
- `GET  /ui` (+ `/` redirect) — read-only static dashboard over `/v1/runs*` + `/v1/traces/aggregates` (no build step; public path, data calls still gated)

All requests accept `X-Correlation-Id` (generated if absent), echoed in
response header + body field + SSE chunks + run metadata.

`/v1/discovery` is the live source of truth at runtime.

## SDKs

Both SDKs share the same shape: a thin `Aitelier` client for the control
plane plus a `.openai()` helper that returns a preconfigured OpenAI client
for inference. The OpenAI SDK is an optional peer dependency.

```python
from aitelier_client import Aitelier

ait = Aitelier(base_url="http://localhost:7777", api_key="...")

# Inference: pass-through to OpenAI SDK
openai = ait.openai()
resp = await openai.chat.completions.create(
    model="agent:claude/claude-sonnet-4-5",
    messages=[{"role": "user", "content": "audit this repo"}],
    extra_body={"aitelier": {"workspace": "/path/to/repo"}},
)

# Control plane: aitelier methods
runs = await ait.list_runs(trace_tag="audit", limit=20)
traces = await ait.recent_traces(status="error")
```

```typescript
import { Aitelier } from "aitelier";

const ait = new Aitelier({ baseUrl: "http://localhost:7777", apiKey: "..." });

const openai = await ait.openai();
const resp = await openai.chat.completions.create({
  model: "agent:claude/claude-sonnet-4-5",
  messages: [{ role: "user", content: "audit this repo" }],
  extra_body: { aitelier: { workspace: "/path/to/repo" } },
} as any);

const runs = await ait.listRuns({ traceTag: "audit", limit: 20 });
```

## Project structure

- `core/src/aitelier/` — Python core
  - `server.py` — FastAPI app bootstrap + lifespan + agent-execution orchestration + the helpers each endpoint module imports lazily (idempotency wrappers, render, probes, webhooks, SSE framing). Routers are included from `endpoints/`; middleware from `middleware.py`.
  - `endpoints/` — one router per resource. Handlers lazy-import shared helpers from `server.py` to avoid module-load cycles.
    - `inference.py` — `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`
    - `runs.py` — `/v1/runs`, `/v1/runs/{id}*` (events, wait, cancel, scores), `/v1/runs/active`, `/v1/runs/export`
    - `schedules.py` — `/v1/schedules*`
    - `traces.py` — `/v1/traces*`
  - `otel.py` — opt-in OTLP GenAI export (off by default; SDK lazy-imported only when `[otel] enabled = true`).
  - `middleware.py` — HTTP middleware stack (auth → correlation → body_size → rate_limit), registered on the app via `register_middleware(app)`.
  - `idempotency.py` — `Idempotency-Key` check/record/release + per-key locks (process-local; DB `ON CONFLICT` is the cross-process safety net).
  - `openai_compat.py` — request/response models (`ChatCompletionRequest`, `AsyncRunRequest`, `ScheduleRequest`, …) + OpenAI ↔ aitelier translation.
  - `providers/`
    - `llm.py` — LiteLLM passthrough for non-Ollama models; shared `LLMError` + `get_shared_client`.
    - `ollama.py` — direct `/api/chat` bypass for `local` / `ollama/*` (LiteLLM's Ollama adapter drops `message.thinking`).
    - `sandbox_agent.py` — high-level ACP session orchestration (`call_via_sandbox`, `call_via_sandbox_stream`, `_build_session_new_meta`, event translation).
    - `acp_transport.py` — ACP-over-HTTP wire layer (`AcpClient`, URL scrubbing, preflight warnings, run-row stamping).
  - `storage/` — `Store` protocol + factory in `_store.py`; `postgres.py` (asyncpg-backed) and `inmemory.py` (process-local, tests + DSN-less dev) implement it; `models.py` carries the dataclasses + `AGGREGATE_GROUP_KEYS`; `migrations/` holds the SQL.
  - `sandbox_proxy.py` — SA workflow choreography (install / commands / files / sidecars / artifacts).
  - `purge_worker.py` — background trim of idempotency keys, terminal webhooks, old events.
  - `webhook_worker.py` — durable webhook delivery worker.
  - `schedules.py` — recurring / one-shot job tick loop.
  - `runner.py`, `runs.py` — run id helper + state-machine recording (`record_run`, `start_run`, `_finalize_terminal`).
  - `config.py`, `errors.py`, `security.py`, `cli.py`
- `schemas/v1/` — JSON Schema source of truth for *control plane* wire format
- `sdks/python/` — Python SDK (`aitelier_client`); inference via `Aitelier.openai()`
- `sdks/python-mcp/` — MCP server (`aitelier-mcp`) exposing the control plane as tools
- `sdks/typescript/` — TypeScript SDK (`aitelier`); inference via `Aitelier.openai()`
- `examples/` — runnable recipes (fan-out, MCP orchestrator, scheduled audit, webhook receiver)
- `docker/` — Postgres (always-on) + LiteLLM proxy + optional Ollama profile
- `scripts/` — start.sh, stop.sh, release.sh, doctor.sh, backup.sh, restore.sh, generate-types.sh, supervise.sh + install/uninstall-launchd.sh (macOS always-on)
- `core/src/aitelier/static/ui.html` — the read-only `/ui` dashboard (vanilla, no build)
- `runs/` — gitignored agent run output (prompt, manifest); durable state lives in Postgres

## Infrastructure

Dockerized Postgres + LiteLLM proxy + host-installed aitelier service + host-installed
Sandbox Agent binary (Rivet's coding-agent runtime). Credentials are extracted
from Claude Code and Codex CLI credential files (`~/.claude/.credentials.json`,
`~/.codex/auth.json`) — no manual API keys needed.

```bash
claude login              # once — OAuth login
make install              # one-time setup
make start                # Postgres + LiteLLM + Sandbox Agent + aitelier
make stop                 # stops everything
make test                 # runs all tests
```

- **Postgres** on `localhost:5433` — durable run/event/schedule/webhook state.
- **LiteLLM proxy** on `localhost:4000` — model routing, caching, cost tracking.
- **Sandbox Agent** on `localhost:2468` (or dynamic) — runs coding agents in isolation;
  `scripts/start.sh` installs the Rust binary on first run.
- **Ollama** — opt-in via `[ollama] mode = "docker"` in `aitelier.toml`. On macOS the
  default is "host" because Docker Desktop has no Metal/MPS passthrough.

Available models (via LiteLLM): `local`, `claude-sonnet`, `claude-haiku`, `nomic-embed-text`,
plus any `anthropic/*`, `openai/*`, `ollama/*` pass-through string.

Available agent backends (via Sandbox Agent): `claude`, `codex`, `opencode`, `cursor`, `amp`, `pi`
(live list at `GET /v1/discovery → dependencies.sandbox_agent.agents`).

## Configuration

**Zero env-var reads in app code.** All configuration is TOML. Layered in this order
(each layer overrides keys in the prior one):

1. **Defaults** — dataclass fields in `core/src/aitelier/config.py`.
2. **Base** — explicit `--config <path>`, else `./aitelier.toml`, else
   `~/.config/aitelier/config.toml`.
3. **Secrets overlay** — `aitelier.secrets.toml` next to the base config
   (gitignored). Same TOML shape; keys here override base. Put API keys,
   tokens, and webhook secrets here.
4. **Session overlay** — `runs/.session.toml` (gitignored, ephemeral). Written
   by `scripts/start.sh` for runtime-discovered values (chosen sandbox-agent
   port, dev Postgres DSN). Removed by `scripts/stop.sh`.

```toml
# aitelier.toml — example
[database]
url = "postgresql://aitelier:aitelier_local@localhost:5433/aitelier"

[litellm]
base_url = "http://localhost:4000"

[sandbox_agent]
base_url = "http://localhost:2468"

[service]
host = "127.0.0.1"
port = 7777
log_format = "human"   # or "json" for aggregator-friendly logs

[storage]
max_metadata_bytes = 65536

[ollama]
mode = "host"          # or "docker"

# Put secrets in aitelier.secrets.toml (gitignored), not here:
# [litellm]       api_key = "..."
# [service]      api_key = "..."    # enables hosted-mode Bearer auth
# [service]      webhook_secret = "..."
# [sandbox_agent] token = "..."
```

The SDK clients (Python + TypeScript) read `~/.config/aitelier/config.toml`'s
`[service]` for their default `baseUrl`; pass an explicit `base_url` to override.

## Run state machine

Every inference call records a row in `runs` and transitions through:

```
pending → running → {completed | failed | cancelled | orphaned}
```

`orphaned` is set on aitelier startup for any row left in `pending`/`running` from a
previous process — Sandbox Agent has no session-resume API today, so those sessions
are unrecoverable. Dashboards should treat `orphaned` as a terminal failure mode.

## Error types

Classification lives in `core/src/aitelier/errors.py`. Full table with consumer
guidance: [`docs/INTEGRATION.md`](docs/INTEGRATION.md) → "Error handling".

## Key decisions

- **OpenAI shape as the inference contract.** LiteLLM (LLM path) and Sandbox Agent
  (agent path) both surface through `/v1/chat/completions` + `/v1/embeddings`.
  Consumers point any OpenAI SDK at aitelier and get full ecosystem leverage
  (eval frameworks, notebooks, tutorials, drop-in replacements).
- **Aitelier-native control plane** for durable run state, traces, schedules.
  These have no OpenAI equivalent and stay as first-class aitelier endpoints.
- Python core, TypeScript + Python SDKs.
- LiteLLM proxy for LLM calls (not library mode).
- Agent delegation via **Rivet's Sandbox Agent** (speaks ACP); all coding agents
  run isolated. No direct subprocess invocation of `claude` / `codex`.
- **Postgres** for durable run/event/schedule/webhook state; `InMemoryStore`
  fallback for tests and `[database] url`-less dev.
- Webhook delivery is durable: queued in Postgres, retried with exponential
  backoff (1s / 5s / 30s / 5min / 1hr), failed on the 6th attempt.
- **Agent workflow consolidation**: `aitelier.prepare` + `aitelier.artifacts` lets one
  HTTP call orchestrate install → commands → file seed → sidecars → agent → artifacts.
  Edge cases beyond this workflow hit Sandbox Agent directly via the URL in
  `/v1/discovery`.
- Agent LLM calls happen inside the Sandbox Agent process and go directly to
  Anthropic/OpenAI, bypassing LiteLLM — so there's no cost header to read.
  `cost_usd` is instead **estimated** from the reported token counts (incl.
  prompt-cache read/write) × a per-model rate table (`pricing.py`), which is
  drift-checked against LiteLLM's maintained map (`scripts/check-model-prices.py`).
  It's `null` only when the model can't be priced — and since `agent:<backend>`
  now **requires** an explicit inner model (`agent:<backend>/<model>`), the
  model is always known. Token usage is captured whenever the backend surfaces it.

## Conventions

- Generated types in `_generated/` dirs — never hand-edit
- `run_id` is a 128-bit hex value (a W3C/OTel trace id); `trace_id == run_id`
- Run directories on disk (when written): `runs/{run_id}/` (prompt, manifest)
- Durable state in Postgres tables: `runs`, `run_events`, `run_scores`,
  `schedules`, `webhook_deliveries`, `idempotency_keys`, `schema_version`
- API versioning: `/v1/` prefix
- Lockstep versioning across all packages
- Kind values: `complete`, `embed`, `agent` (internal — runs.kind)

## Build & run

```bash
make install              # install all deps (Python + TypeScript)
make test                 # run all tests (Python unit + smoke, TypeScript)
make test-py              # Python tests only
make test-ts              # TypeScript only
make lint                 # ruff + tsc
make start                # start infra + service
make stop                 # stop everything
make restart              # restart just the service (after editing core/)
make logs                 # tail service + sandbox-agent logs
make status               # what's running + log paths + dep healthchecks
make doctor               # preflight: ports, tools, creds, docker
make backup               # pg_dump → backups/ (retention-pruned)
make restore FILE=...     # restore from a backup dump (confirmed)
make service-install      # macOS launchd: auto-start at login + restart + daily backup
make service-uninstall    # remove the launchd agents
make reset                # nuclear: stop + drop Postgres volume + wipe runs/
make clean                # remove venv, build artifacts
./scripts/release.sh X.Y.Z  # lockstep version bump
```
