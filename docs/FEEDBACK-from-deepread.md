# Feedback from deepread

> From **deepread** (`~/projects/the-well/deepread`), which uses aitelier for
> `complete`, `embed`, and `runAgent`. Contract on the deepread side:
> `deepread/docs/AITELIER_INTERFACE.md`.

A 2026-06-09 session debugging empty/failed local-model summaries raised the
items below. **All are resolved** — most were already-supported aitelier
features, one was deepread's own bug. Kept as a compact record; no open asks.

| Item | Resolution |
|------|------------|
| Reasoning models emit to `reasoning_content`, `content` empty | Pass `reasoning_effort:"minimal"` → Ollama `think:false`. Already supported. |
| Structured output unreliable in JSON *mode* | `response_format:{type:"json_schema"}` already maps to Ollama native constrained decoding. Already supported. |
| Local `complete()` fails on large payloads | Typed `504 Timeout`; raise `timeout` and set `num_ctx`. Already supported. |
| Persistent local `complete()` `400` | **deepread bug** — sent the model with its `llm:` kind-prefix (`llm:ollama/gemma3:12b`), routing to LiteLLM instead of the native Ollama adapter. Fixed in deepread. |
| Trace a consumer failure into the inference call | `GET /v1/runs/{id}` carries `request_body`, `error_msg`, tokens, latency; correlate via `aitelier_run_id` / `X-Correlation-Id`. Already supported. |
| Upstream provider error body scrubbed to status-only | aitelier now surfaces the provider body in `error_msg` (entropy + regex secret scrubber, `scrub_upstream_body`); full body stays in the WARNING log. Shipped this round. |

**Note for deepread (consumer side):** record `aitelier_run_id` when present,
else the `X-Correlation-Id` response header, and pivot into `GET /v1/runs/{id}`
to debug a call.

**Working well:** `embed` (nomic-embed-text), `runAgent` (Rivet sandbox), and
`completeStream` are all reliable and back deepread's retrieval, clustering,
chat, and "Ask your corpus" features.
