# aitelier-client (Python)

Async Python client for [aitelier](https://github.com/d0cd/aitelier) — a
personal AI runtime that speaks **OpenAI shape for inference** and an
**aitelier-native control plane** for durable runs, traces, schedules,
and webhook delivery.

## Install

```bash
pip install aitelier-client                # control plane only
pip install "aitelier-client[openai]"      # + OpenAI SDK for inference
```

## Two layers, one client

```python
from aitelier_client import Aitelier

ait = Aitelier(base_url="http://localhost:7777", api_key="...")

# Inference — pass-through to the OpenAI SDK.
openai = ait.openai()
resp = await openai.chat.completions.create(
    model="agent:claude",
    messages=[{"role": "user", "content": "audit this repo"}],
    extra_body={"aitelier": {"workspace": "/path/to/repo"}},
)

# Control plane — aitelier methods on the client itself.
runs = await ait.list_runs(trace_tag="audit", limit=20)
traces = await ait.recent_traces(status="error")
```

`Aitelier.openai()` returns a real `openai.AsyncOpenAI` pointed at
aitelier. Streaming, retries, tool semantics, structured outputs — all
OpenAI SDK territory. Install the `[openai]` extra to enable.

## Async agent runs

For long-running agent jobs that should outlive the HTTP request, use
the control plane:

```python
submission = await ait.submit_run(
    model="agent:claude",
    messages=[{"role": "user", "content": "audit /workspace"}],
    aitelier_opts={"workspace": "/path/to/repo", "trace_tag": "audit-2026"},
    webhook_url="https://my.app/webhooks/aitelier",
)
run = await ait.wait_for_run(submission["run_id"], timeout=300)
print(run.result["content"])
```

`wait_for_run` does server-side polling — convenient for submit-and-await
when you don't have a webhook receiver. With one, the terminal payload
lands on `webhook_url` automatically.

## Webhook verification

```python
from aitelier_client import verify_webhook_signature

@app.post("/webhooks/aitelier")
async def receive(request: Request):
    body = await request.body()   # raw bytes — not re-serialized JSON
    sig = request.headers.get("X-Aitelier-Signature")
    if not verify_webhook_signature(body, sig, MY_SECRET):
        raise HTTPException(401)
    ...
```

Uses `hmac.compare_digest` under the hood. Don't roll your own with `==`.

## Configuration

The client reads `[service] host`/`port` from
`~/.config/aitelier/config.toml` if no `base_url` is passed — handy when
your shell already runs `aitelier doctor` / `make start` on the
configured host.

## Examples

End-to-end recipes live in the [`examples/`](../../examples) directory:

- `01_fanout_merge.py` — parallel fan-out + synthesis
- `02_mcp_orchestrator.py` — agent dispatches subagents via `aitelier-mcp`
- `03_scheduled_audit.py` — recurring background job with webhook callback
- `04_webhook_receiver.py` — verify incoming webhooks

See also [`docs/INTEGRATION.md`](../../docs/INTEGRATION.md) for the full
integration guide (error handling, multi-agent workflows, hosted mode).
