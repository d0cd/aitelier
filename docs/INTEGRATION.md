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
    model="claude-sonnet",
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
| `claude-sonnet`, `nomic-embed-text`, `local`, … | LiteLLM (alias) | Curated short names from `docker/litellm/config.yaml` |
| `anthropic/*`, `openai/*`, `ollama/*` | LiteLLM (wildcard) | Pass-through; LiteLLM resolves the provider |
| `agent:<backend>` | Sandbox Agent | Backend's default inner LLM |
| `agent:<backend>/<inner-llm>` | Sandbox Agent | Explicit inner LLM (e.g. `agent:claude/claude-sonnet-4-5`) |

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
- **Agent**: `{id: "agent:<backend>", aitelier_agent: true, aitelier_inner_llms: [...], aitelier_reasoning_levels: [...], aitelier_approval_modes: [...], aitelier_capabilities: {...}}` — the Sandbox Agent capability flags plus the backend's **actually-advertised** options (probed live per backend and cached): the real inner-model ids, reasoning levels, and approval modes it accepts. These are the authoritative ids for `agent:<backend>/<model>` + `aitelier.reasoning_effort` / `aitelier.approval_mode`; passing one the backend doesn't offer fails fast with the valid list. The three option arrays are omitted for a backend whose probe fails (the entry still appears). See "Selecting the inner model" below.

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

For inner-LLM selection on agent routes, see "Selecting the inner model"
under [Available agents](#available-agents) — the id must be backend-native,
and `aitelier_inner_llms` is not the authoritative per-backend list.

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

For Ollama hybrid-reasoning models (qwen3, deepseek-r1, …) aitelier
maps OpenAI's canonical `reasoning_effort` enum (`minimal`, `low`,
`medium`, `high`) to Ollama's binary `think` toggle:

| `reasoning_effort` | Ollama `think` | Behavior |
|---|---|---|
| omitted | (unset) | Model default. Qwen3 family defaults to thinking ON; deepseek-r1 always thinks. |
| `"minimal"` | `false` | Disable thinking entirely. Use this for structured-output tasks that don't benefit from chain-of-thought. |
| `"low"`, `"medium"`, `"high"` | `true` | Enable thinking. Ollama's `think` is binary; aitelier doesn't translate the gradient further. |

```ts
// Structured-output workflow — disable thinking explicitly to avoid
// silent empty-content failures when reasoning exhausts max_tokens.
await openai.chat.completions.create({
  model: "ollama/qwen3:8b",
  messages: [...],
  reasoning_effort: "minimal",
  response_format: { type: "json_schema", json_schema: {...} },
});
```

