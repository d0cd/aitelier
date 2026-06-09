# aitelier — integration guide

aitelier exposes an **OpenAI-compatible HTTP service** for inference, plus an
**aitelier-native control plane** for durable run state, traces, schedules, and
async agent runs.

If you've used OpenAI's API before, the inference surface is exactly what you
expect. The control plane is what makes aitelier interesting: durable Postgres-
backed runs, append-only event timelines, schedules, webhook delivery,
correlation-IDed traces.

## What aitelier is

A personal AI runtime that puts:

- Any LLM behind LiteLLM (Anthropic, OpenAI, Ollama, …) and
- Any coding agent behind Rivet's Sandbox Agent (Claude Code, Codex, OpenCode, …)

…behind a single OpenAI-shaped HTTP API. Inference calls look like
`chat.completions.create(...)`. The `model` field decides whether you hit an
LLM or an agent.

Aitelier-native concerns — durable run state, correlation tracking,
schedules, webhooks, cancellation — live alongside but separate, as
control-plane endpoints.

## Setup

```bash
git clone https://github.com/<you>/aitelier
cd aitelier
make install
make start          # Postgres + LiteLLM + Sandbox Agent + aitelier service
```

The service binds to `127.0.0.1:7777` by default. The SDKs auto-discover this
via `~/.config/aitelier/config.toml`'s `[service]` block.

## Quickstart

### Python

```python
from aitelier_client import Aitelier

ait = Aitelier(api_key="optional-bearer-key")
openai = ait.openai()    # pre-configured AsyncOpenAI client

# LLM call
resp = await openai.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Summarize today's news."}],
)
print(resp.choices[0].message.content)

# Agent call — model prefix flips the routing
resp = await openai.chat.completions.create(
    model="agent:claude/claude-sonnet-4-5",
    messages=[{"role": "user", "content": "Audit this repo for security issues."}],
    extra_body={
        "aitelier": {
            "workspace": "/path/to/repo",
            "tool_allowlist": ["github.search", "shell.run"],
            "max_turns": 25,
        },
    },
)

# Control plane
runs = await ait.list_runs(trace_tag="security-audit", limit=10)
```

### TypeScript

```typescript
import { Aitelier } from "aitelier";

const ait = new Aitelier({ apiKey: "optional-bearer-key" });
const openai = await ait.openai();

const resp = await openai.chat.completions.create({
  model: "agent:claude",
  messages: [{ role: "user", content: "Audit this repo." }],
  // Aitelier's TypeScript types don't extend OpenAI's, so cast extra_body.
  extra_body: { aitelier: { workspace: "/path/to/repo" } },
} as any);

const runs = await ait.listRuns({ traceTag: "security-audit", limit: 10 });
// → control-plane responses are camelCase (runId, startedAt, …) even
//   though the wire is snake_case; the TS SDK normalizes at the
//   boundary. Inference responses from `ait.openai()` keep OpenAI's
//   snake_case (finish_reason, prompt_tokens, …) to match the broader
//   ecosystem.
```

## The inference surface

Three OpenAI-shape endpoints. Use them directly with the OpenAI SDK, curl,
or any tool that knows the protocol.

### `POST /v1/chat/completions`

Standard OpenAI chat-completions request. Set `stream: true` for SSE.

Routing decided by `model`:

| `model` value | Path | Notes |
|---|---|---|
| `claude-sonnet-4-6`, `nomic-embed-text`, `local`, … | LiteLLM (alias) | Curated short names from `docker/litellm/config.yaml` |
| `anthropic/*`, `openai/*`, `ollama/*` | LiteLLM (wildcard) | Pass-through; LiteLLM resolves the provider |
| `agent:<backend>` | Sandbox Agent | Backend's default inner LLM |
| `agent:<backend>/<inner-llm>` | Sandbox Agent | Explicit inner LLM (e.g. `agent:claude/claude-opus-4-7`) |

### `POST /v1/embeddings`

OpenAI-shape batch embeddings via LiteLLM. Default model:
`nomic-embed-text` (768-dim).

**Note for TypeScript consumers**: OpenAI's JS SDK v6 defaults
`encoding_format: "base64"` for embeddings. A 768-float vector arrives as
a packed-byte string unless you pass `encoding_format: "float"`
explicitly. Not aitelier-specific, but worth knowing — silent
dimension-mismatches are no fun:

```typescript
const emb = await openai.embeddings.create({
  model: "nomic-embed-text",
  input: ["hello world"],
  encoding_format: "float",  // ← explicit; JS SDK defaults to "base64"
});
```

The Python SDK defaults to `"float"`, so no action needed there.

### `GET /v1/models`

OpenAI-shape model list. Two flavors of entry:

- **LLM**: standard OpenAI shape — `{id, object: "model", owned_by, response_format}`. `response_format` lists the `json_object` / `json_schema` modes the provider supports. Anthropic / `claude-*` is `[]` because LiteLLM's adapter rejects OpenAI-shape `json_schema`; `local` / `ollama/*` is both (aitelier bypasses LiteLLM and maps to Ollama's native `format` field); OpenAI / `gpt-*` is both.
- **Agent**: `{id: "agent:<backend>", aitelier_agent: true, aitelier_inner_llms: [...], aitelier_capabilities: {...}}` — lists the LLM IDs you can pair after `/` for `agent:<backend>/<inner-llm>` routing, plus the Sandbox Agent capability flags.

