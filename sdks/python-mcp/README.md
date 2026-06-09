# aitelier-mcp

A small [Model Context Protocol](https://modelcontextprotocol.io/) server
that exposes [aitelier](https://github.com/d0cd/aitelier)'s control plane
as MCP tools. Load it into an agent's `mcp_servers` block and the
agent's tool-use loop gains five new tools:

- `submit_run` — dispatch a child agent run
- `get_run` — fetch a single run's state
- `list_runs` — filter by `parent_run_id`, `trace_tag`, `state`
- `list_run_events` — full durable event timeline
- `cancel_run` — cancel an in-flight run

Aitelier becomes the substrate; the **parent agent** is the conductor.
Whatever orchestration shape it picks turn-by-turn — fan-out, sequential
handoff, retry-on-fail — happens inside its reasoning loop, not in
hand-rolled Python.

## Install

```bash
pipx install aitelier-mcp      # or: uv tool install aitelier-mcp
```

This puts the `aitelier-mcp` executable on PATH so agents can spawn it
over stdio.

## Wire into an agent

```jsonc
// Parent run submitted to aitelier:
{
  "model": "agent:claude",
  "messages": [
    {"role": "user",
     "content": "Audit security + deps + docstrings in parallel; summarize."}
  ],
  "aitelier": {
    "mcp_servers": [
      {"name": "aitelier", "transport": "stdio", "command": "aitelier-mcp"}
    ],
    "max_turns": 50
  }
}
```

The inner agent now sees `submit_run` / `list_runs` / etc. alongside its
other tools. Each call is a thin wrapper around the matching aitelier
HTTP endpoint — auth, SSRF guards, idempotency, and observability all
still apply because `aitelier-mcp` is just another consumer.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AITELIER_URL` | `http://localhost:7777` | Base URL of the aitelier service |
| `AITELIER_API_KEY` | unset | Bearer token (hosted-mode auth) |
| `AITELIER_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` for out-of-process testing |

## See also

- [`examples/02_mcp_orchestrator.py`](../../examples/02_mcp_orchestrator.py) — runnable demo
- [`docs/INTEGRATION.md`](../../docs/INTEGRATION.md) → "Multi-agent workflows"
- [`aitelier-client`](../python) — the underlying Python SDK
