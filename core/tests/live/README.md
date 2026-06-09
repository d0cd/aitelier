# Live integration tests

Tests in this directory hit a **running aitelier service** end-to-end:
real LiteLLM, real Sandbox Agent, real Postgres. They catch the class of
bugs unit tests can't — wire-format mismatches with upstream, agent
notification shapes, model availability, SDK round-trips against the
actual API.

## Running

Default `pytest` invocations skip everything here (gated on the
`AITELIER_LIVE_URL` env var). To run:

```bash
make start              # bring up the full stack
make test-live          # or: AITELIER_LIVE_URL=http://localhost:7777 pytest -m live
```

If `AITELIER_LIVE_URL` is unset, every test in this directory is skipped
at collection time. If it's set but the URL isn't reachable, tests fail
fast with a clear error.

## Test budget

Each test should be:
- **Cheap** — uses `claude-haiku` for completions, `nomic-embed-text` for
  embeddings, the `mock` agent backend for agent flows. Whole suite
  < $0.10 / run, < 60s wall clock.
- **Hermetic** — no shared state between tests. Each creates a unique
  `trace_tag` so its runs can be queried back without colliding.
- **Self-contained** — no external MCP servers, no on-disk fixtures
  outside this directory.

## What's covered

- `/v1/chat/completions` (LLM path) sync + stream round-trip
- `/v1/embeddings`
- `/v1/chat/completions` (agent path) sync against the `mock` SA backend
- `/v1/chat/completions` (agent path) `stream: true` SSE event ordering
- `/v1/runs` async submission + webhook callback
- `/v1/schedules` CRUD
- `/v1/runs` query + `/v1/runs/{id}/events` follow-up

## What's NOT covered (intentionally)

- Long-running real agent flows (claude/codex with real prompts) — too
  slow and expensive for CI. Run those manually.
- Failure injection (LiteLLM down, SA down) — covered by unit tests
  with mocks; live tests assume the stack is healthy.
- Multi-process isolation — aitelier is single-process for now.
