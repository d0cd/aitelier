# aitelier integration guide

> For projects consuming aitelier as an AI runtime. Drop this into your
> project or reference it from your CLAUDE.md.

## What aitelier is

A local AI runtime at `~/projects/aitelier`. It owns inference, agent
delegation, and observability. Your project owns prompts, tools, domain
logic, and scheduling.

## Setup

aitelier runs as a local HTTP service on `localhost:7777`. Credentials are
extracted from Claude Code and Codex CLI logins — no manual API keys needed.

```bash
cd ~/projects/aitelier
claude login              # once — OAuth login for Anthropic
make install              # one-time dependency setup
make start                # credentials + LiteLLM proxy + Sandbox Agent + aitelier service
make test                 # verify everything works (unit + smoke tests)
```

`make start` installs and supervises three things:
- **LiteLLM proxy** on `localhost:4000` (Docker)
- **Sandbox Agent** (Rivet) on `localhost:2468` — single Rust binary, auto-installed
- **aitelier service** on `localhost:7777`

Sandbox Agent port can be overridden: `./scripts/start.sh --sandbox-agent-port 3000`
or via `SANDBOX_AGENT_PORT` env. If 2468 is taken, a free port is picked dynamically
and exported as `SANDBOX_AGENT_BASE_URL` for the aitelier service to pick up.

### Remote sandbox-agent (closed-laptop tolerance)

Sandbox Agent can run on a remote host instead of locally — useful when you
want long agent runs to survive your laptop going to sleep. Point aitelier at
the remote URL by setting two env vars before `make start`:

```bash
export SANDBOX_AGENT_BASE_URL="https://your-sandbox-host.example.com"
export SANDBOX_TOKEN="<your token>"   # from `niteshift auth` / E2B / Daytona / Modal
make start
```

`scripts/start.sh` detects a non-local `SANDBOX_AGENT_BASE_URL` and skips the
local binary install. Health-checks the remote, then aitelier reads the URL
from its env at boot. Agent runs go remote; LiteLLM + traces stay local.

Tested against the Rust binary running on the same host. Public hosted
backends (E2B, Daytona, Modal, Vercel Sandboxes) require their respective
provisioning steps — see Sandbox Agent's docs for installing on each.

Or use SDK mode (import directly, no HTTP service needed):

```python
from aitelier.providers.llm import complete, embed
from aitelier.providers.agent import call_agent
```

## Three primitives

### 1. `complete` — chat completion

```python
# Via HTTP (from any language)
POST http://localhost:7777/v1/complete
{
  "model": "claude-sonnet",
  "system_prompt": "You are a fact-checker.",
  "messages": [
    {"role": "user", "content": "Is the sky blue?"}
  ],
  "temperature": 0,
  "max_tokens": 1000,
  "response_format": {
    "type": "json_schema",
    "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
    "strict": true
  }
}

# Response
{
  "kind": "complete",
  "provider": "claude-sonnet",
  "status": "ok",
  "content": "{\"answer\": \"yes\"}",
  "parsed": {"answer": "yes"},
  "usage": {"input_tokens": 25, "output_tokens": 10, "total_tokens": 35},
  "finish_reason": "stop",
  "cost_usd": 0.001,
  "trace_id": "2026-05-07T10-00-00_complete",
  ...
}
```

```python
# Via Python SDK
from aitelier_client import Aitelier

async with Aitelier() as ai:
    result = await ai.complete(
        model="claude-sonnet",
        system_prompt="You are a fact-checker.",
        messages=[{"role": "user", "content": "Is the sky blue?"}],
        response_format={"type": "json_schema", "schema": {...}, "strict": True},
    )
    print(result.parsed)  # {"answer": "yes"}
```

```typescript
// Via TypeScript SDK
import { Aitelier } from "aitelier";

const ai = new Aitelier();
const result = await ai.complete({
  model: "claude-sonnet",
  systemPrompt: "You are a fact-checker.",
  messages: [{ role: "user", content: "Is the sky blue?" }],
  responseFormat: { type: "json_schema", schema: {...}, strict: true },
});
console.log(result.parsed); // { answer: "yes" }
```

