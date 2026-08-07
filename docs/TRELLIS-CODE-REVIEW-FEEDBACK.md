# Trellis code-review integration feedback

Date: 2026-08-07

This feedback comes from repeated local Trellis code-review workflows using Aitelier's
durable agent control plane. The workflow used `agent:codex/gpt-5.5`, Codex CLI 0.132.0,
Sandbox Agent 0.4.2 in host mode, read-only approval, async `POST /v1/runs`, repeated
`POST /v1/runs/{id}/wait`, and trace tag `trellis.code-review/v1`.

The integration's core shape worked well: one acknowledged native run ID survived client
wait reconnects, Postgres retained full run/event history, parallel runs remained separately
addressable, and the records were still queryable through another Aitelier service after the
submitting service stopped. Trellis now handles its own deterministic scope admission and
awaits all started parallel branches. The remaining items below are Aitelier/Sandbox Agent
concerns rather than requests for Aitelier to own application workflow control flow.

## P0: credential inspection reveals the Claude OAuth token by default

While diagnosing the Claude backend, this command printed the raw OAuth token even though no
explicit reveal option was supplied:

```sh
sandbox-agent credentials extract --agent claude
```

The token value is intentionally not reproduced here. This is a secret-disclosure defect in
Sandbox Agent's credential CLI, not a Trellis workflow issue. The exposed credential must be
rotated.

Acceptance criteria:

- Default credential inspection returns only source, authentication type, and a redacted
  fingerprint or status.
- Secret material is emitted only behind a clearly named, explicit reveal option.
- Regression tests cover every supported agent credential, and neither normal logs nor error
  messages contain the secret.

Implementation update: the local Sandbox Agent worktree now makes every default
`credentials extract` path metadata-only, including the previously unsafe
`--agent` and `--provider` branches. Raw selected values require `--reveal`, and
regression tests assert that default summaries contain no secret material.

## Aitelier implementation status

The Aitelier-owned follow-up is now implemented in this worktree:

- Async acceptance commits the pending run before returning its id.
- Cancellation settles the owned ACP prompt task, closes the session, and deletes the
  Sandbox Agent proxy; deployment defaults also keep SA's internal ACP timeout from
  preempting Aitelier's deadline.
- Terminal aggregates count `tool_call`, not matching `tool_result`, events.
- Durable structured results use an ambiguity-rejecting parser and validate requested JSON
  Schema, finalizing nonconformance as `SchemaViolation`.
- `GET /v1/models?agent_backend=<id>` probes only the selected backend.
- Agent discovery records both the native agent and ACP adapter versions; the
  Brig Codex pairing is updated to CLI `0.137.0` plus `codex-acp` `0.16.0`.
- Model selection follows the backend-advertised config option; legacy
  `session/set_model` is used only for adapters that do not advertise one.
- A timeout after answer text was emitted retains that text and records
  `MissingTerminalEvent`, separating a terminal-protocol failure from ordinary
  no-output latency.
- ACP JSON-RPC error data is scrubbed and preserved in API failures, and the
  terminal doctor probe prints that sanitized response instead of reducing all
  HTTP failures to a status line.
- ACP-reported token and reasoning usage is normalized into durable/OpenAI
  usage fields. A local `codex-acp` change now forwards Codex core's exact
  cumulative counters through ACP `PromptResponse.usage`; the local Brig build
  workflow deploys that worktree directly, without requiring a registry release.

The hard work-budget/synthesis contract and any missing reasoning-option facts
remain backend/bridge capabilities. Until a backend implements the former,
`/v1/models` explicitly reports `aitelier_capabilities.hardToolBudget: false`.
Aitelier maps verifiable signals when Sandbox Agent/the selected ACP adapter
exposes them; it does not estimate token spend or claim a hard limit from
prompt-only tool guidance.

## P0: enforceable agent work budget with a synthesis reserve

Prompt-only tool limits are not reliable. The specialists were explicitly instructed to use
at most ten tool invocations, but Aitelier retained more:

- `69495a3907fa5efc161ae7449440b647`: 15 `tool_call` events, then timeout at 360,097 ms.
- `4ea6621848a9b6355d12ebd33bb133f6`: 14 `tool_call` events, then timeout at 360,101 ms.
- `d5ea2e8c70ae018f86ec10b1af6390b1`: 17 `tool_call` events, success at 118,043 ms.
- `4a70c5ebed864cadb2d9f1926f17ceb7`: 15 `tool_call` events, success at 121,609 ms.

The timed-out pair reviewed an application-preflighted scope of six files and 547 changed
lines. The successful pair reviewed one file and nine changed lines. A raw wall-clock timeout
can discard a nearly complete review and gives consumers no way to reserve time for the final
structured response.

Suggested contract:

- Add an advertised agent capability and request field such as `max_tool_calls` or a more
  general work budget that Sandbox Agent/ACP can actually enforce.
- On budget exhaustion, deny further tool work and give the agent one bounded synthesis turn.
- Distinguish `budget_exhausted` with a final result from `timeout` with no result.
- Retain the configured limit, observed count, and exhaustion reason in the run record.

Acceptance criteria:

- An agent requested with a ten-call limit never produces an eleventh `tool_call` event.
- It gets a bounded opportunity to return its requested schema after the tenth result.
- `/v1/models` advertises whether the selected agent backend supports the hard limit.

## P0: timeout/cancellation leaves unobserved ACP task failures

When the two specialist runs above timed out together, the Aitelier service logged two
unhandled task failures:

```text
asyncio: Task exception was never retrieved
... AcpClient.call() ... exception=ReadError('')
```

