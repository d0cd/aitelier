# aitelier — implementation plan

Extracted from [DESIGN.md](./DESIGN.md). Each phase gates on the previous.

## Phase 0: validation — COMPLETE

- [x] Write one real task in `core/tasks/` (audit + 4 others)
- [x] Implement `call_llm` using LiteLLM proxy
- [x] Wire up CLI entry point (`aitelier run <task> [args]`)
- [ ] **Run H1** — fan-out audit across Claude Code, Codex, Sonnet on 3 repos
- [ ] **Run H2** — agent vs LLM for multi-file context
- [ ] **Run H3** — provider differences on text-only tasks

## Phase 1: schemas and core — COMPLETE

- [x] Define `schemas/v1/task.schema.json`
- [x] Define `schemas/v1/result.schema.json`
- [x] Define `schemas/v1/events.schema.json`
- [x] Implement `call_agent` (CLI subprocess with passthrough: system prompt, MCP, tools, response format)
- [x] Implement task runner: workspace prep, dispatch, diffing
- [x] Implement run directory format and `manifest.json`
- [x] Implement `--fanout` with `asyncio.gather` + `Semaphore`
- [x] Implement `complete()` — structured chat completion (deepread contract)
- [x] Implement `embed()` — batch embeddings
- [x] Implement trace store (SQLite) with `recentTraces()` and `purge_traces()`
- [x] Implement typed error classification (`errors.py`)
- [ ] Set up type generation pipeline (`scripts/generate-types.sh`)
- [ ] **Run H4** — rate limits with bounded fan-out
- [ ] **Run H9** — parallel runs don't corrupt state

## Phase 2: HTTP service — COMPLETE

- [x] FastAPI app: `POST /v1/complete`
- [x] `POST /v1/embed`
- [x] `POST /v1/agent`
- [x] `GET /v1/traces` and `GET /v1/traces/{id}`
- [x] `POST /v1/execute`
- [x] `POST /v1/execute/stream` (SSE)
- [x] `POST /v1/fanout`
- [x] `GET /v1/runs/{id}` (with path traversal protection)
- [x] `GET /v1/health` (with known_limitations)
- [x] Startup health check for LiteLLM proxy
- [x] Trace purge on startup (30-day retention)
- [ ] **Run H5** — HTTP overhead measurement

## Phase 3: SDKs — COMPLETE

- [x] Python SDK (`sdks/python/`): Pydantic models, async client, streaming, sync wrapper
- [x] TypeScript SDK (`sdks/typescript/`): TS types, async fetch client, streaming
- [x] Both SDKs expose `complete()`, `embed()`, `runAgent()`, `recentTraces()`
- [ ] Contract test corpus in `tests/contract/` (JSON files exist, not wired to both SDKs)
- [ ] **Run H6** — SSE event ordering
- [ ] **Run H10** — result-dict shape across both SDKs

## Phase 4: Sandbox Agent client — COMPLETE

- [x] Sandbox Agent (rivet-dev/sandbox-agent v0.4.x) installed + supervised by `scripts/start.sh`
- [x] Port resolution: `--sandbox-agent-port` CLI arg → `SANDBOX_AGENT_PORT` env → 2468 with dynamic fallback if taken
- [x] `providers/sandbox_agent.py` — ACP (Agent Client Protocol) client over HTTP/SSE
- [x] `providers/agent.py` reduced to a thin compat shim; direct subprocess paths deleted
- [x] `/v1/discovery` probes the sandbox agent server; reports available agent backends
- [x] All agent calls now isolated by Sandbox Agent (sandboxing, env scoping, MCP wiring delegated)

## Phase 5: ralphx integration

Not started.

## Phase 6: as friction demands

No items scheduled. Each built when it becomes the thing that's annoying.

## Infrastructure — COMPLETE

- [x] Docker compose for LiteLLM proxy
- [x] Credential extraction from CLI logins (Claude Code OAuth, Codex OAuth)
- [x] `scripts/start.sh` — idempotent startup (credentials + infra + service)
- [x] `scripts/stop.sh`
- [x] `Makefile` — install, test, start, stop, lint, clean
- [x] Config system (`aitelier.toml` / env vars)
- [x] Security fixes (temp file cleanup, TOML injection, path traversal)

## Continuous hypotheses

Track throughout all phases:

- **H7** — LiteLLM retry behavior in real use (2 weeks observation)
- **H12** — schema-driven type gen keeps SDKs in sync (first month)
- **H13** — daily AI costs stay under $10/day (one week)
- **H14** — fan-out usefulness in practice (one month)
- **H15** — task taxonomy covers 80% of actual work (one month)