Why this matters: qwen3 in thinking mode under a tight `max_tokens`
budget can burn the entire budget on hidden chain-of-thought and
return `content=""` with `finish_reason: "length"` — a silent failure
shape that's easy to miss in batch pipelines. Setting
`reasoning_effort: "minimal"` is the explicit opt-out.

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
`workspace, mcp_servers, tool_allowlist, max_turns, reasoning_effort,
approval_mode, prepare, artifacts, trace_tag, parent_run_id, examples,
allow_tool_drop`. Anything else (including
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

#### `aitelier.approval_mode` for batch / non-interactive callers

A backend's deliberation/approval behavior is controlled by its advertised
`mode` option, surfaced as `aitelier.approval_mode` (see "Reasoning effort &
approval mode"). For codex, `auto` lets it read+edit+run without per-step
approval; `read-only` is the cautious default. For claude, `plan` makes it
plan-before-acting and `bypassPermissions`/`acceptEdits` reduce prompts.

```jsonc
{
  "model": "agent:codex",
  "messages": [...],
  "aitelier": { "approval_mode": "auto" }
}
```

Omit to inherit the backend default. Symmetric with `max_turns` —
caller specifies what it needs.

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
  them. Use `aitelier.tool_allowlist` (claude-only) to constrain which tools the
  agent uses.
- `n > 1` — the inner agent produces one response.
- **Sampling / decoding / length budget** — `temperature`, `top_p`, `max_tokens`,
  `max_completion_tokens`, `seed`, `stop`, `frequency_penalty`, `presence_penalty`,
  `logprobs`, `top_logprobs`: the inner agent controls these itself. Bound a run
  with the top-level `timeout`.
- `aitelier.max_turns` / `aitelier.tool_allowlist` on **non-claude** backends —
  claude-only (Claude Agent SDK options); other backends have no ACP channel.

Accepted-and-honored on the agent path: `response_format` (`json_object` and
`json_schema` both fold a directive into the prompt — best-effort, since coding
agents may ignore native schema), `stream_options` (`include_usage: false`
suppresses the terminal usage chunk), and the `aitelier.*` block. `user` is
accepted and recorded in the run's `request_body` for audit/correlation but is
not forwarded to the inner agent (no agent-side mechanism honors it). Silent
drops would be the "worst category of bug" — 400 with a clear message is the
contract.

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
| `aitelier_run_id` | The aitelier run id. Use this to fetch `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/runs/{id}/cancel`, or `/v1/traces/{id}` (trace id == run id; one trace per run). |
| `correlation_id` | The request's correlation id — echoed from `X-Correlation-Id` (or minted if absent). Also returned in the `X-Correlation-Id` response header and stamped on every log line for the request. Use it to tie your logs to aitelier's. |

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

Every chunk carries `aitelier_run_id` and `correlation_id` so consumers
can correlate without parsing the body.

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
# → {"run_id": "9f3c2a1b...e07", "status": "accepted",
#    "correlation_id": "...", "webhook_url": "https://..."}
#   run_id is a 128-bit hex value — also the W3C trace id (trace_id == run_id).
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

### `POST /v1/runs/{run_id}/wait`

Server-side polling: blocks until the run reaches a terminal state
(`completed`, `failed`, `cancelled`, `orphaned`), then returns the Run
row.

```
POST /v1/runs/{run_id}/wait?timeout=60&poll_interval=0.5
```

- `timeout` ∈ (0, 600] seconds (default 60). 408 when elapsed with
  the run still pending/running — caller retries to keep waiting.
- `poll_interval` ∈ (0, 10] seconds (default 0.5).
- 404 if the run id doesn't exist.

SDK methods: `Aitelier.wait_for_run(run_id, timeout=…)` (Python) and
`Aitelier.waitForRun(runId, { timeoutSeconds })` (TypeScript). Use when
you submitted async via `POST /v1/runs` and don't want to stand up a
webhook receiver.

### `POST /v1/runs/{run_id}/cancel`

Cancel an in-flight run by ID. Returns `{run_id, cancelled: true}` on
success, 404 if the run isn't in the active registry (already finished or
never started here).

### `GET /v1/runs/active`

Per-process registry of run IDs currently in flight. Useful for ops
dashboards. **Per-process only** — if aitelier scales horizontally, this
reflects only the contacted instance.

### `POST /v1/runs/{run_id}/scores` / `GET /v1/runs/{run_id}/scores`

Scoring sink for bolt-on eval frameworks (Langfuse / Phoenix / PromptFoo
/ custom). Aitelier owns durable storage; the grading logic lives in
the caller.

```bash
curl -X POST http://localhost:7777/v1/runs/run-123/scores -d '{
  "name": "helpfulness",
  "value": 0.85,
  "evaluator": "gpt-4o-judge",
  "comment": "answer was concrete and on-topic",
  "metadata": {"rubric_version": 2}
}'
```

- **No uniqueness** on `(run_id, name, evaluator)` — re-grading writes
  a new row. `GET` returns all rows ordered by `created_at`; consumers
  that want "latest" take `[-1]`.
- **`name` and `evaluator`** are charset-restricted (`[A-Za-z0-9_\-.]`
  and `[A-Za-z0-9_\-.:/]` respectively) so they're safe to use in log
  lines and downstream aggregation queries.
- **`value`** is unconstrained — different rubrics use different ranges
  (0..1 normalized, 1..5 Likert, raw token counts, latency budgets).

### `GET /v1/runs/export`

Bulk NDJSON stream — one full `Run` row per line, including the
captured `request_body` and `rendered_messages`. Designed for backfill
grading: a one-shot export feeds a grader without paging through 500-
row windows.

```bash
curl 'http://localhost:7777/v1/runs/export?since=2026-04-01&trace_tag=audit'
# {"run_id": "run-1", "request_body": {...}, ...}
# {"run_id": "run-2", "request_body": {...}, ...}
```

Filters mirror `GET /v1/runs` (`since`, `until`, `trace_tag`, `kind`,
`state`). Default `limit` is 10000, bumpable to 100000.

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
`run`, `run_event`, `run_score`, `schedule`, `active_runs`, `cancel`,
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

### Captured request body + rendered messages

Every run row (from migration v4 onward) carries the actual request
under two fields:

- `request_body` — the `ChatCompletionRequest` / `AsyncRunRequest` /
  `EmbeddingsRequest` body as the caller submitted it, before any
  aitelier-side translation. What you POST is what you read back.
- `rendered_messages` — the message list after aitelier's agent-path
  translations (system-prompt fold, response_format injection,
  `<aitelier_context>` block). What actually went on the wire to the
  provider. On the LLM path this mostly mirrors `request_body.messages`;
  on the agent path it collapses to `[{role: system, content: <fold>},
  {role: user, content: <last user message>}]`.

Both are `null` for runs created before the v4 migration (historical
state) and for synthetic schedule-side failures that didn't have a
parseable task body. Operators can distinguish "no record" (`null`)
from "empty body sent" (`{}`) — the projection preserves the
distinction.

Both fields pass through the same secret-redaction projection as
`environment` / `result` / `metadata`: `Authorization: Bearer …`
shapes inside `aitelier.mcp_servers[*].headers` (and equivalent
credential keys named `api_key` / `token` / `secret`) are redacted at
the HTTP boundary. The stored Postgres row keeps the originals for
operator debugging.

This unlocks several downstream surfaces (see `docs/PLAN.md` Tier 1):
the Phase H replay endpoint, a static `/ui` browser, bolt-on eval
frameworks reading the captured input via `/v1/runs/{id}` or a future
bulk export, and OpenTelemetry GenAI export (`gen_ai.prompt` /
`gen_ai.completion` conventions).

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
| Agent (Codex) | `agent:codex`, `agent:codex/gpt-5.4` |

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

Use `model: "agent:<backend>"` to route to one. Omitting the inner LLM runs
the backend's own default model.

### Selecting the inner model

Append `/<inner-llm>` to pick the model the backend runs:
`model: "agent:<backend>/<inner-llm>"`. **The id must be one the backend itself
recognizes — a backend-native id, not an aitelier LLM-path id.** `/v1/models`
lists each backend's real ids under `aitelier_inner_llms` (see below).

| Backend | Inner-model id format | Examples | Wired via |
|---|---|---|---|
| `claude` | Anthropic aliases or full ids | `agent:claude/sonnet`, `agent:claude/haiku`, `agent:claude/claude-sonnet-4-5` | `session/new` `_meta` |
| `codex` | Codex-native ids | `agent:codex/gpt-5.4`, `agent:codex/gpt-5.3-codex` | `session/set_model` |
| `opencode`, `cursor`, `amp`, `pi` | the backend's own ids | (see `/v1/models`) | `session/set_model` |

Common mistake: `agent:codex/openai/gpt-4o`. The `openai/*` strings (and curated
aliases like `claude-sonnet`) are **LLM-path** ids — they work for
`/v1/chat/completions` with `model: "openai/gpt-4o"`, but a coding-agent backend
doesn't understand them. aitelier validates the id against the model list the
backend advertises and, on a miss, returns a `ProviderError` **before** running
any turn, naming the ids that are valid:

```jsonc
// agent:codex/openai/gpt-4o  →  HTTP 500
{ "error": { "type": "ProviderError",
  "message": "backend 'codex' does not offer inner model 'openai/gpt-4o'. Available: gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex, gpt-5.3-codex-spark, gpt-5.2. Use a backend-native id ..." } }
```

### Reasoning effort & approval mode

Two `aitelier.*` knobs map to the corresponding session option each backend
advertises (normalized on the ACP option *category*, so the same request works
across backends even though the underlying ids differ):

```jsonc
{ "model": "agent:codex/gpt-5.4",
  "aitelier": { "reasoning_effort": "high", "approval_mode": "auto" } }
```

- **`reasoning_effort`** → the backend's `thought_level` option. codex:
  `low/medium/high/xhigh`; claude: `low/medium/high/xhigh/max`. (Falls back to
  the top-level OpenAI `reasoning_effort` field when unset.)
- **`approval_mode`** → the backend's sandbox/approval preset. codex:
  `read-only/auto/full-access`; claude: `auto/default/acceptEdits/plan/dontAsk/bypassPermissions`.

Both are validated against the backend's advertised values at session start; an
unknown value returns a `ProviderError` listing the valid ones, before any turn.

### Discovering valid ids — `/v1/models`

Each agent entry carries the backend's actually-advertised options (probed live,
cached), so consumers can pre-validate instead of hard-coding:

```jsonc
{ "id": "agent:codex", "aitelier_agent": true,
  "aitelier_inner_llms":      ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.3-codex-spark", "gpt-5.2"],
  "aitelier_reasoning_levels": ["low", "medium", "high", "xhigh"],
  "aitelier_approval_modes":   ["read-only", "auto", "full-access"] }
```

These fields are omitted for a backend whose probe fails (the entry still
appears). They are the authoritative source of valid ids — not the LLM-path
catalog.

### Claude-only options on other backends

`system_prompt`, `aitelier.max_turns`, and `aitelier.tool_allowlist` only have an
ACP channel on `claude` (they ride `session/new` `_meta` into the Claude Agent
SDK). A system prompt is still delivered to other backends — folded into the
prompt text — but `max_turns` / `tool_allowlist` can't be honored, so the agent
path **rejects them with a 400** rather than dropping them silently. For tool
access on codex etc., use `aitelier.approval_mode`; bound the run with the
top-level `timeout`.

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

Aitelier surfaces the parent's own run_id two ways so the child can
record lineage via `parent_run_id`:

1. **`<aitelier_context>` block** prepended to the parent agent's
   system prompt: `run_id={parent_run_id}`. Always present.
2. **`AITELIER_RUN_ID` env var** injected into every stdio MCP server
   spawned by the parent. The `aitelier-mcp` package's `get_my_run_id`
   tool reads it (see Approach 2).