#### Streaming variant

```python
POST http://localhost:7777/v1/complete/stream
# Same request body as /v1/complete
```

Returns Server-Sent Events. Three event types:

```
event: complete.delta
data: {"content": "Hello", "correlation_id": "..."}

event: complete.delta
data: {"content": " world", "correlation_id": "..."}

event: complete.done
data: {"content": "Hello world", "usage": {...}, "finish_reason": "stop",
       "cost_usd": null, "trace_id": "", "correlation_id": "..."}
```

On failure a single `event: complete.error` is emitted instead of `complete.done`.
No retries — once a token has been delivered, replay is unsafe.

### 2. `embed` — batch embeddings

```python
POST http://localhost:7777/v1/embed
{
  "texts": ["first document", "second document"],
  "model": "nomic-embed-text"     # optional, this is the default
}

# Response
{
  "kind": "embed",
  "status": "ok",
  "embeddings": [[0.1, 0.2, ...], [0.3, 0.4, ...]],
  "dimensions": 768,
  ...
}
```

Default model is `nomic-embed-text` (768 dimensions). If dimensions don't
match what you expect, treat it as an error.

### 3. `runAgent` — agent with MCP tools

This is the key primitive. Aitelier delegates to an external coding agent
(Claude Code or Codex) and passes through your control options.

```python
POST http://localhost:7777/v1/agent
{
  "model": "claude",
  "system_prompt": "You are a curator agent for a reading list app...",
  "initial_message": "Process today's feeds and produce an inbox.",
  "mcp_servers": [
    {
      "name": "myproject",
      "transport": "http",
      "url": "http://localhost:3001/mcp"
    }
  ],
  "tool_allowlist": ["myproject.query_corpus", "myproject.add_item"],
  "response_format": {
    "type": "json_schema",
    "schema": {
      "type": "object",
      "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "summary": {"type": "string"}
      },
      "required": ["items", "summary"]
    }
  },
  "max_turns": 25,
  "timeout": 300,
  "trace_tag": "curator-daily-2026-05-07"
}

# Response
{
  "kind": "agent",
  "provider": "claude",
  "status": "ok",
  "content": "{\"items\": [...], \"summary\": \"...\"}",
  "parsed": {"items": [...], "summary": "..."},
  "finish_reason": "completed",    # or "max_turns", "timeout", "error"
  "tool_calls": [...],             # when available from the agent
  "trace_id": "2026-05-07T10-00-00_agent_run",
  ...
}
```

**What happens under the hood:**
aitelier opens an ACP session against the local Sandbox Agent (rivet-dev/sandbox-agent)
and runs the agent inside it. The flow is `initialize` → `session/new` (with cwd +
MCP servers) → `session/prompt` (blocks until turn completes). Notifications
(`session/update`) stream in over SSE during the prompt and contribute message
chunks + tool calls to the final result. See `core/src/aitelier/providers/sandbox_agent.py`.

### 4. `recentTraces` — query the trace store

Every `complete`, `embed`, and `runAgent` call is recorded in a SQLite
trace store. Query it for debugging:

```python
GET http://localhost:7777/v1/traces?trace_tag=curator-daily-2026-05-07&limit=10

# Response: array of trace records
[{
  "trace_id": "2026-05-07T10-00-00_agent_run",
  "started_at": "2026-05-07T10:00:00Z",
  "ended_at": "2026-05-07T10:02:30Z",
  "model": "claude",
  "kind": "agent",
  "finish_reason": "completed",
  "tool_call_count": 5,
  "total_tokens": 15000,
  "cost_usd": 0.12,
  "system_prompt_hash": "a1b2c3d4e5f6...",
  "trace_tag": "curator-daily-2026-05-07",
  "status": "ok"
}]
```

Filter by: `trace_tag`, `status` ("ok"/"error"), `since` (ISO timestamp), `limit`.

Or from the CLI:

```bash
aitelier traces                                            # latest 20
aitelier traces --tag curator-daily-2026-05-07
aitelier traces --status error --since 2026-05-01T00:00:00Z
aitelier traces 2026-05-07T10-00-00_agent_run              # single trace detail
aitelier traces --json                                     # JSON output for piping
```

