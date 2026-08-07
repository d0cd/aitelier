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

## P1: current Codex compatibility and terminal-event probing

Codex CLI 0.146.1 emitted useful output through the host ACP path but never produced the
terminal event Aitelier needed. Run `c4358230830baff1735aa7a73edae716` was manually cancelled
after 398,999 ms. Pinning Codex CLI 0.132.0 restored terminal completion, but consumers should
not need trial-and-error version pinning.

Suggested improvements:

- Publish or probe a supported Codex/Sandbox Agent compatibility matrix.
- Make `doctor` perform a cheap terminal-turn handshake, not only process/capability discovery.
- If an agent emits final content but never terminates, retain a diagnostic that distinguishes
  missing terminal protocol from ordinary model latency.

## P1: usage and reasoning capability gaps

Every successful Codex agent run in these trials returned `usage: null`, including all four
stages of Trellis Run `74d45a05-0c44-47e8-a22b-c49d33cc6512`. The backend also advertised no
reasoning levels, forcing consumers to omit `reasoning_effort` and record `backend-default`.

`null` is preferable to invented usage, and Trellis correctly diagnoses missing coverage.
If Codex ACP exposes actual input/output/inner token facts, Aitelier should map them. Context
window `used` samples must not be relabeled as spend. Reasoning levels should be advertised
only when the backend can prove it applies them.

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