```bash
# Inside the parent agent's sandbox. The agent parses its own run_id
# out of the system-prompt context block and passes it explicitly:
curl -s -X POST http://localhost:7777/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "agent:codex",
    "messages": [{"role": "user", "content": "Audit deps in /workspace/pkg.json"}],
    "aitelier": {
      "parent_run_id":  "<parent run_id from the context block>",
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
`submit_run`, `get_run`, `list_runs`, `list_run_events`, `cancel_run`,
plus `get_my_run_id` (returns the parent's own aitelier run_id from
the `AITELIER_RUN_ID` env var aitelier injects). The inner agent
uses its MCP-tool channel (both `claude` and `codex` advertise
`mcpTools: true`) rather than hand-rolling HTTP.

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
- **Retry policy**: exponential backoff `1s / 5s / 30s / 5min / 1hr`
  (5 retry delays), then marked `failed` on the 6th attempt.
- **Optional Bearer auth**: set `[service] webhook_secret` to send an
  `Authorization: Bearer <secret>` header on every delivery. Receivers
  verify with a constant-time compare:
  ```python
  import hmac
  token = request.headers.get("Authorization", "").removeprefix("Bearer ")
  if not hmac.compare_digest(token, expected_secret):
      reject()  # 401
  ```
  (Bearer over HTTPS rather than an HMAC body signature — HTTPS already
  protects body integrity in transit, and body-byte fidelity between
  sender and receiver is easy to get wrong.)
- **SSRF guard** (always on): webhook URLs must resolve to a public,
  non-loopback host at enqueue time AND at delivery time (DNS-rebinding
  protection across both windows). Set `[service]
  allow_loopback_webhooks = true` to opt back into loopback callbacks
  for local dev. The check applies in both localhost-trust and hosted
  modes.

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
GET  /v1/runs/active            # → {"active": ["9f3c2a1b...e07", ...]}
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

### Hosted-mode deployment envelope

aitelier is built for trusted-team deployments, not multi-tenant SaaS.
The following are intentionally **not** features — design around them:

- **Single shared API key.** All `Authorization: Bearer …` callers share
  one identity. Rotate the key by editing `aitelier.secrets.toml` and
  restarting. No per-user keys, no scopes, no audit trail of which
  consumer made which call (use `X-Correlation-Id` to thread requests
  across systems instead).
- **Schedule task blobs are visible to every key-holder.** `GET
  /v1/schedules` returns the full `task` dict, including
  `aitelier.mcp_servers[*].headers` (typically a Bearer/PAT for the MCP
  server) and `aitelier.prepare.commands[*].env` (often DB DSNs or
  registry creds). Treat the schedule store as team-shared.
- **No body-size cap by default beyond a configurable 4 MiB.** Tune
  `service.max_request_body_bytes` for your traffic. Put a reverse
  proxy in front of hosted aitelier if you need a hard external cap.
- **No per-caller rate limit by default.** Set
  `service.rate_limit_per_minute` to a per-key budget (e.g. 600); it
  excludes `/v1/health`. Returns 429 + `Retry-After`.
- **Background purges run hourly by default.** Tune via `[purge]
  interval_seconds`, `webhook_retention_days`, `event_retention_days`.
  Set `interval_seconds = 0` to disable (only safe if you handle
  cleanup externally; the startup `runs` purge still runs).

Recommended hosted-mode TOML:

```toml
[service]
api_key = "rotate-me-quarterly"      # in aitelier.secrets.toml
webhook_secret = "rotate-me-too"     # in aitelier.secrets.toml
max_request_body_bytes = 4194304
rate_limit_per_minute = 600
max_in_flight_runs = 32