## Available models

These are the model names to use (routed by the LiteLLM proxy):

| Name | What | Cost |
|---|---|---|
| `claude-sonnet` | Claude Sonnet 4.6 | Cloud (Anthropic API) |
| `claude-haiku` | Claude Haiku 4.5 | Cloud (Anthropic API) |
| `local` | Qwen3 8B via Ollama | Free (local) |
| `nomic-embed-text` | Nomic embeddings 768d | Free (local) |

For agent calls, use agent names from `GET /v1/discovery → dependencies.sandbox_agent.agents` (currently: `claude`, `codex`, plus whatever other backends sandbox-agent advertises).

### Alias stability

Model names above are **stable aliases**, not versioned model IDs. The
underlying versions are pinned in `docker/litellm/config.yaml`
(e.g. `claude-sonnet` currently → `anthropic/claude-sonnet-4-6`). aitelier
may bump the underlying version under the same alias when there's a clear
upgrade; behavior changes are noted in `PLAN.md`.

If you need to pin a specific version, request that a versioned alias be
added (e.g. `claude-sonnet-4-6` alongside `claude-sonnet`); aitelier can
route both. Consumers that don't pin should expect alias behavior to track
the current best version for the family.

## Available agents

All agents run through **Rivet's Sandbox Agent** (ACP-based). aitelier no longer
spawns `claude` or `codex` as subprocesses directly — Sandbox Agent owns isolation,
env scoping, and event normalization across all backends.

| Name | Notes |
|---|---|
| `claude` | Anthropic Claude Code via ACP |
| `codex` | OpenAI Codex CLI via ACP |
| `opencode` | OpenCode via ACP |
| `cursor` | Cursor's agent via ACP |
| `amp` | Amp via ACP |
| `pi` | Pi via ACP |

Agent names are exactly what Sandbox Agent advertises — call `GET /v1/discovery`
to see the live list (`dependencies.sandbox_agent.agents`). Don't assume a
name; query it.

## Security model

Two modes:

**Localhost-trust (default).** aitelier binds to `127.0.0.1`, no auth on any
endpoint. Anyone who can reach the port can call any endpoint. Fine for a
laptop-only personal runtime; do not bind to a public interface in this mode.

**Hosted mode** — set `service.api_key` in `aitelier.toml` *or*
`AITELIER_API_KEY` env. Every `/v1/*` endpoint (except `/v1/health`, kept
public for liveness probes) requires `Authorization: Bearer <api_key>`.
SDKs accept `api_key` / `apiKey` in the constructor and send the header
automatically:

```python
async with Aitelier(base_url="https://aitelier.your-host.example.com",
                    api_key="<your-key>") as ai: ...
```

```typescript
const ai = new Aitelier({ baseUrl: "https://aitelier.your-host.example.com",
                          apiKey: "<your-key>" });
```

Always combine hosted mode with TLS termination (e.g., Caddy/Traefik in
front of the container) — Bearer over plain HTTP is unsafe.

Agents themselves run isolated by Sandbox Agent: file system scope, env
scoping, no aitelier-side secrets leak in.

### Container / Dockerfile

`docker/Dockerfile` builds a minimal image running just the FastAPI
service. Point it at remote LiteLLM and Sandbox Agent via env:

```bash
docker build -f docker/Dockerfile -t aitelier:latest .
docker run -p 7777:7777 \
    -e AITELIER_HOST=0.0.0.0 \
    -e AITELIER_API_KEY=<your-key> \
    -e LITELLM_BASE_URL=http://litellm:4000 \
    -e SANDBOX_AGENT_BASE_URL=http://sandbox-agent:2468 \
    -v aitelier-runs:/app/runs \
    aitelier:latest
```

## Error handling

Aitelier returns errors in the result dict, not as HTTP errors:

```json
{
  "status": "error",
  "error_type": "ProviderUnavailable",
  "error_msg": "Connection refused",
  "finish_reason": "error"
}
```

