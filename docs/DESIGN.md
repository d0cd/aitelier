# aitelier — design doc

**Status:** Draft
**Author:** [you]
**Last updated:** 2026-05-06

## Summary

`aitelier` is a personal toolkit for running AI tasks (audit, research, lint,
implement, summarize, etc.) against multiple providers — LLMs and coding
agents — with the option to fan out across providers for comparison.

It composes existing libraries (LiteLLM, Sandbox Agent, Langfuse) with a small
amount of glue, deliberately avoiding framework-building until specific
friction justifies it.

aitelier runs as both a CLI and a local HTTP service. The service exposes
`execute`, `executeStream`, and related operations that other tools (notably
ralphx) invoke for agent and LLM calls. Two SDKs — Python and TypeScript —
wrap the service for ergonomic in-process use.

The name is a portmanteau of "AI" and "atelier" (a craftsperson's workshop).

## Goals

- **Run named tasks end-to-end** with one command: `aitelier audit ./my-repo`.
- **Dispatch by task kind**: text-only tasks go to LLMs; code/file tasks go
  to coding agents. Each task declares its preferred providers.
- **Fan out for calibration**: same task across multiple providers, outputs
  saved side by side for comparison.
- **Provide a service interface** that other personal tools (ralphx, scripts,
  notebooks) call for their own agent and LLM invocations.
- **Survive long-running tasks**: closed-laptop tolerance via remote sandbox
  execution where it matters; local execution where it doesn't.
- **Be observable**: every run is traceable, every cost is attributable.
- **Be small enough to understand** in an afternoon. Single-developer
  maintainable indefinitely.

## Non-goals

- **Not a published SDK or product.** Personal use, no API stability
  guarantees for external consumers, no migration paths owed to anyone.
  Version discipline exists for self-protection (see "Versioning"), not
  contract.
- **Not a multi-tenant or multi-user system.** Single developer, single
  laptop primarily, occasionally remote sandboxes.
- **Not a replacement for any underlying tool.** Composes LiteLLM, Sandbox
  Agent, and Langfuse rather than wrapping or hiding them.
- **Not a full agent orchestrator.** Plain `asyncio.gather` for concurrency.
  Convergent loops live in ralphx, not here.
- **Not an authoritative web service.** The HTTP server is for localhost
  invocation by personal tools, not internet exposure.

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
- **Python SDK.** Personal-use client wrapping the HTTP service. Used from
  scripts and notebooks. Same operations as the TS SDK, idiomatic Python.
- **TypeScript SDK.** Personal-use client wrapping the HTTP service. Used
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
                       │  ralphx (TS, sibling)  │ ──┐
                       └────────────────────────┘   │
                                                    │
                       ┌────────────────────────┐   │
                       │ Personal Python scripts│ ──┤ HTTP
                       │ Jupyter notebooks      │   │
                       └────────────────────────┘   │
                                                    ▼
                                    ┌─────────────────────────────┐
                                    │   aitelier-ts SDK (TS)      │
                                    │   aitelier-py SDK (Python)  │
                                    └────────────┬────────────────┘
                                                 │ HTTP/SSE
                       ┌────────────────────────────────────────┐
                       │      aitelier service (FastAPI)        │
                       │  /execute /execute/stream /runs /health│
                       └────────────────┬───────────────────────┘
                                        │
                       ┌────────────────▼────────────────┐
                       │       Task runner (core)        │
                       │  workspace prep, dispatch,      │
                       │  event streaming, diffing       │
                       └────┬───────────────────────┬────┘
                            │                       │
                  ┌─────────▼────────┐    ┌─────────▼──────────┐
                  │     call_llm     │    │    call_agent      │
                  │   (LiteLLM)      │    │  (Sandbox Agent)   │
                  └─────────┬────────┘    └─────────┬──────────┘
                            │                       │
                            ▼                       ▼
                  ┌──────────────────┐    ┌────────────────────┐
                  │  LLM providers   │    │   Coding agents    │
                  │  via LiteLLM     │    │  via Sandbox Agent │
                  └──────────────────┘    └────────────────────┘

           All telemetry → Langfuse (LiteLLM callback +
                                     manual instrumentation for agents)
```

The CLI is also still a valid entry point (`aitelier audit ./repo`); it
calls into the same task runner the HTTP service uses. Direct CLI invocation
bypasses the HTTP layer for local convenience.

## Components

### Tasks

A task is a function or JSON object describing a unit of work. Tasks
conform to the shared `task.schema.json`:

```python
def audit_code(workspace: Path, focus: str = "security") -> dict:
    return {
        "name": "audit_code",
        "kind": "agent",
        "prompt": f"Audit the code in this directory for {focus}. ...",
        "workspace": str(workspace),
        "preferred_providers": ["claude-code", "codex"],
        "workspace_mode": "copy",  # or "in_place" for ralphx-style loops
    }
```

Task functions live in `core/tasks/` and are discovered by name. Each task
declares:
- `kind`: `"llm"` or `"agent"`
- `prompt`: the prompt string (may reference files in `prompts/`)
- `preferred_providers`: ordered list of providers
- `workspace` (agent tasks only): directory the agent should operate on
- `workspace_mode`: `"copy"` (isolate, default) or `"in_place"` (ralphx
  manages workspace state)

Tasks own their domain; they don't know about dispatch, persistence, or
service plumbing.

### Result shape

Defined in `result.schema.json`. Every call returns:

```json
{
    "kind": "llm" | "agent",
    "provider": "anthropic/claude-sonnet-4.7" | "sandbox-agent:claude-code",
    "text": "primary output",
    "duration_s": 12.4,
    "status": "ok" | "error",
    "cost_usd": 0.034,
    "error_type": null,
    "error_msg": null,
    "session_id": null,
    "files_changed": null,
    "run_id": "2026-05-06T14-32-00_audit_claude-code"
}
```

The schema is the source of truth. SDKs generate types from it. The core
returns dicts matching it; SDKs surface them as typed objects.

### Call functions

Two thin adapters in core, importable as a library and exposed via HTTP:

`call_llm(model, prompt, timeout=60) -> dict` — wraps `litellm.acompletion`
with `num_retries=3`, `request_timeout=timeout`. Reads cost from
`response._hidden_params["response_cost"]`. Returns the result dict.

`call_agent(name, prompt, workspace, timeout=600, workspace_mode="copy") -> dict`
— wraps the Sandbox Agent client. Creates a session, posts the prompt,
streams events to a JSONL file in the run directory, accumulates text from
`item.delta` events, returns the result dict. Wraps a 3-attempt retry loop
around connection errors and timeouts. Honors `workspace_mode` for tmpdir
copy vs in-place operation.

Neither function knows about tasks, run directories, or HTTP — those are
layered on top.

### Task runner

Given a task spec:

1. **Workspace preparation**: for agent tasks with `workspace_mode: "copy"`,
   copy `task.workspace` to a temporary directory. Real source untouched.
   For `workspace_mode: "in_place"` (ralphx), operate directly on the
   provided directory.
2. **Dispatch**: route to `call_llm` or `call_agent` based on `task.kind`.
3. **Fan-out (optional)**: when invoked with `--fanout` or via fan-out API,
   run all `preferred_providers` concurrently with `asyncio.gather` and a
   `Semaphore` for bounded parallelism.
4. **Streaming**: for agent tasks, stream events to `events.jsonl` in the
   run directory as they arrive. This is the durable transcript.
5. **Diffing**: for `workspace_mode: "copy"` agent tasks, compute file
   diff between the original workspace and the post-run tmpdir. Save as
   `diff.patch` in run dir.
6. **Persistence**: write each result dict as a separate file in the run
   directory. Write a `manifest.json` with all results plus run metadata.

### HTTP service

`aitelier serve` starts a FastAPI app on `localhost:7777` (configurable):

```
POST /v1/execute
  body: TaskSpec (matches task.schema.json)
  response: ResultDict (matches result.schema.json)

POST /v1/execute/stream
  body: TaskSpec
  response: SSE stream of events, terminating with final ResultDict
  used when callers want live progress (e.g., ralphx mid-iteration timeouts)

POST /v1/fanout
  body: { task: TaskSpec, providers: list[str], max_concurrent?: int }
  response: list[ResultDict]

GET /v1/runs/{id}
  response: full run record including events, files_changed, etc.

GET /v1/health
  response: { status, providers_available, langfuse_connected, ... }
```

The `/v1/` prefix exists from day one. Future incompatible changes go to
`/v2/`; old endpoints can be retired when no caller uses them, which for
personal use is when ralphx and your scripts have all migrated.

The server is bound to `localhost` by default. Authentication is not
implemented — this is not a public service. If you ever need remote
invocation, add a token check then; not before.

### CLI

`aitelier <command> [args]`

CLI commands:
- `aitelier <task> [args] [--fanout] [--remote]` — run a named task
- `aitelier serve [--port 7777]` — start the HTTP service
- `aitelier list` — list available tasks
- `aitelier runs [--last N]` — show recent run records
- `aitelier compare <run-id>` — open a comparison view of a fan-out run

Implemented with `argparse` for now. May graduate to `typer` or `click`
when CLI grows.

### Run directory

Each invocation creates `runs/{ISO-timestamp}_{task}/` containing:

```
runs/2026-05-06T14-32-00_audit/
├── manifest.json                # task spec, providers, timing, exit codes
├── prompt.txt                   # exact prompt sent (after template fill)
├── claude-code/
│   ├── result.txt               # final text output
│   ├── result.json              # full result dict
│   ├── events.jsonl             # full event stream
│   └── diff.patch               # file changes the agent made
├── codex/
│   ├── result.txt
│   ├── result.json
│   ├── events.jsonl
│   └── diff.patch
└── compare.md                   # auto-generated side-by-side summary
```

The same layout is used whether the run came from the CLI or the HTTP
service. ralphx's iteration runs land here too — each ralphx iteration
that goes through aitelier produces a normal run record under
`runs/`, and ralphx records the run IDs in its own iteration history.

Format is intentionally human-browseable; no database.

### SDKs

Both SDKs wrap the HTTP service with idiomatic surface for their language.

**Operations are identical across both:**
- `execute(taskSpec) -> Result`
- `executeStream(taskSpec) -> AsyncIterable<Event>` (Python: `async for`)
- `fanOut(taskSpec, options) -> Result[]`
- `getRun(runId) -> RunRecord`
- `health() -> HealthStatus`

**TypeScript SDK** (`sdks/typescript/`):

```typescript
import { Aitelier } from "aitelier";

const client = new Aitelier({ baseUrl: "http://localhost:7777" });

const result = await client.execute({
  name: "audit_code",
  kind: "agent",
  prompt: "Audit this for security issues",
  workspace: "/path/to/repo",
  preferredProviders: ["claude-code"],
});

for await (const event of client.executeStream(taskSpec)) {
  if (event.type === "item.delta") process.stdout.write(event.data.text);
}
```

Conventions: camelCase fields (transformed at boundary), AsyncIterable for
streams, discriminated unions for events, no global state.

**Python SDK** (`sdks/python/`):

```python
from aitelier import Aitelier

async with Aitelier(base_url="http://localhost:7777") as client:
    result = await client.execute(
        name="audit_code",
        kind="agent",
        prompt="Audit this for security issues",
        workspace="/path/to/repo",
        preferred_providers=["claude-code"],
    )

    async for event in client.execute_stream(task_spec):
        if event.type == "item.delta":
            print(event.data.text, end="", flush=True)
```

Conventions: snake_case fields, async-first with `Aitelier.sync()` for
sync callers, Pydantic models, context manager for connection lifecycle.

**Type generation**: `datamodel-code-generator` for Python (JSON Schema →
Pydantic), `json-schema-to-typescript` for TS. Wired into
`scripts/generate-types.sh`. Generated types live in `_generated/` in each
SDK and are not hand-edited.

**Contract testing**: `tests/contract/` contains JSON test cases (input
spec, expected response shape, expected events). Both SDKs run their tests
against the same corpus. Divergence is caught immediately rather than
discovered later.

### Observability

LiteLLM is configured with `litellm.callbacks = ["langfuse"]` at module
import. Every LLM call is automatically traced. For agent calls,
instrument manually with the Langfuse Python SDK:

```python
trace = langfuse.trace(name=task["name"])
gen = trace.generation(name=f"agent:{name}", model=name, input=prompt)
# ... run agent ...
gen.end(output=text, metadata={"session_id": session_id, "run_id": run_id})
```

Self-hosted Langfuse via Docker Compose locally; cloud free tier
acceptable for early use. Same Langfuse instance used by aitelier and (via
service delegation) ralphx, so traces are unified across both tools.

### Credentials

Handled by `agent-auth` (already built). Run `agent-auth check` at start
of any session; run `agent-auth ensure` interactively when something's
expired. CI and remote sandbox contexts use API keys via env vars
(propagated by `agent-auth export`). Local interactive use leverages OAuth
Max plans via the keychain.

The HTTP service inherits whatever credentials are present in its
environment when started. SDKs do not handle credentials directly; they
trust the service to be authenticated.

### Sandbox provisioning

Out of scope for `aitelier` itself. The service assumes one of:
- **Local execution**: subprocess or Sandbox Agent embedded mode directly
  on the laptop. Suitable for short tasks.
- **Local Docker**: a maintained `agent-sandbox:latest` image, run with
  credentials propagated via `agent-auth export --format docker`. Suitable
  for tasks needing filesystem isolation but not durability.
- **Remote sandbox**: E2B, Daytona, or similar, provisioned by user-written
  setup script. aitelier connects to the resulting Sandbox Agent server.
  Suitable for long-running tasks needing closed-laptop tolerance.

A `--remote` flag in task specs selects the third path; default is local.
Provider configuration lives in `config/sandbox.toml` (TBD).

## Versioning

Personal-use versioning, kept lightweight but real:

- **Semantic versioning** for the project as a whole: major.minor.patch.
  Bumped on tagged releases. No external users to break, but the version
  numbers anchor "what state was this in when I built X."
- **Lockstep across packages**: core, both SDKs, schemas all share the
  version number. A v0.5.2 of aitelier means v0.5.2 of every package in
  the monorepo. Avoids "which SDK version goes with which server version"
  confusion.
- **API path versioning**: `/v1/` from day one. New incompatible endpoints
  go to `/v2/`. Old paths can stay until all callers migrate, then
  removed. For personal use, "all callers migrate" is "I update the two
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

```
aitelier/
├── README.md
├── CHANGELOG.md
├── pyproject.toml                     # workspace root for Python tooling
├── package.json                       # workspace root for TS tooling
├── pnpm-workspace.yaml                # if using pnpm workspaces
├── .gitignore
├── docs/
│   ├── design.md                      # this doc
│   ├── hypotheses.md                  # experiment log
│   └── api.md                         # OpenAPI dump from FastAPI
├── schemas/                           # source of truth for wire format
│   └── v1/
│       ├── task.schema.json
│       ├── result.schema.json
│       └── events.schema.json
├── core/                              # the Python core + service + CLI
│   ├── pyproject.toml
│   ├── src/aitelier/
│   │   ├── __init__.py
│   │   ├── cli.py
│   │   ├── server.py                  # FastAPI app
│   │   ├── runner.py
│   │   ├── fanout.py
│   │   ├── workspace.py               # tmpdir + diffing
│   │   ├── observability.py
│   │   └── providers/
│   │       ├── llm.py                 # call_llm
│   │       └── agent.py               # call_agent
│   ├── tasks/                         # task definitions
│   │   ├── __init__.py
│   │   ├── audit.py
│   │   ├── research.py
│   │   ├── lint.py
│   │   ├── implement.py
│   │   └── summarize.py
│   ├── prompts/                       # versioned prompts
│   └── tests/
├── sdks/
│   ├── python/
│   │   ├── pyproject.toml
│   │   └── src/aitelier_client/
│   │       ├── __init__.py
│   │       ├── client.py
│   │       ├── streaming.py
│   │       └── _generated/
│   │           └── models.py          # generated from schemas
│   └── typescript/
│       ├── package.json
│       ├── tsconfig.json
│       └── src/
│           ├── index.ts
│           ├── client.ts
│           ├── streaming.ts
│           └── _generated/
│               └── types.ts           # generated from schemas
├── tools/
│   ├── agent_auth.py                  # the credential bootstrap script
│   └── compare.py                     # ad-hoc comparison utility
├── docker/
│   ├── Dockerfile.agent-sandbox
│   └── entrypoint.sh
├── config/
│   ├── agents.toml                    # agent-auth config
│   └── sandbox.toml                   # sandbox provider config (TBD)
├── scripts/
│   ├── generate-types.sh              # regenerates types from schemas
│   ├── test-contract.sh               # runs contract tests in both SDKs
│   └── release.sh                     # bump version across all packages
├── tests/
│   └── contract/                      # JSON test cases for both SDKs
└── runs/                              # gitignored
```

`tasks/` and `prompts/` deliberately top-level inside `core/` (still
visible). `_generated/` directories in each SDK are explicitly marked —
those files regenerate from schemas, not hand-edited.

## Build phases

### Phase 0: validation (this week)

- One real task in `core/tasks/`.
- `call_llm` only, using LiteLLM directly via the CLI.
- Run it. Look at output. Form opinion.
- **Run hypotheses H1, H2, H3 (see below).** These collapse the design
  space dramatically and should happen before any infrastructure.

Goal: confirm the basic loop produces something useful before building
service or SDK infrastructure.

### Phase 1: schemas and core (week 1-2)

- Lock `task.schema.json`, `result.schema.json`, `events.schema.json`.
- Set up type generation pipeline (`generate-types.sh`).
- Build core CLI: `call_llm`, `call_agent` (subprocess form initially —
  no Sandbox Agent client yet), task runner, workspace prep, diffing,
  run directory format, `--fanout` flag.
- Set up Langfuse integration.
- **Run H4 (rate limits) and H9 (parallel runs).**

Goal: the CLI works end-to-end against real LLMs and via subprocess
against Claude Code. Schemas validate. Type generation works.

### Phase 2: HTTP service (week 2-3)

- Build FastAPI server exposing `/v1/execute`, `/v1/execute/stream`,
  `/v1/fanout`, `/v1/runs/{id}`, `/v1/health`.
- Validate against the same schemas the SDKs will use.
- Manual testing with `curl` to confirm the wire format works.
- **Run H5 (HTTP overhead).**

Goal: aitelier callable as a service from any HTTP-speaking client.

### Phase 3: SDKs in parallel (week 3)

- Build Python SDK (`aitelier_client`): generated Pydantic models, async
  HTTP client, streaming via httpx-sse, sync wrapper.
- Build TypeScript SDK (`aitelier`): generated TS types, async fetch
  client, streaming via native EventSource or `eventsource-parser`.
- Set up contract test corpus; both SDKs pass it.
- **Run H6 (SSE ordering) and H10 (result-dict shape).**

Goal: ergonomic invocation in both languages, kept in sync via shared
schemas and tests.

### Phase 4: Sandbox Agent client — DONE

- Sandbox Agent (rivet-dev/sandbox-agent v0.4.x) installed + supervised by
  `scripts/start.sh`. Port resolution: CLI flag → env var → 2468 → dynamic.
- `providers/sandbox_agent.py` — ACP (Agent Client Protocol) client speaking
  JSON-RPC over the Sandbox Agent's HTTP + SSE transport.
- `providers/agent.py` reduced to a thin compat shim; direct `claude`/`codex`
  subprocess paths and the codex-config-toml generator deleted.
- `/v1/discovery` probes the sandbox server's `/v1/agents` and reports the
  available agent backends (claude-code, codex, opencode, cursor, amp, pi).
- All sandboxing, env scoping, MCP wiring, and event normalization is now
  Sandbox Agent's responsibility — aitelier just speaks ACP to it.

Deferred follow-ups (not blockers): wire `tool_allowlist` and `max_turns`
through `session/set_config_option` once Sandbox Agent documents the
canonical config keys; expose ACP `session/update` events to streaming
consumers via a new `/v1/agent/stream` endpoint (separate hypothesis).

### Phase 5: ralphx integration (week 4)

- Replace ralphx's direct Claude Code spawning with TypeScript SDK
  calls to aitelier.
- Verify cost and event traces unify across both tools in Langfuse.
- Tag aitelier v0.2.0; update ralphx to depend on this version.
- **Run H11 (ralphx loop composition).**

Goal: ralphx loops use aitelier as their agent invocation primitive.

### Phase 6: as friction demands

Each item below is built when it becomes the thing that's annoying:

- Sandbox provisioning automation.
- Compose tasks (research → implement pipelines).
- Custom-agent framework (PydanticAI or LangGraph) when control flow
  needs more than `asyncio`.
- CLI ergonomics (when `argparse` strains).
- Remote-sandbox session resume (when you've actually lost work).
- Evals harness (when "vibes" stops being enough for provider selection).
- v1.0.0 cut (when the design feels stable across multiple months of use).

No items in Phase 6 are scheduled. Each waits for sentence-shaped pain.

## Hypotheses and experiments

Design decisions are bets. This section enumerates the ones that are
testable, what evidence would confirm or refute each, and how to actually
run the test. Each experiment is small enough to execute in hours-to-days,
not weeks.

The discipline: if a hypothesis fails, update the design rather than
patching around the failure. A failed hypothesis in week one is cheap; a
failed hypothesis discovered in month three after building three layers
on top is expensive.

Track results in `docs/hypotheses.md` with the date, the result, and what
changed in the design. When a hypothesis is falsified, update the
relevant design section here and link to the falsification.

### Provider behavior hypotheses (run before Phase 1)

These are the highest-leverage tests: they validate the *premise* of the
project before any infrastructure is built. Run them first.

**H1: Different coding agents produce meaningfully different audit
outputs.**

If true, fan-out across multiple agents is justified for audit-style
tasks. If false, pick one and stop fanning out.

- *Experiment:* Three real codebases of different sizes. Identical
  security audit prompt across Claude Code, Codex, and one Sonnet LLM
  call. Compare outputs subjectively.
- *Prediction:* Claude Code and Codex 50-70% overlapping findings, each
  catches ~20% the other misses. Sonnet shallower without
  project-specific context.
- *Falsification:* >90% overlap means fan-out is mostly redundant.
- *Timeline:* Half a day. Phase 0.

**H2: Coding agents add value over LLMs for tasks involving multi-file
context.**

If true, the agent/llm distinction is meaningful. If false, the
distinction is overhead and most tasks should be LLM calls.

- *Experiment:* Multi-file task ("explain the auth flow"). Run via
  Claude Code (agent) vs Sonnet (LLM with relevant files pasted in).
- *Prediction:* Claude Code wins when relevant files aren't obvious;
  Sonnet ties or wins when right files can be supplied explicitly.
- *Falsification:* Sonnet wins when given obvious files → "prepare
  context, then call LLM" pattern is preferable.
- *Timeline:* Half a day. Phase 0.

**H3: For text-only tasks (research, summarize), provider differences
are small enough that the cheapest competent provider wins by default.**

If true, default routing should be cost-optimized. If false, more
nuanced routing is justified.

- *Experiment:* Same research and summarize tasks across Sonnet, Haiku,
  GPT-5, and a strong open-source model via Ollama. Quality and cost.
- *Prediction:* Smaller quality differences than expected for summarize.
  Research favors more capable models. Cost-per-quality favors Haiku
  for summarize, Sonnet for research.
- *Falsification:* Large quality differences within text tasks → "text
  task" isn't a meaningful routing category.
- *Timeline:* Half a day. Phase 0.

### System behavior hypotheses

**H4: A bounded fan-out (4 concurrent) doesn't trigger Max plan rate
limits for normal interactive workloads.**

- *Experiment:* Fan out an audit task to four providers (two via Max
  plan OAuth, two via API keys) ten times in a row, with a 30-second
  gap between runs. Watch for rate-limit errors.
- *Prediction:* No rate limits at this rate. Limits triggered at 10+
  concurrent or sustained fan-out without gaps.
- *Falsification:* Any rate limits hit → lower default `max_concurrent`
  to 2; document the threshold.
- *Timeline:* An hour, monitoring across a day. Phase 1.

**H5: The HTTP service's per-call overhead is negligible compared to
agent execution time.**

- *Experiment:* Time 100 short LLM calls (`generateText("ping")`)
  directly through LiteLLM vs through the aitelier HTTP service. Median
  overhead.
- *Prediction:* HTTP adds <50ms median overhead. Agent calls are
  seconds-to-minutes; this is in the noise.
- *Falsification:* Overhead exceeds 200ms median → profile and either
  fix or document a `local-only` mode that bypasses HTTP for in-process
  Python.
- *Timeline:* An hour. Phase 2.

**H6: SSE streaming through the HTTP boundary preserves event ordering
and doesn't drop events under load.**

- *Experiment:* Run a long-streaming agent task through
  `/v1/execute/stream` ten times. Compare SSE event sequence against
  events written to `events.jsonl` directly by the runner.
- *Prediction:* Match. SSE is well-trodden; implementation is thin.
- *Falsification:* Any divergence → investigate buffering in
  FastAPI/uvicorn or in the SSE client; document the failure mode.
- *Timeline:* A few hours. Phase 3.

**H9: Two parallel runs against the same source repo (one via fan-out,
one ad-hoc) don't corrupt each other's state.**

- *Experiment:* Start a fan-out audit on `./my-repo`. While it's
  running, in another terminal, start an `aitelier lint ./my-repo`
  against the same directory. Both should complete without interfering.
- *Prediction:* No interference; both runs operate on independent
  tmpdir copies.
- *Falsification:* Collision (file lock, corrupted output) → bug in
  workspace handling or need explicit lock semantics.
- *Timeline:* An hour. Phase 1.

### Reliability hypotheses

**H7: LiteLLM's `num_retries=3` correctly handles the transient failures
we actually see.**

- *Experiment:* Run the system in real use for two weeks. Log every
  call that errors. Classify: transient (resolved on retry), persistent
  (auth, bad request), ambiguous.
- *Prediction:* >80% transient and resolved. <10% persistent. Rest
  ambiguous and needs investigation.
- *Falsification:* Transient retries fail more often than expected, or
  persistent errors misclassified as retryable → override
  `retry_policy` per error class.
- *Timeline:* Two weeks of passive observation. Continuous.

**H8: Closed-laptop tolerance via remote sandbox actually works in
practice.**

- *Experiment:* Start a 30-minute audit task in a remote sandbox via
  aitelier. Close the laptop. Reopen 10 minutes later. Reconnect to
  the session and verify the agent kept working and we can pick up the
  event stream.
- *Prediction:* Works. Agent runs server-side; laptop is just a client.
- *Falsification:* Reconnection fails or agent stopped mid-execution →
  durability story broken; reconsider sandbox provider or streaming
  protocol.
- *Timeline:* An hour, requires Phase 4 done.

### Design assumption checks

**H10: The result-dict shape composes cleanly across both SDKs and
across LLM/agent kinds without fields-that-shouldn't-be-there or
fields-that-are-missing.**

- *Experiment:* Implement Phase 1 and Phase 3. Exercise both SDKs with
  both LLM and agent calls. Notice every conditional based on which
  fields are present.
- *Prediction:* Works, with one or two awkward spots: `cost_usd`
  optional for LLM calls without billing data; `session_id` agent-only.
- *Falsification:* Conditional-on-shape code becomes pervasive in SDK
  consumers → discriminated union (Python: tagged TypedDicts; TS:
  discriminated union types) or typed class hierarchy.
- *Timeline:* Build Phases 1-3, then evaluate.

**H11: ralphx's loop machinery composes cleanly with aitelier's
per-iteration call.**

- *Experiment:* Migrate ralphx from direct Claude Code spawning to
  aitelier SDK invocation for one real workspace. Run a real loop and
  verify: convergence detection works, hints inject correctly, budget
  enforcement triggers, resumability functions, costs aggregate.
- *Prediction:* All ralphx semantics work because none depend on how
  the agent is invoked, only on the result.
- *Falsification:* Any ralphx feature breaks (especially resumability
  or budget tracking) → coupling missed; boundary needs adjustment.
- *Timeline:* A day or two. Phase 5.

**H12: Schema-driven type generation keeps both SDKs in sync without
drift.**

- *Experiment:* Over the first month, change the schema three times.
  Verify both SDKs regenerate cleanly and contract tests pass. Track
  any drift discovered later.
- *Prediction:* Generation works smoothly; contract tests catch
  behavioral drift; manual review catches field-naming drift.
- *Falsification:* Generators produce inconsistent output between
  languages → better generators or hand-curated types from a single
  source.
- *Timeline:* First month. Continuous.

### Cost and workflow hypotheses

**H13: Daily AI costs for personal use stay under $10/day with this
stack.**

- *Experiment:* Use the system as primary AI tooling for a full week.
  Watch Langfuse cost dashboards. Compute average daily cost.
- *Prediction:* $3-7/day for normal interactive use. Higher on heavy
  fan-out days. Spikes to $20+ on heavy implement-loop days.
- *Falsification:* Costs routinely above $20/day → examine which
  tasks/providers responsible; adjust defaults.
- *Timeline:* One week of real use.

**H14: Fan-out is genuinely useful in practice, not just in theory.**

- *Experiment:* For one month, default to fan-out for all new task
  types. After a month, count how often fan-out actually influenced
  the decision (preferred one provider's output) vs how often you used
  the first/default result anyway.
- *Prediction:* Fan-out useful in calibration phase (first 2-3 runs of
  any new task type) and rarely useful afterward. Steady-state should
  be single-provider with occasional re-calibration.
- *Falsification:* Fan-out routinely changes which output you act on
  even for established task types → keep fan-out as default. Never
  useful past calibration → change default to single-provider with
  explicit `--fanout` opt-in.
- *Timeline:* One month. Continuous.

**H15: The four task types (audit, research, lint, implement) plus
summarize cover 80% of actual AI work.**

- *Experiment:* For a month, every AI-assisted task: ask "is this
  audit, research, lint, implement, summarize, or something else?"
  Track the "something else" cases.
- *Prediction:* 70-80% fit cleanly. Common new categories: "explain"
  (closer to summarize but for code), "design" (architectural sketches),
  "review" (assess proposed changes).
- *Falsification:* "Something else" is the majority → task taxonomy is
  wrong; rethink based on actual usage patterns.
- *Timeline:* One month. Continuous.

### Open questions (not yet hypotheses)

Real design questions where I don't yet have a clear hypothesis but
need empirical answers. Watch for them during use; collect evidence.

- **Q1:** What's the right `max_concurrent` for fan-out, and does it
  vary by task type or provider mix? Need data before defaulting.
- **Q2:** When ralphx's loop fails an iteration, should aitelier's
  retry kick in, or should ralphx handle the retry? Defining the
  responsibility split needs real failure scenarios.
- **Q3:** Does Langfuse's free tier handle the volume of traces, or
  do we need self-hosted from the start? Depends on H13.
- **Q4:** Should remote-sandbox provisioning be a separate tool or
  part of aitelier? Depends on how often you actually invoke it.
- **Q5:** Is per-task `preferred_providers` better as static config
  or as a function evaluated at runtime (cost-aware, latency-aware)?
  Need experience with the static version first.
- **Q6:** Does the Python SDK earn its keep, or do you mostly import
  core directly anyway? Real usage will tell.

### When and how to run these

These aren't unit tests. They're investigations to run when their
phase comes up:

- **Pre-Phase 1:** H1, H2, H3 — provider behavior, before building
  anything. These determine whether the project is worthwhile.
- **Phase 1-2:** H4, H5, H9, H10 — quantitative checks that the
  system behaves as expected.
- **Phase 3-4:** H6, H12 — system integration checks.
- **Phase 5:** H8, H11 — durability and ralphx integration checks.
- **Continuous:** H7, H13, H14, H15 — long-running observations.

If a week passes after a hypothesis's timeline and the experiment
hasn't been run, either run it or remove the hypothesis. Don't let
unfinished experiments become decoration.

## Tradeoffs and rationale

**Two SDKs over a single language.** ralphx is TypeScript and earns the
TS SDK; personal scripts are Python and benefit from the Python SDK.
Symmetry helps future-you maintain a single mental model across both.
Cost: two implementations, kept in sync via shared schemas and contract
tests.

**Schema-driven type generation.** Both SDKs generate types from JSON
Schema rather than hand-writing. Removes a class of drift bugs. Mandatory
discipline for personal use where no external pressure catches divergence.

**Versioning even for personal use.** Version numbers anchor "what was
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

**LiteLLM SDK mode over proxy mode.** Single-developer use doesn't need
centralized spend management. Decision revisitable when adding more
projects.

**Langfuse over JSONL + jq.** Trades zero-infrastructure for richer
visibility. Self-hosted Docker Compose is one command; the UI pays back
within a week.

**Sandbox Agent over AgentAPI.** Better architecture (typed events vs.
TUI scraping), better remote-sandbox story, official commitment from
Rivet. Cost: youngest of the options, no Python SDK. Mitigation: build it.

**No abstract `Provider` class in core.** Two thin functions returning
dicts is sufficient for the current call sites. The HTTP boundary is
where dispatch happens; no need for an in-process abstraction.

**ralphx invokes aitelier, not the reverse.** Right layering: ralphx is
control-flow over a primitive; aitelier provides the primitive. Each
tool stays focused on its actual job.

**Workspace = tmpdir copy by default, in-place when caller asks.**
ralphx loops need workspace persistence across iterations; one-shot
aitelier callers benefit from isolation. `workspace_mode` field handles
both cleanly.

**Run directory format = filesystem layout, not database.** Human
browseable, git-friendly, no schema migration story. Will outlive any
specific tool; trivial to ingest into a database later if needed.

## Risks and open questions

**Sandbox Agent schema drift.** Project at v0.4.x, monthly releases.
Mitigation: pin versions, review changelog, keep dict shape thick enough
that Sandbox Agent specifics don't leak into application code.

**`--max-turns 0` flags in `agent-auth`.** Unverified against installed
CLI versions. Mitigation: verify interactively before relying. Override
via TOML if rejected.

**Max plan rate limits in fan-out.** Unknown safe parallel ceiling.
Mitigation: see H4.

**Closed laptop on local agent runs.** No fix; tasks die. Mitigation:
short tasks local, long tasks remote. See H8.

**Two SDKs drift.** Real risk for personal projects without external
pressure. Mitigation: schema-driven types, contract test corpus,
"never add a feature to one SDK without the other" discipline. See H12.

**HTTP service auth.** Currently none; localhost-bound. Mitigation:
accept the risk for personal use; add token check before any non-local
exposure.

**Composition (research → implement).** Plain async function composition
assumed sufficient. May need explicit pipeline support later.

**Sandbox Agent's Python client is on us to build.** Time cost: ~3-4
days for v0.1, ~1-2 weeks for parity with TS SDK. Mitigation: ship v0.1,
contribute upstream, defer parity.

## Validation criteria

The system is working when:

1. `aitelier audit ./my-repo` produces useful output in <30 seconds for
   small repos.
2. `aitelier audit ./my-repo --fanout` produces side-by-side outputs
   from 2+ providers in <2 minutes.
3. The HTTP service handles the same task spec as the CLI and produces
   identical run records.
4. Both SDKs (Python and TS) successfully invoke `execute` against the
   service and return typed results.
5. ralphx loops successfully use the TypeScript SDK to invoke agents,
   replacing direct Claude Code spawning, with no behavioral
   regression.
6. Re-running the same task produces traceable, comparable results in
   Langfuse, regardless of whether invoked via CLI, SDK, or ralphx.
7. A failed provider (network error, rate limit) doesn't fail the
   whole fan-out; other providers succeed; failure captured.
8. Contract tests pass for both SDKs against the same test corpus.
9. Adding a new task type ("research") requires editing one file
   (`core/tasks/research.py`) and changes nothing else.
10. Bumping the version with `scripts/release.sh` updates core, both
    SDKs, and the changelog atomically.
11. Hypotheses H1-H6 and H9-H11 have been run with results recorded in
    `docs/hypotheses.md`. (H7, H8, H12, H13, H14, H15 are continuous
    and not gated on Phase 5 completion.)

When these all hold, Phase 5 is done and we're at v0.2.0.

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

**Building a `Provider` Protocol with formal types in core.** Rejected.
Dicts sufficient inside the service; SDKs handle types at the boundary.

**Using Vercel AI SDK + ACP in TypeScript core.** Considered for the TS
side. Rejected for core because LiteLLM in Python is the preferred LLM
abstraction. Vercel AI SDK might still appear inside the TS SDK for
specific patterns (e.g., tool-use), but is not the architectural
foundation.

**Using AgentAPI instead of Sandbox Agent.** AgentAPI older, more stable,
broader agent coverage, but uses TUI scraping. Sandbox Agent chosen for
proper event model and remote-sandbox-first design.

**One SDK in TypeScript, none in Python.** Considered. Rejected because
personal Python scripting is a real use case and going through ad-hoc
httpx is annoyingly inconsistent with how other code calls the service.

**LangGraph as the agent framework from day 1.** Rejected. Heavy
abstractions for control flow we don't yet have.

**Postgres or ClickHouse for event storage.** Rejected. JSONL files in
the run directory plus Langfuse covers visibility. Database when queries
across runs become a real workflow.

**A web dashboard.** Rejected for now. Langfuse covers 80% of what one
would do.

**aitelier wraps ralphx (instead of ralphx invokes aitelier).** Rejected
in favor of the inverse layering. ralphx is control flow over a
primitive; aitelier provides the primitive. Cleaner boundary, less
coupling.

**`atelier` without the AI prefix.** Considered. Rejected because the
pun is descriptive and personal-toolkit memorability matters more than
timelessness.

**Submodules for the Sandbox Agent fork.** Rejected. Fork lives in
separate repo; depended on via pip from upstream when their Python SDK
ships.

**Monorepo with Nx or Turborepo.** Considered. Rejected as overkill for
a personal project. Plain pnpm + uv coexisting is simpler.
