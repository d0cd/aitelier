# aitelier

Personal AI runtime. One local HTTP service that's **OpenAI-compatible for
inference** and exposes an **aitelier-native control plane** for durable run
state, traces, schedules, async agent submissions, and observability.

Backed by LiteLLM (LLM routing), Rivet's Sandbox Agent (isolated coding agents
behind ACP), and Postgres (durable run/event/schedule/webhook state).

Built to be the one AI dev stack so projects don't each re-integrate
Claude / OpenAI / Ollama themselves — point any OpenAI SDK at aitelier and
go.

## Quick start

```bash
claude login              # once — OAuth for Anthropic via Claude Code
make install              # one-time deps (uv + pnpm)
make start                # Postgres + LiteLLM + Sandbox Agent + aitelier service
```

The service is now at `http://localhost:7777`. Health check:
`curl localhost:7777/v1/health`.

```python
from aitelier_client import Aitelier

ait = Aitelier()
openai = ait.openai()   # preconfigured OpenAI client

# LLM call
resp = await openai.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Summarize today's news."}],
)

# Agent call — model prefix flips the routing
resp = await openai.chat.completions.create(
    model="agent:claude/claude-sonnet-4-5",
    messages=[{"role": "user", "content": "Audit this repo for security issues."}],
    extra_body={"aitelier": {"workspace": "/path/to/repo"}},
)
```

## Day-to-day

| Command | What |
|---|---|
| `make start` | Bring up everything. Idempotent. |
| `make stop` | Bring everything down. Postgres data survives. |
| `make restart` | Restart just the aitelier service (leaves infra hot). Use after editing `core/src/aitelier/`. |
| `make logs` | Tail service + sandbox-agent logs. |
| `make status` | What's running, where logs are, are dependencies healthy. |
| `make doctor` | Preflight checks. Run when `make start` fails with a confusing error. |
| `make test` | Python + TS test suites. |
| `make reset` | **Destructive.** Stops everything *and* wipes the Postgres volume. Asks before doing it. |

## When something's wrong

- `make start` fails → `make doctor` names the cause (port conflict, missing creds, Docker down)
- Service won't respond → `make logs` shows the last output. Files live at:
  - `runs/logs/aitelier.log` (FastAPI service)
  - `runs/.sandbox-agent.log` (Rivet Sandbox Agent)
- Want a clean slate → `make reset`

## Configuration

All TOML, zero env-var reads in app code. Layered:

1. `aitelier.toml` (repo-local) or `~/.config/aitelier/config.toml` (global) — primary
2. `aitelier.secrets.toml` next to it (gitignored) — API keys, tokens, webhook secrets
3. `runs/.session.toml` (gitignored, ephemeral) — written by `scripts/start.sh`

See `aitelier.toml.example` and `aitelier.secrets.toml.example` for the
full surface; `docs/INTEGRATION.md` for the rationale.

## Deeper docs

- [`docs/INTEGRATION.md`](docs/INTEGRATION.md) — **consumer guide**. OpenAI-shape
  inference + control plane. SDK usage (Python + TS). Auth, security, retries,
  idempotency, correlation IDs, cost tracking.
- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture + tradeoffs.
- [`docs/PLAN.md`](docs/PLAN.md) — what's built, directions worth exploring, what's deliberately out of scope.
- [`docs/deploy/`](docs/deploy/) — sample deployment configs (brig cell yaml, Docker compose profile).
- [`examples/`](examples/) — four runnable recipes (fan-out, MCP orchestrator, scheduled audit, webhook receiver).
- [`CHANGELOG.md`](CHANGELOG.md) — release notes.
- [`CLAUDE.md`](CLAUDE.md) — orientation for AI agents working in this repo.

## Project layout

```
core/                   Python core (FastAPI service, providers, storage)
sdks/python/            Python SDK (`aitelier_client`)
sdks/python-mcp/        MCP server exposing the control plane (`aitelier-mcp`)
sdks/typescript/        TypeScript SDK (`aitelier`)
schemas/v1/             JSON Schemas — control-plane wire format
examples/               Runnable recipes against a live aitelier
docker/                 docker-compose (Postgres, LiteLLM, optional Ollama)
scripts/                start / stop / status / doctor / release
docs/                   design, plan, integration guide
runs/                   gitignored — per-run agent artifacts + logs
```

## License

Personal use. No published SDK, no API stability guarantees for outside consumers.