| error_type | Meaning |
|---|---|
| `ProviderUnavailable` | Can't reach LiteLLM proxy or agent |
| `Timeout` | Call exceeded timeout |
| `RateLimited` | HTTP 429 from provider |
| `AuthError` | HTTP 401/403 from provider |
| `ProviderError` | Other HTTP errors from provider |
| `SchemaViolation` | Structured output didn't parse |
| `NonZeroExit` | Agent CLI exited with error |
| `Cancelled` | Run was cancelled via `POST /v1/runs/{id}/cancel` |

Your project should handle these by checking `result.status == "error"`.

## Cost tracking

| Primitive | `cost_usd` | Why |
|---|---|---|
| `complete` | ✅ tracked | Routed through the LiteLLM proxy, which logs cost per call |
| `embed` | ✅ tracked | Same |
| `runAgent` / `runAgentStream` | ❌ `null` | Agent LLM calls happen *inside* the Sandbox Agent process, going directly to Anthropic/OpenAI — they bypass LiteLLM, so aitelier doesn't see them |

Tokens (`usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens`)
are captured on agent results **if** the backend surfaces them in the
ACP `session/prompt` response. claude-code typically does; coverage of
other backends varies.

**Workarounds for per-run agent cost (none ideal yet):**

1. **Route agent calls through LiteLLM.** Set `ANTHROPIC_BASE_URL` /
   `OPENAI_BASE_URL` on the sandbox-agent process so its child CLIs send
   to LiteLLM. Captures aggregate cost; per-run attribution would need
   sandbox-agent to inject a `Aitelier-Run-Id` header per session,
   which it doesn't expose today (upstream feature ask).
2. **Estimate from tokens.** Multiply `usage` by your own price table.
   Drifts as prices change; doesn't account for caching discounts.

