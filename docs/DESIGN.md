# aitelier — design doc

The architecture and the tradeoffs behind it. For the current state of
the system see [PLAN.md](./PLAN.md); for the consumer-facing surface see
[INTEGRATION.md](./INTEGRATION.md).

## Summary

`aitelier` is a self-hosted gateway and control plane for LLM inference and coding-agent delegation,
exposed as a local HTTP service plus thin SDKs (Python + TypeScript). The
public contract is OpenAI shape (chat completions, embeddings, models) with
an aitelier-native control plane for durable run state, traces, schedules,
and async submissions.

It composes existing libraries (LiteLLM, Sandbox Agent) with a small
amount of glue, deliberately avoiding framework-building until specific
friction justifies it.

The name is a portmanteau of "AI" and "atelier" (a craftsperson's workshop).

## Goals

- **Speak OpenAI shape for inference**, so any OpenAI-ecosystem tool can
  point at aitelier and route through LiteLLM or Sandbox Agent transparently.
- **Survive long-running agent calls**: closed-laptop tolerance via remote
  sandbox execution where it matters; local execution where it doesn't.
- **Be observable**: every run is traceable through `/v1/runs*` and
  `/v1/traces*`, with correlation IDs, durable state, and an append-only
  event timeline.
- **Be small enough to understand** in an afternoon. Single-developer
  maintainable indefinitely.

## Non-goals

- **Not a commercial or supported product.** Maintained as an open-source
  project — no SLAs or dedicated support. Version discipline exists to
  track what changed (see "Versioning").
- **Not a multi-tenant or multi-user system.** Single developer, single
  laptop primarily, occasionally remote sandboxes.
- **Not a replacement for any underlying tool.** Composes LiteLLM and
  Sandbox Agent rather than wrapping or hiding them.
- **Not a full agent orchestrator.** Plain `asyncio.gather` for concurrency.
  Convergent loops live in ralphx, not here.
- **Not an authoritative web service.** The HTTP server is for localhost
  invocation by local tools, not internet exposure.

## Background

The motivation: AI work currently fragments across LLM provider SDKs, coding
agent CLIs, and ad-hoc scripts. Comparing outputs across providers requires
repetitive setup. Long-running coding agent tasks die when the laptop sleeps.
Cost and behavior visibility is fragmentary. There's no shared vocabulary for
tasks like "audit this repo" or "research this topic" that recur across work.

ralphx (sibling project) already solves the *autonomous coding loop* problem
— PRD-driven convergence, quality gates, budget enforcement, hint injection,
resumability. Different shape of work; different tool.

aitelier solves the *fan-out dispatch* problem and additionally provides a
unified call layer that ralphx delegates to for its agent invocations. This
puts agent-call infrastructure (provider selection, retries, cost tracking,
observability) in one place across both tools.

Existing libraries cover pieces but not the composition:

- **LiteLLM**: solves LLM provider unification, retries, cost tracking,
  observability. Doesn't address coding agents.
- **Sandbox Agent**: solves coding agent unification, sandbox execution,
  session event streams. Doesn't address LLM calls or task definitions.
- **Vercel AI SDK + ACP**: gives a unified call site across LLMs and
  ACP-compatible agents in TypeScript. Used directly in the TS SDK where
  it earns its keep; not the right primitive for the Python side.
- **LangGraph / PydanticAI / agent frameworks**: solve in-process agent
  orchestration. Overkill for fan-out workloads; deferred until needed.

The gap is a thin glue layer that defines tasks, dispatches them to the right
provider type, persists results, exposes a service interface, and provides
observability — without becoming its own framework.

## Language and project shape

aitelier is a multi-package, multi-language project. The split:

- **Core (Python).** The actual aitelier — task runner, fan-out, provider
  adapters, CLI, HTTP server. Where the logic lives.
- **Python SDK.** Thin client wrapping the HTTP service. Used from
  scripts and notebooks. Same operations as the TS SDK, idiomatic Python.
- **TypeScript SDK.** Thin client wrapping the HTTP service. Used
  from ralphx and any future TS tooling. Same operations as the Python SDK,
  idiomatic TS.
- **Shared schemas.** JSON Schema documents defining the wire format. Both
  SDKs generate types from these. The schemas are the source of truth.

Python was chosen for the core because:
- Existing scripts and tooling already in Python.
- LiteLLM is Python-native; the LLM call path is shorter without language
  hop.
- Comfort and velocity for the developer.

TypeScript SDK is required because ralphx is TypeScript and needs ergonomic
access to the service. Python SDK is included for symmetry — when invoking
aitelier from scripts and notebooks, it should feel native.

## High-level architecture

```
                       ┌────────────────────────┐
                       │  Consumer apps (TS)    │ ──┐
                       └────────────────────────┘   │
                                                    │
                       ┌────────────────────────┐   │
                       │ Your Python scripts    │ ──┤ HTTP
                       │ Jupyter notebooks      │   │
                       └────────────────────────┘   │
                                                    ▼
                                    ┌─────────────────────────────┐
                                    │   aitelier (TS SDK)         │
                                    │   aitelier_client (Python)  │
                                    └────────────┬────────────────┘
                                                 │ HTTP/SSE
                       ┌────────────────────────────────────────┐
                       │      aitelier service (FastAPI)        │
                       │  /v1/chat/completions  /v1/embeddings  │
                       │  /v1/models  /v1/runs*  /v1/traces*    │
                       │  /v1/schedules*  /v1/discovery /health │
                       └────────────────┬───────────────────────┘
                                        │ routes by `model` prefix
                       ┌────────────────▼────────────────┐
                       │  openai_compat.parse_model_route│
                       │  + state-machine + event timeline│
                       └────┬───────────────────────┬────┘
                            │                       │
                  ┌─────────▼────────┐    ┌─────────▼──────────┐
                  │   complete/embed │    │  call_via_sandbox  │
                  │    (LiteLLM)     │    │   (Sandbox Agent)  │
                  └─────────┬────────┘    └─────────┬──────────┘
                            │                       │
                            ▼                       ▼
                  ┌──────────────────┐    ┌────────────────────┐
                  │  LLM providers   │    │   Coding agents    │
                  │  via LiteLLM     │    │  via Sandbox Agent │
                  └──────────────────┘    └────────────────────┘

  Durable run/event/schedule/webhook state → Postgres (InMemoryStore fallback)
```

The HTTP service is the contract. The CLI (`aitelier serve|runs|status|...`)
is operational tooling, not a task-runner entry point.

## Components

### Primitives

Three call kinds, exposed over OpenAI-shape HTTP and routed by `model` prefix:

- **`complete`** — chat completion (messages, system prompt, temperature,
  `response_format`, streaming) via LiteLLM proxy.
- **`embed`** — batch embeddings via LiteLLM proxy.
- **`agent`** — agent run with MCP tools, `aitelier.prepare` (env setup)
  and `aitelier.artifacts` (file fetch) phases. Sync via
  `/v1/chat/completions`, async via `POST /v1/runs` with webhook delivery.

Each inference call records a row in the `runs` table and emits to the
append-only `run_events` timeline. `openai_compat.parse_model_route(model)`
decides LLM vs agent path — no named-task lookup, no fanout, no diffing.

### Result shape

Inference responses are **OpenAI's** ChatCompletion / ChatCompletionChunk /
CreateEmbeddingResponse shapes, extended with two aitelier fields stamped
onto every response: `aitelier_run_id` and `correlation_id`. Schema is
documented at
[platform.openai.com](https://platform.openai.com/docs/api-reference).

Internal sandbox-agent results (the dicts `call_via_sandbox` returns) are
aitelier-shape and live behind the translation helpers in
`openai_compat.py`; consumers never see them.

Control-plane responses (`Run`, `RunEvent`, `Schedule`, `Discovery`, …) are
aitelier-shape, defined in `schemas/v1/*.schema.json`. SDKs maintain
parallel control-plane types in `_generated/`.

### Provider adapters

`chat_completion(...)` / `chat_completion_stream(...)` / `embeddings(...)`
in `providers/llm.py` — forward OpenAI-shape requests to the LiteLLM proxy
via httpx and return its response unchanged. Honor `response_format`
(including `json_schema`) with per-provider capability gates that
hard-reject unsupported combinations rather than silently downgrade.

`call_via_sandbox(name, prompt, ...)` in `providers/sandbox_agent.py` —
opens an ACP session against Sandbox Agent, streams notifications as
run_events, aggregates the terminal turn result. `call_via_sandbox_stream`
is the underlying async iterator; the sync wrapper consumes it and returns
the final aggregated dict.

None of these know about HTTP, run state, or schedules — that's the
runner's job.

### HTTP service

`aitelier serve` starts a FastAPI app on `localhost:7777` (configurable).
Full request/response shapes live in [`INTEGRATION.md`](./INTEGRATION.md);
`GET /v1/discovery` is the runtime source of truth for the endpoint list.

Highlights:

- **OpenAI-shape inference** — `POST /v1/chat/completions` (sync + stream),
  `POST /v1/embeddings`, `GET /v1/models`. Model routing by prefix: `agent:*`
  hits Sandbox Agent, anything else hits LiteLLM. Agent-specific options
  ride in `extra_body.aitelier.*`.
- **Async agent runs** — `POST /v1/runs` for long-running submissions with
  durable webhook delivery.
- **Durable runs** — `GET /v1/runs*`, `GET /v1/runs/{id}/events*`,
  `GET /v1/runs/active`, `POST /v1/runs/{id}/cancel`,
  `POST /v1/runs/{id}/wait`, `GET/POST /v1/runs/{id}/scores`,
  `GET /v1/runs/export` (NDJSON backfill).
- **Traces** — `GET /v1/traces*`, `GET /v1/traces/aggregates`.
- **Schedules + webhooks** — `GET/POST/DELETE /v1/schedules*`.
- **Discovery** — `GET /v1/discovery`, `GET /v1/health`, `GET /v1/metrics`.

The `/v1/` prefix exists from day one. Future incompatible changes go to
`/v2/`; old endpoints can be retired when no caller uses them.

The server is bound to `localhost` by default and trusts local callers.
Setting `[service] api_key` enables Bearer auth on every `/v1/*` route
except `/v1/health` (see `middleware.py` auth_middleware) — flip it on for
any remote/hosted deployment.

### CLI

`aitelier <command> [args]`

Operational subcommands only — task execution is HTTP-only:
- `aitelier serve [--port 7777]` — start the HTTP service
- `aitelier runs [--last N]` — show recent run records (reads `runs/`)
- `aitelier traces [filters]` — query the durable run store
- `aitelier status` — service + dependency healthcheck
- `aitelier doctor` — preflight diagnostics

Implemented with `argparse`.

### Run directory

Agent invocations create `runs/{ISO-timestamp}_{task}/` for prompt/manifest
artifacts. Durable run state lives in Postgres (`runs` + `run_events`); the
on-disk directory is for human inspection of agent transcripts, not the
system-of-record.

### SDKs

Both SDKs wrap the HTTP service with idiomatic surface for their language.

**Both SDKs split along the same line.** Inference goes through `.openai()`,
which returns a preconfigured OpenAI client. Control plane methods live on
`Aitelier` directly.

```python
ait = Aitelier(api_key="...")
openai = ait.openai()                          # AsyncOpenAI
resp = await openai.chat.completions.create(model="agent:claude/claude-sonnet-4-5", messages=[...])

# Control plane:
await ait.list_runs(trace_tag="audit")
await ait.cancel_run(run_id)
await ait.submit_run(model="agent:claude/claude-sonnet-4-5", messages=[...], webhook_url="...")
await ait.recent_traces(status="error")
```

The OpenAI SDK is an **optional peer dependency** — consumers using only
the control plane don't need to install it.

**Type generation**: hand-curated parallel modules in `_generated/`
(`models.py` for Python, `types.ts` for TypeScript), kept in sync with
`schemas/v1/*.schema.json`. Inference shapes come from `openai`; aitelier
maintains only control-plane types.

**Contract testing**: inference contract is OpenAI's spec — the OpenAI SDK
is the test corpus. Control-plane endpoints are tested via per-SDK unit
suites (`sdks/python/tests/`, `sdks/typescript/tests/`) that mock the HTTP
layer. End-to-end coverage lives in `core/tests/live/` against a running
service.

### Observability

Structured JSON logging (`[service] log_format = "json"`) is the baseline —
every log line carries `correlation_id`, every run carries durable state in
Postgres, every primitive call adds rows to the `run_events` timeline.

Richer LLM observability (per-message tokens, prompt/response capture,
spans) is a deliberate future investment, not currently wired. The
`/v1/traces*` endpoints surface aggregate metrics from the durable store.

### Credentials

Claude Code's OAuth tokens (`~/.claude/.credentials.json`) and Codex's
auth file (`~/.codex/auth.json`) are read directly by Sandbox Agent.
aitelier itself doesn't handle agent credentials.

LiteLLM API keys (for provider routing) live in `aitelier.secrets.toml`
under `[litellm] api_key`. Optional `[service] api_key` enables hosted-mode
Bearer auth on every endpoint except `/v1/health`.

### Sandbox provisioning

Out of scope for `aitelier` itself. The service assumes one of:
- **Local execution**: Sandbox Agent binary running on the host
  (`scripts/start.sh` installs and starts it on `localhost:2468`).
  Suitable for short tasks.
- **Remote sandbox**: a Sandbox Agent server reachable over the network
  (provisioned by user-written setup script). aitelier connects via
  `[sandbox_agent] base_url` + `token` in `aitelier.toml`. Suitable for
  long-running tasks needing closed-laptop tolerance.

## Versioning

Single-user versioning, kept lightweight but real:

- **Semantic versioning** for the project as a whole: major.minor.patch.
  Bumped on tagged releases. No external users to break, but the version
  numbers anchor "what state was this in when I built X."
- **Lockstep across packages**: core, both SDKs, schemas all share the
  version number. A v0.5.2 of aitelier means v0.5.2 of every package in
  the monorepo. Avoids "which SDK version goes with which server version"
  confusion.
- **API path versioning**: `/v1/` from day one. New incompatible endpoints
  go to `/v2/`. Old paths can stay until all callers migrate, then
  removed. In a small project, "all callers migrate" is "I update the few
  places I call from" — fast.
- **Schema versioning by directory**: when a breaking schema change
  happens, schemas live at `schemas/v2/` alongside `schemas/v1/`. Both
  SDKs generate from current; old data files remain readable via the
  archived schemas.
- **No semver discipline below v1.0.0**: the project stays at v0.x while
  the design is settling. Breaking changes don't bump major. When the
  shape feels stable across a few months of use, tag v1.0.0 and start
  taking semver seriously.
- **Tag releases for milestones, not every change**: `v0.1.0` when the
  HTTP service first works. `v0.2.0` when ralphx integration ships.
  `v0.3.0` when streaming lands. Etc. Day-to-day commits don't get
  release tags.
- **Changelog as a single CHANGELOG.md** at the repo root, manually
  curated. No automation; just a place to record "what changed in v0.4
  vs v0.3" for future-you.

The discipline isn't for stability guarantees. It's so that six months
from now, when something breaks, you can answer "what version was
working" and "what changed" without archaeology.

## Repo layout

Illustrative, not exhaustive — see `CLAUDE.md` "Project structure" for the
authoritative module map.

```
aitelier/
├── README.md
├── CHANGELOG.md
├── pyproject.toml                     # workspace root for Python tooling
├── package.json                       # workspace root for TS tooling
├── pnpm-workspace.yaml                # if using pnpm workspaces
├── .gitignore
├── docs/
│   ├── DESIGN.md                      # this doc
│   ├── INTEGRATION.md                 # consumer-facing API + error guide
│   ├── PLAN.md                        # roadmap
│   └── deploy/                        # brig cell + Dockerfile samples
├── schemas/                           # control-plane wire format (inference is OpenAI)
│   └── v1/
│       ├── aitelier_request.schema.json
│       ├── run.schema.json
│       ├── run_event.schema.json
│       ├── run_score.schema.json
│       ├── schedule.schema.json
│       ├── discovery.schema.json
│       ├── health.schema.json
│       ├── active_runs.schema.json
│       ├── cancel.schema.json
│       └── traces_aggregate.schema.json
├── core/                              # the Python core + service + CLI
│   ├── pyproject.toml
│   ├── src/aitelier/
│   │   ├── __init__.py
│   │   ├── cli.py
│   │   ├── server.py                  # FastAPI app bootstrap + lifespan + primitive routes + re-exports
│   │   ├── inference_exec.py          # request prep/validation + agent/LLM orchestration + streaming
│   │   ├── serializers.py             # run/event → dict projections + credential redaction
│   │   ├── probes.py                  # live dependency probes for /v1/discovery + /v1/health
│   │   ├── runtime.py                 # in-flight registry + saturation cap + SSE/webhook infra (leaf)
│   │   ├── endpoints/                 # one router per resource
│   │   ├── middleware.py              # auth → correlation → body_size → rate_limit
│   │   ├── idempotency.py             # Idempotency-Key check/record/release
│   │   ├── openai_compat.py           # request/response models + translation
│   │   ├── otel.py                    # opt-in OTLP GenAI export
│   │   ├── runner.py                  # run-id helper (lean)
│   │   ├── runs.py                    # record_run + finalize state machine
│   │   ├── schedules.py
│   │   ├── webhook_worker.py          # durable delivery queue
│   │   ├── purge_worker.py            # background trim of old rows
│   │   ├── sandbox_proxy.py           # prepare/artifacts workflow choreography
│   │   ├── storage/                   # Store protocol + Postgres + InMemory + migrations
│   │   ├── security.py                # SSRF public-URL gate (webhooks + MCP URLs) + workspace/path validation
│   │   └── providers/
│   │       ├── llm.py                 # LiteLLM passthrough (chat / embed)
│   │       ├── ollama.py              # direct /api/chat bypass for local/ollama
│   │       ├── sandbox_agent.py       # ACP session orchestration
│   │       └── acp_transport.py       # ACP-over-HTTP wire layer
│   └── tests/
├── sdks/
│   ├── python/
│   │   ├── pyproject.toml
│   │   └── src/aitelier_client/
│   │       ├── __init__.py
│   │       ├── client.py              # control plane + .openai() helper
│   │       └── _generated/
│   │           └── models.py          # control-plane Pydantic models
│   ├── python-mcp/                    # MCP server (aitelier-mcp) over the control plane
│   └── typescript/
│       ├── package.json
│       ├── tsconfig.json
│       └── src/
│           ├── index.ts
│           ├── client.ts              # control plane + .openai() helper
│           └── _generated/
│               └── types.ts           # control-plane TS types
├── docker/
│   ├── docker-compose.yml             # Postgres + LiteLLM (+ optional Ollama)
│   └── litellm/                       # LiteLLM proxy config
├── scripts/
│   ├── start.sh / stop.sh             # boot/teardown of infra + service
│   ├── doctor.sh / status.sh          # preflight + ops
│   └── release.sh                     # bump version across all packages
└── runs/                              # gitignored — per-run artifacts + logs
```

`_generated/` directories in each SDK are explicitly marked — those files
mirror `schemas/v1/*.schema.json` and are not hand-edited as part of
day-to-day work.


## Tradeoffs and rationale

**Two SDKs over a single language.** ralphx is TypeScript and earns the
TS SDK; ad-hoc scripts are often Python and benefit from the Python SDK.
Symmetry helps future-you maintain a single mental model across both.
Cost: two implementations, kept in sync via shared schemas and contract
tests.

**Schema-driven type generation.** Both SDKs generate types from JSON
Schema rather than hand-writing. Removes a class of drift bugs. Mandatory
discipline for a small project where no external pressure catches divergence.

**Versioning even for a small project.** Version numbers anchor "what was
working when" and protect future-you from archaeology. Lockstep across
packages avoids per-component version-matrix confusion.

**HTTP service for the integration layer, not direct library import.**
ralphx is TypeScript; cross-language imports aren't an option. Even for
the Python SDK, going through HTTP (rather than importing core directly)
gives uniformity and means both SDKs are exercised the same way.

**Dicts over typed result classes (in core).** Initial cost is
documentation discipline; benefit is no premature abstraction tax in the
hot path. Types live in the SDKs (where users want them), not in core
(which is service plumbing).

**Python over TypeScript for core.** LiteLLM is Python-first; existing
scripts and tooling are Python; ML/data libraries familiar. Cost is
building Sandbox Agent's Python client ourselves.

**LiteLLM proxy mode (not SDK mode).** Centralized model routing,
provider-agnostic auth, and request caching are worth the second hop for
a multi-consumer runtime.

**Sandbox Agent over AgentAPI.** Better architecture (typed events vs.
TUI scraping), better remote-sandbox story, official commitment from
Rivet. Cost: youngest of the options, no Python SDK. Mitigation: build it.

**No abstract `Provider` class in core.** Two thin functions returning
dicts is sufficient for the current call sites. The HTTP boundary is
where dispatch happens; no need for an in-process abstraction.

**Consumers invoke aitelier, not the reverse.** Right layering: control
flow (loops, agent orchestration) lives in the consumer; aitelier provides
the primitive call surface. Each tool stays focused on its actual job.

**Workspace isolation is Sandbox Agent's job.** aitelier doesn't tmpdir-copy
or in-place-edit on behalf of the caller; the sandbox handles isolation
semantics and aitelier passes `workspace` through.

**Durable state in Postgres, transcripts on disk.** Runs/events/schedules/
webhooks need durable querying — Postgres. Agent transcripts are
human-browseable artifacts under `runs/` for inspection; they're not the
system of record.

## Risks

**Sandbox Agent schema drift.** Project at v0.4.x, monthly releases.
Mitigation: pin versions, review changelog, keep dict shape thick enough
that Sandbox Agent specifics don't leak into application code.

**Closed laptop on local agent runs.** No fix; tasks die. Mitigation:
short tasks local, long tasks remote.

**Two SDKs drift.** Real risk for a solo project without external
pressure. Mitigation: schema-driven types, contract test corpus,
"never add a feature to one SDK without the other" discipline.

**HTTP service auth.** Two modes:
- **Localhost-trust** (default) — `127.0.0.1` bind, no auth.
- **Hosted mode** — set `service.api_key`. Every `/v1/*` except
  `/v1/health` requires `Authorization: Bearer <key>`. Always combine
  with TLS termination upstream; Bearer over plain HTTP is unsafe.


## Naming notes

`aitelier` for the project. CLI binary: `aitelier`. Python core package:
`aitelier`. Python SDK package: `aitelier_client` (or `aitelier-py` on
disk). TypeScript SDK package: `aitelier`. Importable as
`from aitelier import Aitelier` in Python (SDK) and
`import { Aitelier } from "aitelier"` in TS.

For shell ergonomics, alias to `ait` if `aitelier` becomes tiresome:

```bash
alias ait='aitelier'
```

## Appendix: alternatives considered

**Provider Protocol with formal types in core.** Rejected. Dicts
sufficient inside the service; SDKs handle types at the boundary.

**Vercel AI SDK + ACP for the TS core.** Rejected — LiteLLM (Python) is
the preferred LLM abstraction. Vercel AI SDK may still appear inside
the TS SDK for specific patterns, but is not the architectural foundation.

**AgentAPI instead of Sandbox Agent.** AgentAPI broader but uses TUI
scraping. Sandbox Agent chosen for proper event model and
remote-sandbox-first design.

**TypeScript SDK only.** Rejected — Python scripting is a real
use case; ad-hoc httpx inconsistent with how other code calls the service.

**LangGraph as the agent framework.** Rejected. Heavy abstractions for
control flow we don't have.

**Web dashboard.** A *full* dashboard stays rejected — observability is
primarily structured JSON logs + `/v1/traces*` queries. But a minimal,
read-only `/ui` now ships: a single static page (no build step) over the
existing GET endpoints (`/v1/runs`, run events, `/v1/traces/aggregates`).
A read/write dashboard (run replay, behavior graphs) remains future work
(see PLAN.md Tier 1).

**Monorepo tooling (Nx, Turborepo).** Rejected as overkill for a small
project; plain pnpm + uv coexisting is simpler.