Both stacks ended in `httpx.ReadError` from `providers/acp_transport.py`. The durable run rows
correctly reached `Timeout`, but pending ACP tasks were not consumed cleanly.

Acceptance criteria:

- Timeout and cancellation cancel/await every owned ACP task and close the session best-effort.
- Expected transport fallout from cancellation does not produce an unhandled asyncio error.
- The durable terminal run state remains the authoritative outcome even if cleanup fails.

## P1: `tool_call_count` counts tool results as calls

The aggregate field does not match the documented meaning "inner-agent tools that fired":

- `d5ea2e8c70ae018f86ec10b1af6390b1` reports `tool_call_count: 34`, while its event stream has
  17 `tool_call` and 17 `tool_result` events.
- `4a70c5ebed864cadb2d9f1926f17ceb7` reports `tool_call_count: 30`, while its event stream has
  15 `tool_call` and 15 `tool_result` events.

Count calls, not both sides of the call/result pair. Add a regression test that compares the
terminal aggregate with `run_events WHERE kind = 'tool_call'`.

## P1: durable agent runs do not apply documented structured-output parsing

Run `d50bf03c9765a30c6951d6175cb438f7` completed successfully with status prose followed by
one valid JSON object. Its durable result retained `parsed: null`. The integration docs say
that fenced or prose-prefixed JSON is parsed into `aitelier_parsed`; the async durable agent
path should provide equivalent behavior.

Suggested behavior:

- Apply the same unique structured-value parser to terminal async agent results.
- Populate `result.parsed` when exactly one value is recoverable.
- If `json_schema` was requested, validate the parsed value and record a typed conformance
  failure rather than returning provider success with nonconforming output.
- Reject ambiguity; never choose between multiple JSON objects.

## P1: agent compatibility and terminal-turn probing

Codex CLI 0.146.1 emitted useful output through the host ACP path but never produced the
terminal event Aitelier needed. Run `c4358230830baff1735aa7a73edae716` was manually cancelled
after 398,999 ms. Pinning Codex CLI 0.132.0 restored terminal completion, but consumers should
not need trial-and-error version pinning.

Suggested improvements:

- Publish or probe a supported Codex/Sandbox Agent compatibility matrix.
- Make `doctor` perform a cheap terminal-turn handshake, not only process/capability discovery.
- If an agent emits final content but never terminates, retain a diagnostic that distinguishes
  missing terminal protocol from ordinary model latency.

The same class of readiness failure was reproduced with Claude. Discovery advertised
`agent:claude`, several models, reasoning levels, and approval modes, and session creation
succeeded, but every actual prompt failed in about 2.2–2.6 seconds before a tool call with a
long-context usage-credit error. This included a one-turn Haiku probe with no tools and no JSON
Schema (`7777f7efa8ef918b07410803d2c6cdec`). Direct Claude CLI 2.1.221 worked with the same
logged-in account. The managed route used `@agentclientprotocol/claude-agent-acp` 0.33.1 with
Claude Agent SDK/Claude Code 2.1.132, while Aitelier's integration source expects at least 0.36.

Backend discovery should therefore distinguish syntactic availability from execution readiness.
For the selected agent, `doctor` or a strict readiness probe should perform a cheap terminal turn,
report the adapter and embedded agent versions, and explain known incompatibilities. Process
startup, capability advertisement, and `session/new` alone must not be reported as a usable
backend.

## P1: usage and reasoning capability gaps

Every successful Codex agent run in these trials returned `usage: null`, including all four
stages of Trellis Run `74d45a05-0c44-47e8-a22b-c49d33cc6512`. The backend also advertised no
reasoning levels, forcing consumers to omit `reasoning_effort` and record `backend-default`.

`null` is preferable to invented usage, and Trellis correctly diagnoses missing coverage.
If Codex ACP exposes actual input/output/inner token facts, Aitelier should map them. Context
window `used` samples must not be relabeled as spend. Reasoning levels should be advertised
only when the backend can prove it applies them.

Implementation update: Codex core `0.137.0` already emits exact input, cached-input,
output, reasoning-output, and total counters. The local `codex-acp` worktree now
maps those counters to ACP's native `PromptResponse.usage`, and Aitelier preserves
them (including OpenAI `completion_tokens_details.reasoning_tokens`). This is not
an extrapolation from context-window occupancy. The deployed adapter must include
that bridge change; `make brig-local` supplies it directly from the local worktree.

## P2: model discovery latency is dominated by unrelated backends

`GET /v1/models` took 10,084 ms in the latest run because an unavailable Cursor probe reached
its timeout, even though Codex was healthy. Deployment checks for a selected backend should not
regularly pay the slowest unrelated probe cost.

Possible shapes:

- Add a backend-filtered discovery/probe endpoint.
- Cache per-backend probe results independently and longer than the whole response cache.
- Return known capability state plus per-backend staleness/errors without blocking on every
  configured backend.

## Reproduction and retained evidence

- Successful revised workflow correlation: Trellis Run
  `74d45a05-0c44-47e8-a22b-c49d33cc6512`.
- Timed-out revised workflow correlation: Trellis Run
  `42d8faf1-352a-4513-8b68-f824f4098cca`.
- Aitelier trace tag: `trellis.code-review/v1`.
- Full evidence is available through `/v1/runs/{id}` and `/v1/runs/{id}/events` in the shared
  development Postgres store.

The Trellis-side fixes are in commit `3e38055`: deterministic preflight rejects oversized
subjects before provider work, triage must use the verified file set, and parallel specialist
failure is propagated only after both already-started branches reach terminal state.