If you need per-run agent cost for budget alerting, file the upstream
ask with [rivet-dev/sandbox-agent](https://github.com/rivet-dev/sandbox-agent)
for per-session env injection. Until then, treat `cost_usd: null` on
agent results as expected, not a bug.

## Configuration

Aitelier reads config from (in order):
1. `aitelier.toml` in the current directory
2. `~/.config/aitelier/config.toml`
3. Environment variables: `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `SANDBOX_AGENT_BASE_URL`, `SANDBOX_TOKEN`
4. Defaults (`localhost:4000`, `sk-litellm-local`, `localhost:2468`)

## SDK reference — new primitives

The four "deepread contract" primitives (`complete`, `embed`, `runAgent`,
`recentTraces`) are unchanged. The SDKs additionally expose:

### `completeStream` — streaming chat completion

```python
async with Aitelier() as ai:
    async for ev in ai.complete_stream(model="claude-sonnet",
                                       messages=[{"role": "user", "content": "tell me a story"}]):
        if ev["type"] == "complete.delta":
            print(ev["data"]["content"], end="", flush=True)
        elif ev["type"] == "complete.done":
            print()  # final aggregated result with usage, finish_reason
```

```typescript
const ai = new Aitelier();
for await (const ev of ai.completeStream({ model: "claude-sonnet", messages: [...] })) {
  if (ev.type === "complete.delta") process.stdout.write(ev.data.content as string);
}
```

### `cancelRun` / `listActiveRuns`

```python
runs = await ai.list_active_runs()   # ActiveRuns(active=[run_id, ...])
await ai.cancel_run("2026-05-12T..._agent_run")   # CancelAck(run_id=..., cancelled=True)
```

```typescript
const { active } = await ai.listActiveRuns();
await ai.cancelRun("2026-05-12T..._agent_run");   // { runId, cancelled: true }
```

### `runAgentStream` — streaming agent run

For chat-style UIs where the user wants to watch the agent's reasoning + tool
calls unfold instead of staring at a spinner:

```python
async for ev in ai.run_agent_stream(model="claude",
                                     initial_message="Process today's feeds.",
                                     mcp_servers=[...], tool_allowlist=[...]):
    if ev["type"] == "agent.delta":
        print(ev["data"]["content"], end="", flush=True)
    elif ev["type"] == "agent.tool_call":
        print(f"\n[tool] {ev['data']['server']}.{ev['data']['tool']}({ev['data']['input']})")
    elif ev["type"] == "agent.done":
        print()  # final Result is in ev["data"]
```

```typescript
for await (const ev of ai.runAgentStream({ model: "claude", initialMessage: "...", mcpServers: [...] })) {
  if (ev.type === "agent.delta") process.stdout.write(ev.data.content as string);
  // ev.type also covers agent.tool_call / agent.tool_result / agent.done / agent.error
}
```

### `agentPreview` — dry-run tool resolution

```python
preview = await ai.agent_preview(
    mcp_servers=[{"name": "deepread", "transport": "http", "url": "http://localhost:3001/mcp"}],
    tool_allowlist=["deepread.query_corpus", "deepread.fact_check"],
)
# preview["allowlist_misses"]  → names in the allowlist that match no tool (likely typos)
# preview["unused_tools"]      → tools available but excluded by the allowlist
# preview["servers"]           → per-server reachability + the tools it advertises
```

```typescript
const preview = await ai.agentPreview({
  mcpServers: [{ name: "deepread", transport: "http", url: "http://localhost:3001/mcp" }],
  toolAllowlist: ["deepread.query_corpus", "deepread.fact_check"],
});
```

Use this when iterating on agent setups — it queries each HTTP MCP server's
`tools/list` and shows which allowlist entries actually match. stdio MCP
servers are marked `previewable: false` (can't query without spawning).

### `discovery` / `getSchema`

```python
d = await ai.discovery()
# d.dependencies.litellm.reachable, d.capabilities["complete"].available, d.endpoints, ...
task_schema = await ai.get_schema("task")
```

```typescript
const d = await ai.discovery();
// d.dependencies.litellm.reachable, d.capabilities.complete.available, d.endpoints, ...
```

### Correlation IDs (per call or default)

```python
# Default for all calls on this client
async with Aitelier(default_correlation_id="job-123") as ai:
    await ai.complete(model="claude-sonnet", messages=[...])

# Override per call
await ai.complete(model="claude-sonnet", messages=[...], correlation_id="req-abc")
```

```typescript
const ai = new Aitelier({ defaultCorrelationId: "job-123" });
await ai.complete({ model: "claude-sonnet", messages: [...] }, { correlationId: "req-abc" });
```

## Correlation IDs

Every request carries an `X-Correlation-Id` header. Supply your own ID to
tie your application logs to aitelier's traces, or let aitelier generate one:

```
POST /v1/complete
X-Correlation-Id: req-abc-123       # optional; generated as UUID if absent
```

The same value is:
- echoed back in the response `X-Correlation-Id` header,
- included in the response JSON as `correlation_id`,
- included in every SSE event payload (for streaming endpoints),
- persisted in `traces.metadata.correlation_id` for runner-routed calls
  (`/v1/agent`, `/v1/execute`, `/v1/fanout`).

## Cancellation

Long-running runs (agent, execute, fanout) can be cancelled by `run_id`:

```
GET  /v1/runs/active            # → {"active": ["2026-05-11T...-agent_run", ...]}
POST /v1/runs/{run_id}/cancel   # → {"run_id": "...", "cancelled": true}
```

The owning request returns a result with `status: "error"`,
`error_type: "Cancelled"`, `finish_reason: "cancelled"`. For streaming
`/v1/execute/stream`, a terminal `event: run.cancelled` is emitted instead
of `run.completed`. Cancelling a run that has already finished returns 404.

Note: the active-run registry is per-process; if you scale aitelier
horizontally, this list reflects only the contacted instance.

## Wire format

Snake_case on the wire (HTTP JSON). The TypeScript SDK converts to camelCase.
The Python SDK uses snake_case natively.

Schemas at: `~/projects/aitelier/schemas/v1/*.schema.json`

## What aitelier does NOT do

- **No prompts or domain logic** — your project owns those
- **No scheduling** — your project runs cron/timers and calls aitelier
- **No tool execution** — your project hosts MCP servers; aitelier brokers
- **No memory** — your project decides what to persist
- **No delivery** — your project handles email, messaging, etc.