[purge]
interval_seconds = 3600
webhook_retention_days = 7
event_retention_days = 30

[service]
# For brig-cell or any deployment where you want to bound what host
# paths consumers can hand to the agent. Empty (default) = no allowlist.
allowed_workspace_roots = ["/var/lib/aitelier/workspaces"]
```

### Workspace + artifact path validation

`aitelier.workspace`, `aitelier.artifacts.fetch[*]`, and
`aitelier.prepare.files[*].path` are validated at the request boundary
on every agent-path call:

- **`..` components are refused** regardless of resolved location.
- **Symlinked components are refused** when the path is absolute.
  Stops the trivial "consumer hands aitelier a workspace that points
  through a symlink to `/etc`" vector. Once the path is resolved on
  dispatch, every component (including the leaf) must be a real
  directory or file.
- **Allowlist** (`service.allowed_workspace_roots`) — when set, the
  resolved path must be a descendant of one of the listed roots. For
  brig-cell deployments, set this to the cell's workspace mount root
  (`/Users/<u>/.brig/state/<cell>/workspace`).

**What this does *not* fix:** the agent's own filesystem tool calls
(claude-code's `Read`, `Bash`, etc.) bypass aitelier and go directly
through the Sandbox Agent's filesystem layer. A symlink *inside* the
workspace pointing outside the workspace will still be followed by
the agent's `Read` tool. The complete fix lives in Sandbox Agent —
brig ships a `safe_open(cell, relpath)` primitive that walks path
components with `O_NOFOLLOW`; SA needs to adopt it. Until that lands,
the application-side defense here is the consumer's first layer; a
mount-side `nosymfollow` (podman 5.x) closes the bug class entirely.

### Where Sandbox Agent runs (isolation comes from the substrate)

Rivet's Sandbox Agent is a control plane, not a sandbox. It's a
15 MB Rust binary that gives a uniform HTTP/ACP API over claude-code /
codex / opencode / cursor / amp / pi. **Isolation comes from wherever
you run the binary** — SA itself doesn't add it. aitelier supports
three deployment modes via `[sandbox_agent] mode`:

```toml
[sandbox_agent]
mode = "host"    # bare host install; no isolation. Default. Dev only.
# mode = "docker"  # SA inside the docker-compose `sa` profile container.
# mode = "remote"  # SA elsewhere; set base_url + token.
```

- **`mode = "host"`** — `scripts/start.sh` installs the SA binary
  with `curl ... install.sh | sh` and runs it as your user. The agent
  inherits your user's full host permissions. Fine for personal dev,
  unsafe for any deployment that exposes /v1/* to untrusted callers.

- **`mode = "docker"`** — `start.sh` flips on the compose `sa` profile
  (`docker/sandbox-agent.Dockerfile`). SA runs in an Alpine container
  with credentials mounted read-only; the agent inherits the
  container's permissions. For strongest isolation, use Docker Desktop
  Sandboxes (Docker Desktop 4.60+ runs each container in a microVM —
  hard hypervisor boundary; the agent can't see host paths at all).

- **`mode = "remote"`** — aitelier connects to SA running elsewhere.
  Set `base_url` to the remote URL and `token` to the auth header in
  `aitelier.secrets.toml`. Common targets: E2B, Daytona, Modal, Vercel
  Sandboxes, Cloudflare Containers, or a brig cell. `start.sh` detects
  non-localhost URLs and skips the local install.

For **brig-cell deployments**, the recommended shape is the cell
itself as the sandbox — aitelier + SA both run inside one cell with
`mode = "host"` (within the cell). The cell's podman boundary +
Warden network policy provide isolation. A sample
`docs/deploy/sandbox-agent.cell.yaml` shows the layout, including
workspace mount declarations, ingress on :7777, and the recommended
`policy.allow` list.

### Test coverage for deployment modes

Three tiers, ordered by cost:

1. **Shape validation** (`core/tests/test_deploy_samples.py`) — runs
   on every CI commit. Parses `docs/deploy/sandbox-agent.cell.yaml`,
   checks required brig keys (`name`, `image`, `command`, `network`,
   `policy.allow`, `ingress.port == 7777`, …), validates
   `docker/sandbox-agent.Dockerfile` mentions the Rivet install URL +
   `EXPOSE 2468`, and asserts the compose `sa` profile is opt-in.
   ~10 ms; pure file inspection.

2. **Docker image build smoke** (`make test-docker-build`, also a CI
   job) — runs `docker compose --profile sa build sandbox-agent`
   without starting the container. ~30 s on CI. Catches: stale Rivet
   install URL, base-image regression, apk dependency drift. Skips
   cleanly if `docker` isn't on PATH.

3. **Full e2e against Docker SA** (`./scripts/test-docker-mode.sh` or
   `make test-docker-mode-e2e`) — **destructive, manual only**.
   Stops your running aitelier + host SA, swaps `aitelier.toml` to
   `mode = "docker"`, builds the SA image, brings the compose `sa`
   profile up, and runs the full live test suite against it. Restores
   the original config + restarts in the original mode on exit.
   Requires Docker + real LLM credentials. Not in CI.

**Brig-mode e2e is intentionally not in this repo's CI.** Brig
isn't on PyPI / homebrew / a CI-installable artifact registry; setting
it up in GitHub Actions is impractical. The user-side integration
testing (your hermes-in-brig and dispatcher-in-brig deployments
talking to an aitelier cell) IS the brig e2e suite — that signal
lives in those projects' CI, not here.

## Cost tracking

LLM-path calls go through LiteLLM, which reports per-call cost in the
`x-litellm-response-cost` response header. aitelier reads that header and
persists the value on `runs.cost_usd` (using LiteLLM's own number, not a
homemade pricing table). Streaming has no usable cost header, so streamed
calls leave `cost_usd: null`.

Agent-path runs always have `cost_usd: null` by design: agent LLM calls
happen inside the Sandbox Agent process and go directly to Anthropic /
OpenAI, bypassing LiteLLM.

Token usage (`runs.input_tokens` / `output_tokens` / `total_tokens`) is
captured per path and normalized — the LLM/embed paths report OpenAI-shape
`prompt_tokens`/`completion_tokens`; the agent path reports
`input_tokens`/`output_tokens`. **`null` means the backend reported no
usage** (some agent backends, or a failed/in-flight run) — distinct from a
genuine `0`, so dashboards can tell "unknown" from "zero".

## Observability — OpenTelemetry export

aitelier can emit an OTLP span *tree* per run, tagged with the
[GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
(`gen_ai.system`, `gen_ai.request.*`, `gen_ai.response.*`,
`gen_ai.usage.*`). The **trace id is the run id** (a 32-hex W3C value), so a
run is addressable by id in any backend. A root span carries the
request/response attributes; each agent tool call becomes a child
`execute_tool` span (reconstructed from the durable `run_events` log at
finalize, so it's off the request hot path). Point any OTLP-speaking
backend at the configured endpoint and you'll see the full agent trace —
model, token counts, finish reason, error type, and per-tool timing.

The export is **off by default** and the OTel SDK is an optional
dependency — a default install pays no import cost.

**Install:**

```bash
uv pip install 'aitelier[otel]'
```

**Enable** in `aitelier.toml`:

```toml
[otel]
enabled         = true
endpoint        = "http://localhost:4317"   # OTLP collector
protocol        = "grpc"                    # "grpc" (4317) or "http" (4318)
insecure        = true                      # grpc only; http infers from URL scheme
service_name    = "aitelier"
capture_content = false                     # true → emit message bodies as span events
```

**What gets exported** (root span per run, + an `execute_tool` child span per agent tool call):

| Attribute | Source |
|---|---|
| `gen_ai.operation.name` | `chat` or `embeddings` |
| `gen_ai.system` | `anthropic`, `openai`, `gemini`, `ollama`, or `aitelier.agent.<backend>` |
| `gen_ai.request.model` | request `model` field |
| `gen_ai.request.max_tokens` | request `max_completion_tokens` ?? `max_tokens` |
| `gen_ai.request.temperature` | request `temperature` |
| `gen_ai.request.top_p` | request `top_p` |
| `gen_ai.request.stop_sequences` | request `stop` (string or array) |
| `gen_ai.response.id` | response `id` |
| `gen_ai.response.model` | response `model` |
| `gen_ai.response.finish_reasons` | deduplicated tuple across choices |
| `gen_ai.usage.input_tokens` | `usage.prompt_tokens` or `usage.input_tokens` |
| `gen_ai.usage.output_tokens` | `usage.completion_tokens` or `usage.output_tokens` |

**Error spans** carry `error.type` + `error.message` and a non-OK status
so trace backends can filter for failed inference without parsing logs.

**Content opt-in.** Messages are *not* exported unless `capture_content
= true`. With it on, each `system` / `user` / `assistant` / `tool`
message is emitted as a span event (`gen_ai.{role}.message`) following
the GenAI convention's content-as-events model. Enable only when you've
audited the destination collector for retention and access — message
bodies routinely contain customer data, secrets, and PII.

**One span per request, not per agent turn.** Agent-path runs collapse
the entire run (all internal turns + tool calls) into a single span.
Per-turn detail still lives in `run_events` (and `/v1/runs/{id}/events`)
— OTel sees the outer shape only.

**Graceful degradation.** If `[otel] enabled = true` but the SDK isn't
installed, you get one WARN log at startup and inference keeps working
— the instrumentation call sites are no-ops when the tracer wasn't
initialized. No request-path cost when disabled.

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
| `stream_run_events` / `streamRunEvents` | `GET /v1/runs/{id}/events/stream` (SSE) |
| `wait_for_run` / `waitForRun` | `POST /v1/runs/{id}/wait` |
| `add_run_score` / `addRunScore` | `POST /v1/runs/{id}/scores` |
| `list_run_scores` / `listRunScores` | `GET /v1/runs/{id}/scores` |
| `export_runs` / `exportRuns` | `GET /v1/runs/export` (NDJSON) |
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
