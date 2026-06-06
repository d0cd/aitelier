# aitelier

Personal AI runtime — inference primitives, agent delegation, and observability for personal projects.

## API surface (three primitives + traces)

- **`complete()`** — structured chat completion (messages, system prompt, temperature, response format incl. JSON schema)
- **`embed()`** — batch embeddings via LiteLLM proxy (default: nomic-embed-text, 768-dim)
- **`runAgent()`** — delegate to external agent (Claude Code, Codex) with passthrough of: system prompt, MCP servers, tool allowlist, response format, max turns
- **`recentTraces()`** — query SQLite trace store by tag, status, time range

Legacy fan-out task system (`execute`, `fanout`) still works on top of these.

## Project structure

- `core/src/aitelier/` — Python core: providers, runner, server, config, traces, errors
- `core/tasks/` — named task definitions (audit, research, lint, implement, summarize)
- `schemas/v1/` — JSON Schema source of truth for wire format
- `sdks/python/` — Python SDK (`aitelier_client`)
- `sdks/typescript/` — TypeScript SDK (`aitelier`)
- `tests/contract/` — shared contract test corpus
- `docker/` — docker-compose for LiteLLM proxy
- `scripts/` — start.sh, stop.sh, release.sh, generate-types.sh
- `runs/` — gitignored run output + traces.db

## Infrastructure

Dockerized LiteLLM proxy + host-installed aitelier + host-installed Sandbox Agent
binary (Rivet's coding-agent runtime). Credentials are extracted from Claude Code
and Codex CLI credential files (`~/.claude/.credentials.json`,
`~/.codex/auth.json`) — no manual API keys needed.

```bash
claude login              # once — OAuth login
make install              # one-time setup
make start                # credentials + LiteLLM proxy + Sandbox Agent + aitelier service
make stop                 # stops everything
make test                 # runs all tests (unit + smoke)
```

- **LiteLLM proxy** on `localhost:4000` — model routing, caching, cost tracking
- **Sandbox Agent** on `localhost:2468` (or dynamic) — runs coding agents in isolation;
  `scripts/start.sh` installs the Rust binary on first run

Available models (via LiteLLM): `local`, `claude-sonnet`, `claude-haiku`, `nomic-embed-text`
Available agent backends (via Sandbox Agent): `claude-code`, `codex`, `opencode`, `cursor`, `amp`, `pi`

## Configuration

Single file: `aitelier.toml` (repo-local) or `~/.config/aitelier/config.toml` (global).
Env vars override: `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `SANDBOX_AGENT_BASE_URL`.

## HTTP endpoints

```
POST /v1/complete             — chat completion
POST /v1/complete/stream      — chat completion via SSE (complete.delta / complete.done / complete.error)
POST /v1/embed                — batch embeddings
POST /v1/agent                — run agent with MCP/tool options
POST /v1/agent/stream         — run agent via SSE (agent.delta / tool_call / tool_result / done / error)
POST /v1/agent/preview        — dry-run resolve MCP servers + allowlist (catch typos before a real run)
GET  /v1/traces               — query trace store
GET  /v1/traces/{id}          — get single trace
POST /v1/execute              — run a named task
POST /v1/execute/stream       — run task with SSE streaming
POST /v1/fanout               — fan-out across providers
GET  /v1/runs/{id}            — get run record
GET  /v1/runs/active          — list in-flight run_ids in this process
POST /v1/runs/{id}/cancel     — cancel an in-flight run
GET  /v1/health               — cheap liveness probe (status, version, known_limitations)
GET  /v1/discovery            — capability + endpoint inventory (live dependency probes)
GET  /v1/schemas/{name}       — fetch a JSON Schema by name (task, result, events)
```

All requests accept `X-Correlation-Id` (generated if absent), echoed in
response header + body field + SSE events + trace metadata.

## Error types

Errors are classified via `core/src/aitelier/errors.py`:

| error_type | Triggers |
|---|---|
| `ProviderUnavailable` | ConnectError, ConnectionError, OSError |
| `Timeout` | TimeoutException, TimeoutError, agent timeout |
| `SchemaViolation` | JSONDecodeError, ValidationError |
| `RateLimited` | HTTP 429 |
| `AuthError` | HTTP 401/403 |
| `ProviderError` | Other HTTP errors |
| `NonZeroExit` | Agent CLI exited with error |
| `Cancelled` | Run cancelled via `/v1/runs/{id}/cancel` (or asyncio.CancelledError) |

## Key decisions

- Python core, TypeScript + Python SDKs
- LiteLLM proxy for LLM calls (not library mode)
- Agent delegation via **Rivet's Sandbox Agent** (speaks ACP); all coding agents run isolated.
  No direct subprocess invocation of `claude` / `codex` from aitelier anymore.
- Schema-driven type generation — `schemas/v1/*.schema.json` is the source of truth
- SQLite trace store in runs/ dir (queryable by trace_tag, status, time; purged after 30 days)
- Dicts in core, typed models in SDKs
- Agent `cost_usd` is always `null` by design: agent LLM calls happen inside
  the Sandbox Agent process and go directly to Anthropic/OpenAI, bypassing
  LiteLLM. Token usage *is* captured when the backend surfaces it. See
  `docs/INTEGRATION.md` → "Cost tracking" for workarounds.

## Conventions

- Task definitions in `core/tasks/` — one file per task, discovered by name
- Generated types in `_generated/` dirs — never hand-edit
- Run directories: `runs/{ISO-timestamp}_{task}/`
- API versioning: `/v1/` prefix
- Lockstep versioning across all packages
- Kind values: `complete`, `embed`, `agent` (legacy `llm` mapped to `complete`)

## Build & run

```bash
make install              # install all deps (Python + TypeScript)
make test                 # run all tests (Python unit + smoke, TypeScript type check)
make test-py              # Python tests only
make test-ts              # TypeScript type check only
make lint                 # ruff + tsc
make start                # start infra + service
make stop                 # stop everything
make clean                # remove venv, build artifacts
./scripts/release.sh X.Y.Z  # lockstep version bump
```

## Consumer contract (deepread)

deepread is the first consumer. It depends on `complete()`, `embed()`, `runAgent()`, and `recentTraces()`. The contract is documented in deepread's repo. Breaking changes to these four primitives require coordinated updates. See `docs/INTEGRATION.md` for the consumer-facing guide.