Every entry also carries an `aitelier_request_caps` block declaring which
OpenAI request fields the route honors:

```json
{
  "id": "agent:claude",
  "aitelier_request_caps": {
    "tools":           false,
    "tool_choice":     false,
    "n_gt_1":          false,
    "top_p":           false,
    "streaming":       true,
    "response_format": ["json_schema"]
  }
}
```

Use it to pre-strip request fields in a model picker / proxy rather than
hard-coding aitelier-specific quirks. The agent path enforces the same
rules as 400 responses; consulting `aitelier_request_caps` lets the
consumer avoid the round-trip:

```python
caps = catalog[model].get("aitelier_request_caps", {})
for field in ("tools", "tool_choice"):
    if not caps.get(field, True):
        request.pop(field, None)
```

Consumers can validate `agent:<backend>/<inner-llm>` strings upfront
rather than after a failed run.

### Reasoning models on `local` / `ollama/*`

`local` and `ollama/*` bypass LiteLLM entirely — aitelier calls Ollama's
`/api/chat` directly and translates [Ollama's response shape](https://github.com/ollama/ollama/blob/main/docs/api.md)
to OpenAI's:

| Ollama field | OpenAI ChatCompletion field |
|---|---|
| `message.content` | `choices[0].message.content` |
| `message.thinking` | `choices[0].message.reasoning_content` |
| `message.tool_calls` | `choices[0].message.tool_calls` |
| `done_reason: "length"` | `choices[0].finish_reason: "length"` |
| `done_reason: "stop"` | `choices[0].finish_reason: "stop"` |
| `prompt_eval_count` | `usage.prompt_tokens` |
| `eval_count` | `usage.completion_tokens` |

For Ollama reasoning models (qwen3, deepseek-r1, …), thinking is **off
by default** — set OpenAI's `reasoning_effort` to enable it:

```ts
await openai.chat.completions.create({
  model: "local",
  messages: [...],
  reasoning_effort: "medium",   // ← enables Ollama `think: true`
});
```

Forcing `think` on unconditionally caused a class of bug where qwen3
under a tight `max_tokens` budget consumed the entire budget on hidden
thoughts and returned `content=""`. With the gate, default calls produce
content; reasoning is opt-in via the standard OpenAI signal.

When thinking is enabled:

- **Thinking surfaces as `reasoning_content`**. Even when a tight
  `max_tokens` budget gets fully consumed by reasoning and `content` is
  empty, you'll see what the model produced in
  `choices[0].message.reasoning_content`. `finish_reason: "length"`
  signals the budget was hit.

- **`aitelier_exit: "empty"`** fires only when *neither* content *nor*
  reasoning_content *nor* tool_calls landed despite `completion_tokens
  > 0` — a true degenerate case rather than the normal reasoning-budget
  exhaustion.

Two consumer-side mitigations when you want visible content with
reasoning enabled:

1. **Use `max_completion_tokens`** (OpenAI's reasoning-model field) when
   you want visible output regardless of reasoning size. Aitelier maps
   it to Ollama's `num_predict`.

2. **Set `max_tokens` generously** (e.g. 500+ for short responses).
   Below ~100, reasoning models often won't reach visible output.

**Structured outputs work natively** on `local` / `ollama/*`:
`response_format: { type: "json_object" }` maps to Ollama's `format:
"json"`, and `response_format: { type: "json_schema", json_schema: {...} }`
maps to Ollama's schema-mode `format: <schema>`. No 400 — Ollama enforces
the shape server-side.

Configure the `local` alias target via `[ollama] default_model` in
`aitelier.toml` (default: `qwen3:8b`).

### Agent options — `extra_body.aitelier.*`

Anything the agent path needs that doesn't fit OpenAI's request shape rides
in `extra_body.aitelier`:

```json
{
  "model": "agent:claude/claude-sonnet-4-5",
  "messages": [...],
  "aitelier": {
    "workspace":      "/path/to/repo",
    "mcp_servers":    [{"name": "github", "transport": "http", "url": "..."}],
    "tool_allowlist": ["github.search", "shell.run"],
    "max_turns":      25,
    "prepare":   { /* install agents, run commands, seed files, start sidecars */ },
    "artifacts": { "fetch": ["/workspace/out.json"] },
    "trace_tag": "security-audit-2026-05",
    "examples":  [{"user": "...", "assistant": "..."}]
  }
}
```

The `aitelier` namespace is **only accepted when `model` starts with `agent:`** —
400 otherwise.

The `aitelier` block is `additionalProperties: false` — see
[`/v1/schemas/aitelier_request`](http://localhost:7777/v1/schemas/aitelier_request)
for the authoritative field list. The accepted properties are exactly:
`workspace, mcp_servers, tool_allowlist, max_turns, prepare, artifacts,
trace_tag, examples, allow_tool_drop`. Anything else (including
common-misplacement candidates like `timeout`, `model`, `messages`,
`stream`) is rejected.

#### Request-body `timeout` lives at the top level, not under `aitelier`

`timeout` is a server-side execution budget (integer seconds) and is a
**top-level body field**, peer to `model` / `messages` / `stream` —
exactly where the OpenAI client puts it. It is **not** an `aitelier.*`
option, and putting it there errors out under the strict schema.

```jsonc
{
  "model":    "agent:claude",
  "messages": [...],
  "timeout":  300,          // ← here
  "aitelier": {             // ← NOT in here
    "workspace": "/path",
    "trace_tag": "..."
  }
}
```

Both SDKs map this correctly: TS `submitRun({timeout: 300})` writes
`body.timeout`, Python `submit_run(..., timeout=300)` writes
`body["timeout"]`. If you handcraft requests, mirror that placement.

#### Multi-turn history

A `messages` array with prior `assistant` turns is accepted on the agent
path. Aitelier folds the conversation into a single inner-agent prompt
with this structure:

```
<conversation_history>
  <message role="user">…</message>
  <message role="assistant">…</message>
  <message role="tool">…</message>     ← any non-system role is included verbatim
</conversation_history>

<current_task>
…last user message…
</current_task>
```

System messages (any number) concatenate into the inner agent's system
prompt. The last non-system message must be `role="user"` (400 otherwise).
`role: "tool"` and `role: "function"` messages **are included** in
`<conversation_history>` verbatim with their original role attribute —
the inner agent decides whether to act on them.

The agent runs in a fresh ACP session per call, so prior turns are
replayed verbatim rather than persisted on the backend side. Cost
scales linearly in `aitelier_inner_tokens` with conversation length.

Cost implication: long histories grow linearly in `aitelier_inner_tokens`
because the inner agent re-processes the same prefix each turn. For
long-conversation deployments, lift stable prefixes into the system
prompt and rely on Anthropic prompt caching (next section).

#### Anthropic prompt caching (`cache_control`)

`cache_control` markers on message content blocks are passed through to
Anthropic. Aitelier auto-attaches the `anthropic-beta:
prompt-caching-2024-07-31` header on `claude*` / `anthropic/*` routes
whenever any message content block carries a `cache_control` marker —
without that header LiteLLM strips the marker silently and cache hits
go to zero.

```jsonc
{
  "model": "claude-haiku",
  "messages": [
    {"role": "system", "content": [{
      "type": "text",
      "text": "...long stable prefix...",
      "cache_control": {"type": "ephemeral"}
    }]},
    {"role": "user", "content": "..."}
  ]
}
```

Cache stats surface in `usage` on the response:

```
usage.cache_creation_input_tokens
usage.cache_read_input_tokens
usage.prompt_tokens_details.cached_tokens
usage.prompt_tokens_details.cache_creation_tokens
```

### What the agent path rejects

The agent path **hard-rejects** OpenAI fields it can't honestly honor:

- `tools` / `tool_choice` — the inner agent runs its own tools; we don't bridge
  them. Use `aitelier.tool_allowlist` to constrain which tools the agent uses.
- `n > 1` — the inner agent produces one response.
- `top_p` — the agent backend controls sampling.

Silent drops would be the "worst category of bug." 400 with a clear message
is the contract.

**Escape hatch for `tools` / `tool_choice` only**: consumers whose transport
sends `tools` per a global toolset config and can't suppress it per-profile
(Hermes) can set `aitelier.allow_tool_drop: true`. With that flag, `tools`
and `tool_choice` are silently dropped on the agent path. The opt-in is
explicit so consumers can't accidentally lose tools without knowing.

### Response identifiers

Every non-streaming response (and every SSE chunk) carries three IDs:

| Field | Meaning |
|---|---|
| `id` | OpenAI chat-completion id — `chatcmpl-<run_id>`. Set by aitelier so an OpenAI client's correlation logic works against aitelier responses. |
| `aitelier_run_id` | The aitelier run id. Use this to fetch `/v1/runs/{id}`, `/v1/runs/{id}/events`, or `/v1/runs/{id}/cancel`. |
| `aitelier_trace_id` | The trace id used in `/v1/traces` queries. Today this is always equal to `aitelier_run_id` — one trace per run. Kept as a separate field so future divergence (e.g., grouping retried runs under one trace) doesn't break consumers. |

### aitelier-specific response signals

Side-channel fields aitelier adds to OpenAI responses (consumers can
ignore them; they don't break the OpenAI shape):

- **`choices[i].message.reasoning_content`** — passed through verbatim from
  LiteLLM (Anthropic extended-thinking, qwen3 reasoning, Bedrock thinking
  all land here). Available in non-streaming responses for backends that
  surface it; in streaming, individual chunks carry
  `delta.reasoning_content` pieces.

- **`choices[i].message.aitelier_parsed`** — when `response_format` is
  `json_object` or `json_schema` and the model wrapped JSON in
  ` ```json` fences or prose ("Here you go: {...}"), aitelier fence-strips
  and parses. The raw text stays in `content`; the parsed value lands here.

- **`choices[i].aitelier_exit: "empty"`** — when `completion_tokens > 0`
  but `content == ""` and no `reasoning_content` and no `tool_calls`,
  aitelier flags it. Diagnoses "reasoning model burned its budget on
  hidden thinking" which OpenAI's `finish_reason` vocabulary can't
  express. Streaming surfaces this on a synthetic terminal chunk emitted
  just before `data: [DONE]`.

- **`aitelier_tool_call_count`** (int) and **`aitelier_tool_names`**
  (list of str) — present on every agent-path response, including the
  terminal SSE chunk of streaming responses. The inner-agent tools that
  fired during the run, so consumers can render "this run used Read,
  Edit, Bash" in the UI without a follow-up `GET /v1/runs/{id}/events`.
  Empty list / `0` when nothing fired.

- **`usage.aitelier_inner_tokens`** (int, agent path only) — the inner
  agent's hidden overhead (system prompt + tool schemas + intermediate
  reasoning) measured in tokens. Aitelier preserves the OpenAI invariant
  `total_tokens == prompt_tokens + completion_tokens` for user-visible
  I/O; this field surfaces the rest so consumers who care about
  subscription cost can sum the two. Omitted when zero (LLM path, or
  agent backends that don't surface the breakdown).

### Streaming

Set `stream: true`. The response is OpenAI-compatible SSE.

- **LLM path**: real token streaming via LiteLLM's SSE forwarding.
- **Agent path**: ACP `session/update` notifications mapped to OpenAI
  `delta.content` chunks. Tool-call / tool-result events from the inner
  agent are **not** surfaced as OpenAI `tool_calls` (those imply the
  consumer should respond) but the terminal SSE chunk carries
  `aitelier_tool_call_count` and `aitelier_tool_names` so consumers know
  what fired without a follow-up GET. The full event trace
  (`tool_call`, `tool_result`, `thought`, …) lives at
  `GET /v1/runs/{id}/events`.

Every chunk carries `aitelier_run_id`, `aitelier_trace_id`, and `correlation_id`
so consumers can correlate without parsing the body.

**Keepalive cadence.** During silent agent-planning phases — including
phases where the inner agent is emitting tool-call events that aitelier
drops from the wire — an SSE comment frame `: keepalive` is emitted at
most ~25 seconds since the last wire chunk. SSE parsers ignore comments,
but reverse proxies and client read timeouts see traffic and don't tear
down the connection. Consumers can safely set read timeouts ≥ 30s; for
very long-planning inner agents we recommend ≥ 60s.

## The control plane

Everything that doesn't fit OpenAI's vocabulary — long-running async
submissions, durable state queries, schedules, webhooks, cancellation —
lives on aitelier-native endpoints.

### `POST /v1/runs` — submit an async agent run

Long-running agent runs can outlive an HTTP connection. Submit them via
`/v1/runs`; aitelier returns a `run_id` immediately and webhook-delivers the
final ChatCompletion when the run finishes:

```bash
curl -X POST http://localhost:7777/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agent:claude",
    "messages": [{"role": "user", "content": "Long task..."}],
    "webhook_url": "https://your.app/aitelier-callback"
  }'
# → {"run_id": "2026-05-14T...-chat_agent_async", "status": "accepted",
#    "correlation_id": "...", "webhook_url": "https://..."}
```

The webhook receives the ChatCompletion (or an error body) when the run
completes. Failed runs whose process died are flipped to `orphaned` on
service restart (see [Run state machine](#run-state-machine)).

LLM calls don't go here — they're short enough to stream synchronously.
`/v1/runs` 400s on non-`agent:` models.

### `GET /v1/runs[/{id}[/events[/stream]]]`

Durable run state + append-only event timeline.

```
GET    /v1/runs                       # filter by trace_tag, state, correlation_id, ...
GET    /v1/runs/{run_id}              # one run record
GET    /v1/runs/{run_id}/events       # all events
GET    /v1/runs/{run_id}/events/stream # SSE — tails events live
```

### `POST /v1/runs/{run_id}/cancel`

Cancel an in-flight run by ID. Returns `{run_id, cancelled: true}` on
success, 404 if the run isn't in the active registry (already finished or
never started here).

### `GET /v1/runs/active`

Per-process registry of run IDs currently in flight. Useful for ops
dashboards. **Per-process only** — if aitelier scales horizontally, this
reflects only the contacted instance.

### `GET /v1/traces[/{id}|/aggregates]`

Trace-record summaries of runs (a narrower projection focused on
observability: model, kind, tokens, cost, status, error_type). The
`aggregates` endpoint groups by model, kind, or status for at-a-glance
dashboards.

### `/v1/schedules*`

```
GET    /v1/schedules                # list
POST   /v1/schedules                # create (interval or one-shot)
GET    /v1/schedules/{id}           # fetch
DELETE /v1/schedules/{id}           # remove
```

A schedule's `task` field is the same shape as a `/v1/chat/completions`
request body. When a schedule fires, aitelier builds the request and
routes it through the same code path as a live HTTP call.

```json
POST /v1/schedules
{
  "name": "nightly-feed-curator",
  "task": {
    "model": "agent:claude",
    "messages": [{"role": "user", "content": "Curate today's feeds."}]
  },
  "interval_seconds": 86400,
  "webhook_url": "https://your.app/aitelier-callback"
}
```

Set `at_iso` instead of `interval_seconds` for a one-shot trigger.
Failures route through the durable webhook worker (see
[Webhooks](#webhooks)).

### `GET /v1/health` / `GET /v1/discovery` / `GET /v1/metrics`

`/v1/health` is liveness only — does the process answer HTTP. No
dependency probing, no caching needed. Cheap enough for k8s liveness
probes on a tight cadence. Public (no auth required even in hosted mode).

`/v1/discovery` is the runtime source of truth: endpoint inventory, live
dependency probes (LiteLLM, Sandbox Agent, traces), per-model
`response_format` capabilities, and the `schemas.*` map pointing at
`GET /v1/schemas/{name}` (JSON Schema source for `aitelier_request`,
`run`, `run_event`, `schedule`, `active_runs`, `cancel`,
`traces_aggregate`, `discovery`). 5s response cache so consumers can
poll without hammering downstreams.

`/v1/metrics` is the operator endpoint for process-level health:

```json
{
  "uptime_seconds":  3712.482,
  "timestamp":       "2026-05-18T14:23:01Z",
  "process":         { "rss_mb": 71.4, "cpu_user_seconds": 12.3,
                       "cpu_system_seconds": 0.4 },
  "runs":            { "in_flight": 0,
                       "recent_5min": { "total": 50,
                                        "by_status": { "ok": 48, "error": 2 } } },
  "webhooks":        { "pending": 0 }
}
```

Reach for it when investigating memory growth, runaway in-flight runs,
or a webhook-delivery backlog. Never probes downstreams — use
`/v1/discovery` for that.

**Distinguishing aitelier-down from dep-down at call time.** When
LiteLLM is unreachable, `POST /v1/chat/completions` returns
`503 Service Unavailable` with `{"error": {"type": "ProviderUnavailable",
"message": "..."}}` — that's the typed signal consumers should branch
on rather than re-probing `/v1/discovery` per call. Other typed error
shapes: `Timeout` (504), `RateLimited` (429), `AuthError` (401),
`UnsupportedResponseFormat` (400), `ProviderError` (502, catch-all).

## Run state machine

Every inference call records a row in the `runs` table:

```
pending → running → {completed | failed | cancelled | orphaned}
```

| State | Meaning |
|---|---|
| `pending` | Spec inserted; awaitable hasn't started yet |
| `running` | Awaitable started |
| `completed` | Returned a non-error result |
| `failed` | Threw an exception or returned `status="error"` |
| `cancelled` | Caller cancelled via `POST /v1/runs/{id}/cancel` |
| `orphaned` | Was `running` when the previous aitelier process died; flipped on startup |

`orphaned` is set on aitelier startup for any row left in `pending`/`running`
from a previous process — Sandbox Agent has no session-resume API today, so
those sessions are unrecoverable. Dashboards should treat `orphaned` as a
terminal failure mode.

## Available models

LiteLLM resolves both curated aliases (declared in `docker/litellm/config.yaml`)
and pass-through wildcards (`anthropic/*`, `openai/*`, `ollama/*`). Aliases
exist so the most common models have short, stable names; pass-through
covers everything else without aitelier needing config changes.

| Type | Examples |
|---|---|
| Curated alias | `claude-sonnet`, `claude-haiku`, `local`, `nomic-embed-text` |
| Anthropic wildcard | `anthropic/claude-opus-4-7`, `anthropic/claude-haiku-3.5` |
| OpenAI wildcard | `openai/gpt-4o`, `openai/gpt-4-turbo` |
| Ollama wildcard | `ollama/qwen2.5-coder`, `ollama/llama3.2:8b` |
| Agent (Claude Code) | `agent:claude`, `agent:claude/claude-sonnet-4-5` |
| Agent (Codex) | `agent:codex`, `agent:codex/gpt-4o` |

Capability discovery at runtime: `GET /v1/models` returns each model
annotated with the `response_format` types it supports.

### `response_format` normalization

The LLM path honors OpenAI-spec `response_format` with per-provider gating:

- `json_schema` on **OpenAI / `gpt-*`**: passthrough.
- `json_schema` on **Anthropic / `claude-*`**: **hard-rejected** with
  `UnsupportedResponseFormat`. LiteLLM's Anthropic adapter rejects
  OpenAI-shape `json_schema` at its parameter-translation step; we
  refuse up-front instead of returning the resulting 502 traceback.
  Use `json_object` for soft JSON enforcement on claude models.
- `json_schema` on **Ollama / `local`**: passes through to Ollama's
  native schema-mode `format: <schema>`. Aitelier bypasses LiteLLM for
  these models — Ollama enforces the shape server-side.
- `json_object` on **Ollama / `local`**: passes through as Ollama's
  `format: "json"`. Same bypass path.
- `json_object` on **OpenAI / `gpt-*`**: passthrough.
- `json_object` on **providers without native support** (including
  Anthropic): format is stripped and a "return JSON only" system
  directive is injected. The consumer still gets JSON, just via prompt
  engineering. Documented soft fallback.

Defense in depth: if upstream regresses and returns a 5xx whose body
mentions the OpenAI `response_format` translation path, aitelier
reclassifies it as `UnsupportedResponseFormat` (400) instead of letting
the traceback leak to consumers.

## Available agents

Sandbox Agent advertises its backends at `GET /v1/discovery →
dependencies.sandbox_agent.agents`. Typical list:

`claude` (Claude Code), `codex` (OpenAI Codex CLI), `opencode`, `cursor`,
`amp`, `pi`.

Use `model: "agent:<backend>"` to route to one. Optional inner-LLM override:
`model: "agent:<backend>/<inner-llm>"`.

## Multi-agent workflows

Aitelier is the execution backend. It exposes the primitives a
multi-agent system needs — sandboxed agent runs, async submission,
cancellation, observability, idempotency, correlation — without
imposing a workflow shape. The patterns below show how the mechanism
composes; pick whichever fits your orchestrator.

### Approach 1: agent dispatches subagents via HTTP loopback

The parent agent calls aitelier's HTTP API directly (claude-code has
`Bash` + `curl`; codex has `commandExecution`). The agent submits a
child run, gets the `run_id` back, and either polls or registers a
webhook for completion.

```bash
# Inside the parent agent's sandbox:
curl -s -X POST http://localhost:7777/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "agent:codex",
    "messages": [{"role": "user", "content": "Audit deps in /workspace/pkg.json"}],
    "aitelier": {
      "parent_run_id":  "'"$AITELIER_RUN_ID"'",
      "trace_tag":      "deps-audit-2026-05",
      "workspace":      "/workspace"
    }
  }'
```

In a Brig-cell deployment, replace `localhost:7777` with
`http://aitelier.host.brig/v1`. For local dev where you need
loopback webhook callbacks, set `[service] allow_loopback_webhooks = true`.

### Approach 2: agent loads `aitelier-mcp` as an MCP server

`aitelier-mcp` is a small MCP server (separate package, sibling of
`aitelier-client`) that exposes the control plane as typed MCP tools:
`submit_run`, `get_run`, `list_runs`, `list_run_events`, `cancel_run`.
The inner agent uses its MCP-tool channel (both `claude` and `codex`
advertise `mcpTools: true`) rather than hand-rolling HTTP.

```jsonc
{
  "model": "agent:claude",
  "messages": [{"role": "user", "content": "Run audit + lint in parallel; summarize."}],
  "aitelier": {
    "mcp_servers": [
      {
        "name": "aitelier",
        "transport": "stdio",
        "command": "aitelier-mcp"
      }
    ]
  }
}
```

The agent's tool-use loop now sees `submit_run`, `list_runs`, etc.
alongside its other tools. Whatever orchestration pattern the agent
runs — fan-out + merge, sequential handoff, retry-on-failure — happens
inside the inner agent's reasoning; aitelier just executes each
submitted run.

### Recovering a workflow's tree

Use `parent_run_id` (and optionally `trace_tag`) to reconstruct
lineage after the fact:

```bash
# All children of one orchestrator run:
curl 'http://localhost:7777/v1/runs?parent_run_id=orchestrator-run-id'

# All runs in a workflow, with token + cost rollups:
curl 'http://localhost:7777/v1/traces?trace_tag=deps-audit-2026-05'

# Aggregate by status:
curl 'http://localhost:7777/v1/traces/aggregates?group_by=status&trace_tag=deps-audit-2026-05'
```

`parent_run_id` is a pure pass-through field: aitelier records it but
doesn't enforce hierarchy semantics (no FK, no cycle check, no cascade
cancellation). The orchestrator above aitelier owns the meaning. Pair
with `trace_tag` so a whole workflow's runs (including children of
children) share one queryable handle.

### What aitelier does *not* do

- **No built-in fanout primitive** (`parallel: [...]` in the request).
  Picks a workflow shape. Use `asyncio.gather` / `Promise.all` /
  whatever your orchestrator gives you.
- **No coordinator agent type**. There's no `agent:orchestrator`
  privileged backend.
- **No graph DSL / DAG runner**. Workflow shape lives in your
  orchestrator (LangGraph, Crew, Mastra, or hand-rolled).
- **No automatic parent→child cancellation propagation**. `POST /v1/runs/{id}/cancel`
  is per-run. Whether cancelling a parent should also cancel children
  is a consumer policy — query `list_runs(parent_run_id=parent)`,
  iterate, cancel each.
- **No "share inner-agent context across calls"**. ACP sessions are
  per-call by design. To pass state from a parent agent's reasoning
  to a child, marshal it into the child's `messages`.

### Visibility limit: subagents *inside* claude-code

Claude Code's `.claude/agents/*.md` subagent mechanism dispatches
**inside** the inner agent process. To aitelier (one rung up at the
ACP layer), the entire parent + subagent sequence looks like one
long turn: subagent identity, prompt, and per-subagent token cost
are merged into the outer run's row. If a parent claude run fires
three subagents internally, you'll see one aitelier run with merged
tokens — not four runs with parent_run_id edges.

To make subagent dispatch visible to aitelier, the parent must
submit each child as its own aitelier run (Approach 1 or 2). Then
`parent_run_id` records the edge and `/v1/traces?parent_run_id=X`
recovers the subtree.

## Webhooks

Async run completions and scheduled jobs route through a durable webhook
worker:

- **Queued** in Postgres (table `webhook_deliveries`).
- **Retry policy**: exponential backoff `1s / 5s / 30s / 5min / 1hr`,
  5 attempts.
- **Optional HMAC signing**: set `[service] webhook_secret` to enable a
  `X-Aitelier-Signature: sha256=<hmac>` header on every delivery.
- **SSRF guard** (hosted mode only): when `[service] api_key` is set,
  webhook URLs must resolve to a public, non-loopback host.

The payload is the ChatCompletion (success) or
`{error: {...}, aitelier_run_id}` (failure).

**Orphan webhook**: if aitelier restarts while an async run is in flight,
the run flips to `orphaned` on startup and a terminal webhook fires with
`{error: {type: "Orphaned", ...}, aitelier_state: "orphaned"}`. Sandbox
Agent has no session-resume API today, so the run itself is
unrecoverable — but the consumer no longer polls forever.

## Error handling

aitelier returns OpenAI-shaped error responses:

```json
{
  "error": {
    "type":    "RateLimited",
    "message": "...",
    "code":    "error"
  },
  "aitelier_run_id": "..."
}
```

Error types (classified in `core/src/aitelier/errors.py`):

| `error_type` | Triggers | Retry? |
|---|---|---|
| `ProviderUnavailable` | ConnectError, NetworkError | Yes |
| `Timeout` | Request timed out | Yes |
| `RateLimited` | HTTP 429 from upstream | Yes (honor `Retry-After`) |
| `AuthError` | HTTP 401/403 | No |
| `ProviderError` | Other 4xx/5xx from upstream | No |
| `UnsupportedResponseFormat` | `json_schema` on a provider without support | No (change model or drop the format) |
| `PrepareFailed` | `aitelier.prepare` phase failed before the agent ran | No |
| `NonZeroExit` | Agent CLI exited with error | No |
| `Cancelled` | Run cancelled via `/v1/runs/{id}/cancel` or consumer disconnect | No |
| `Orphaned` | Run was in-flight when aitelier restarted; finalised via orphan-sweep + terminal webhook (see "Run state machine" → `orphaned`) | No |
| `SchemaViolation` | JSON parse / validation error | No |

### Retries

Retries are **the OpenAI SDK's responsibility** on the LLM path —
configure them via the OpenAI client options. Aitelier doesn't add a
second retry layer.

For async agent submissions (`POST /v1/runs`), retries don't apply because
the call returns immediately. Use the `Idempotency-Key` header to make
re-submissions safe.

## Idempotency

`POST /v1/chat/completions` (agent path) and `POST /v1/runs` accept
`Idempotency-Key: <uuid>` headers. On replay with the same key + body,
aitelier returns the cached response (24h window). On the same key with a
different body, 422.

**Streaming is covered.** When `stream: true` plus an `Idempotency-Key`
header, aitelier records the SSE chunks on successful completion and
replays the same chunks (with the same `aitelier_run_id` on each) on a
retry with the same key + body. The consumer sees a fresh SSE stream
that's byte-identical to the original — no re-execution of inner-agent
work or side effects.

**Caching boundaries.** Only *successful* completions are cached.
Streams that ended in an error frame, were cancelled, or had the
producer task aborted are not stored — a retry produces a fresh attempt
at success rather than locking the consumer into the failure.

Useful for retried submissions where the agent path has side effects
(`aitelier.prepare.commands`, sidecars, file writes).

## Cancellation

In-flight runs can be cancelled by `run_id`:

```
GET  /v1/runs/active            # → {"active": ["2026-05-14T...-chat_agent", ...]}
POST /v1/runs/{run_id}/cancel   # → {"run_id": "...", "cancelled": true}
```

Cancelling a sync `/v1/chat/completions` call surfaces as an error response
(`error_type: "Cancelled"`). Cancelling a stream emits a terminal error
chunk. Cancelling a run that already finished returns 404.

Get the `run_id`: sync responses include `aitelier_run_id` in the body;
streaming chunks include it in each event; async submissions return it
immediately.

The active-run registry is **per-process** — if aitelier scales horizontally,
the list reflects only the contacted instance.

## Correlation IDs

Every request accepts `X-Correlation-Id: <opaque-string>`. If absent,
aitelier generates one.

A correlation ID is:

- echoed in `X-Correlation-Id` response header,
- included in the response JSON as `correlation_id`,
- included in every SSE chunk on streaming endpoints,
- persisted as `runs.correlation_id` (and inside `runs.metadata`) for every
  call, queryable via `GET /v1/runs?correlation_id=…`,
- propagated to log lines emitted during the request via a contextvar.

## Configuration

**Aitelier reads zero env vars in app code.** All settings come from TOML,
loaded in this layered order (later layers override earlier ones):

1. **Defaults** — dataclass fields in `core/src/aitelier/config.py`.
2. **Base config** — explicit `aitelier --config <path>`, else:
   - `./aitelier.toml` (repo-local), or
   - `~/.config/aitelier/config.toml` (user-global).
3. **Secrets overlay** — `aitelier.secrets.toml` next to the base config
   (gitignored). Same TOML shape; put API keys, OAuth tokens, and webhook
   secrets here.
4. **Session overlay** — `runs/.session.toml` (gitignored, ephemeral).
   Written by `scripts/start.sh` for runtime-discovered values (chosen
   sandbox-agent port, dev Postgres DSN). Cleaned up by `scripts/stop.sh`.

| Section | Common fields |
|---|---|
| `[database]` | `url` (Postgres DSN; unset = InMemoryStore for dev) |
| `[litellm]` | `base_url`; `api_key` lives in `aitelier.secrets.toml` |
| `[sandbox_agent]` | `base_url`; `token` (remote mode) in secrets overlay |
| `[service]` | `host`, `port`, `log_format` (`human` or `json`); `api_key` + `webhook_secret` in secrets overlay |
| `[storage]` | `max_metadata_bytes` (default 65536) |
| `[ollama]` | `mode` (`host` or `docker`), optional `base_url` |

The SDK clients (Python + TypeScript) read `~/.config/aitelier/config.toml`'s
`[service]` host/port for their default `baseUrl`; pass an explicit `base_url`
to override.

## Security model

aitelier supports two modes:

- **Localhost-trust** (default) — `127.0.0.1` bind, no auth required. Any
  process on the host can call. SSRF guards are off for ergonomic dev.
- **Hosted mode** — set `[service] api_key`. Every `/v1/*` except
  `/v1/health` requires `Authorization: Bearer <key>`. SSRF guard activates
  on webhook URLs (no loopback / private ranges / metadata services).

Always combine hosted mode with TLS termination upstream. Bearer over
plain HTTP is unsafe.

## Cost tracking

LLM-path calls go through LiteLLM, which exposes cost via the
`response._hidden_params["response_cost"]` field. aitelier persists this
on `runs.cost_usd` when LiteLLM reports it.

Agent-path runs always have `cost_usd: null` by design: agent LLM calls
happen inside the Sandbox Agent process and go directly to Anthropic /
OpenAI, bypassing LiteLLM. Token usage *is* captured when the backend
surfaces it (`runs.input_tokens` / `output_tokens` / `total_tokens`).

## SDK reference

Both SDKs share the same shape: a thin `Aitelier` client for the control
plane plus a `.openai()` helper for inference.

### Inference — `.openai()`

Returns a preconfigured OpenAI client pointed at this aitelier service.
Lazy import: requires the `openai` package as a peer dependency.

```python
ait = Aitelier(api_key="...")
openai = ait.openai()                          # AsyncOpenAI
resp = await openai.chat.completions.create(...)
```

```typescript
const ait = new Aitelier({ apiKey: "..." });
const openai = await ait.openai();             // OpenAI
const resp = await openai.chat.completions.create(...);
```

Use the OpenAI SDK directly for: streaming, retries, structured outputs,
tool semantics, embeddings, model listing.

### Control plane

| Method | Endpoint |
|---|---|
| `submit_run` / `submitRun` | `POST /v1/runs` |
| `cancel_run` / `cancelRun` | `POST /v1/runs/{id}/cancel` |
| `list_active_runs` / `listActiveRuns` | `GET /v1/runs/active` |
| `get_run` / `getRun` | `GET /v1/runs/{id}` |
| `list_runs` / `listRuns` | `GET /v1/runs` |
| `list_run_events` / `listRunEvents` | `GET /v1/runs/{id}/events` |
| `stream_run_events` (Python) | `GET /v1/runs/{id}/events/stream` (SSE) |
| `recent_traces` / `recentTraces` | `GET /v1/traces` |
| `get_trace` / `getTrace` | `GET /v1/traces/{id}` |
| `aggregate_traces` / `aggregateTraces` | `GET /v1/traces/aggregates` |
| `list_schedules` / `listSchedules` | `GET /v1/schedules` |
| `create_schedule` / `createSchedule` | `POST /v1/schedules` |
| `get_schedule` / `getSchedule` | `GET /v1/schedules/{id}` |
| `delete_schedule` / `deleteSchedule` | `DELETE /v1/schedules/{id}` |
| `discovery` | `GET /v1/discovery` |
| `health` | `GET /v1/health` |
| `get_schema` / `getSchema` | `GET /v1/schemas/{name}` |

## What aitelier does NOT do

- **Multi-tenancy** — single-developer use; in-process active-run registry is fine.
- **Authentication / authorization beyond Bearer** — hosted mode is for trusted access; SSO/RBAC isn't justified.
- **Cost budgets / rate limiting per consumer** — let the LLM provider enforce.
- **A web dashboard** — structured logs + `/v1/traces*` cover the current need.
- **Bridge inner-agent tool calls to consumer-side tools** — the inner agent runs its own tools; consumers can't fulfill them via OpenAI's `tools` parameter.
