# aitelier examples

Runnable recipes showing the patterns from `docs/INTEGRATION.md` end to end.
Each example is self-contained and assumes an aitelier instance running at
`http://localhost:7777`. Start it with `make start` from the repo root.

| File | Pattern | What it shows |
|---|---|---|
| `01_fanout_merge.py` | Fan-out / merge | Submit N agent runs in parallel, await all, summarize with a final LLM call. Uses `submit_run` + `wait_for_run`. |
| `02_mcp_orchestrator.py` | Agent dispatches subagents | A parent agent loaded with `aitelier-mcp` decides how to fork children — aitelier is the substrate, the parent is the conductor. |
| `03_scheduled_audit.py` | Recurring background job | `POST /v1/schedules` to fire an audit every N minutes with a webhook callback. |
| `04_webhook_receiver.py` | Verify incoming webhooks | Minimal FastAPI receiver using `verify_webhook_bearer` to authenticate aitelier's Bearer-token deliveries. |

Run any example:

```bash
cd examples
uv run python 01_fanout_merge.py
```

Each file is ≤100 lines and meant to be copied + modified, not imported.
