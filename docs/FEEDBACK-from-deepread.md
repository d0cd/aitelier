# Feedback from deepread

> Consumer feedback from **deepread** (`~/projects/the-well/deepread`), which
> uses aitelier for `complete`, `embed`, and `runAgent`. Opened 2026-06-09
> while debugging why local-model summarization produced empty `doc_summaries`.
> Evidence is from direct probes against `localhost:7777/v1/chat/completions`.
>
> Contract reference on the deepread side: `deepread/docs/AITELIER_INTERFACE.md`.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Reasoning models emit to `reasoning_content`, `content` empty | ✅ Resolved — `reasoning_effort:"minimal"` |
| 2 | Local `complete()` fails on large payloads | ✅ Resolved — typed `Timeout` + new `num_ctx`; raise `timeout` |
| 3 | Structured output via JSON Schema constrained decoding | ✅ Already implemented — send `json_schema` |
| 4 | Observability: trace a consumer failure into the inference call | ✅ Mostly shipped — `GET /v1/runs/{runId}` already carries the full trace (request_body, error, tokens, latency); record runId (or the `X-Correlation-Id` header for pre-dispatch rejections). Partial: upstream error *body* is scrubbed to status-only. |

> **Routing correction (from aitelier):** `local` and `ollama/*` do **not** go
> through LiteLLM. aitelier has its own Ollama adapter
> (`core/src/aitelier/providers/ollama.py`) because LiteLLM's adapter drops
> `message.thinking`. So this doc's original LiteLLM framing (issues #27956 /
> #18922 and "LiteLLM 500 on payload size") does not apply to the `ollama/*`
> path — that path is all aitelier + Ollama. Items below reflect the corrected
> understanding.

---

## 1. Reasoning models: answer lands in `reasoning_content`, `content` empty ✅

**Issue.** Ollama "thinking" models (qwen3, deepseek-r1, gpt-oss) return
`content: ""` with the answer routed to `reasoning_content`. A caller reading
`content` sees empty output and records a failed summary — the mechanism
behind repeated "N empty `doc_summaries`" incidents.

**Resolution (aitelier).** Pass **`reasoning_effort: "minimal"`** — a
first-class OpenAI field (so `extra="forbid"` does not block it). aitelier maps
it to Ollama `think:false` and `content` is populated. Verified:

```
ollama/qwen3:8b + reasoning_effort:"minimal"  →  content:"OK", reasoning_content:""   ✓
ollama/qwen3:8b  (no reasoning_effort)         →  thinking ON; under a tight
                                                  max_tokens the budget burns on
                                                  hidden CoT and content can be ""
```

A true-empty signal also already exists: **`aitelier_exit:"empty"`** on the
choice (fires only when neither content nor reasoning_content nor tool_calls
landed). See INTEGRATION.md → "Reasoning models".

**deepread action.** Send `reasoning_effort:"minimal"` on summarize/classify
completions. (Also use non-reasoning models where possible.)

---

## 2. Local `complete()` fails on large payloads ✅

**Issue.** A small `complete()` returns valid JSON in seconds; the full
summarize payload (long document + system/priming context + a few thousand
output tokens) failed.

**Resolution (aitelier).** Not a generic 500 — it surfaces as a **typed `504`
`Timeout`** (`"Upstream transport failure (ReadTimeout)"`). Two
consumer-controllable causes:

1. **Slow generation on a big context → timeout.** Raise the per-request
   **`timeout`** field (seconds); default is 60.
2. **Silent truncation.** Ollama defaults `num_ctx` to a small window and drops
   input past it. aitelier shipped a sanctioned **`num_ctx`** field (Ollama
   routes only, ignored elsewhere). Verified: a ~84k-char request with
   `num_ctx:32768` + `timeout:240` completes (~53s).

**deepread action.** Set `num_ctx` and a generous `timeout` on long documents.

> **Open follow-up from deepread (2026-06-09):** with `reasoning_effort`,
> `json_schema`, `num_ctx:32768`, and `timeout:240` all set, a *direct* HTTP
> probe to aitelier succeeds, but the summarize run through deepread's shim
> still fails (`no output (error)`) after minutes — consistent with a real
> timeout on slow generation, but not yet root-caused. This is the immediate
> motivation for item 4: we couldn't trace the failing call into aitelier.

---

## 3. Structured output via JSON Schema constrained decoding ✅

**Issue.** deepread was sending `response_format:{type:"json_object"}` (JSON
*mode*), which is prompt-only and unreliable on local models.

**Resolution (aitelier).** Already implemented: aitelier maps
`response_format:{type:"json_schema", json_schema:{schema}}` to Ollama's native
`format:<schema>` (XGrammar constrained decoding). Verified schema-valid JSON
against `ollama/gemma3:12b`. This is also the most robust mitigation for #1
(constrained decoding forces structured `content` regardless of thinking).

**deepread action.** Send the `json_schema` shape (the shim already emits it
via `toOpenAIResponseFormat`).

---

## 4. Observability: tracing a consumer failure into the inference call 🔲

**Issue (this session's real pain).** When summarize failed, deepread logged
`"no output (error)"` with no way to see *what aitelier actually sent to Ollama
or what came back*. Debugging took an hour because the two sides' logs
couldn't be joined, and aitelier's recent per-request traces weren't easily
discoverable (the `runs/` dir held only stale bringup logs).

**Concrete evidence (after deepread added correlation logging).** deepread now
logs the full `Result` on failure. A representative summarize failure:

```
errorType: "ProviderError"   errorMsg: "Upstream provider error (HTTP 400)"   runId: ""
```

Two things stand out: (a) it's a typed `HTTP 400` from the Ollama provider —
useful, deepread can act on it; but (b) **`runId` is empty on the error
response**, so there is no thread back into aitelier to see the exact request
that produced the 400. Direct probes with the same params (`json_schema`
incl. a complex nested schema, `reasoning_effort:"minimal"`, `num_ctx`) all
*succeed*, so the 400 comes from something in the real request that only
aitelier's record of the outbound Ollama call could reveal — which is exactly
what's missing.

**The principle (where the line should sit).** Each side should log only what
it can see, joined by a correlation ID:

- **aitelier owns the inference boundary** — it is the *only* layer that can see
  the exact request sent to the provider (incl. whether `num_ctx`/
  `reasoning_effort` arrived), the raw provider response, the typed error/
  timeout, latency, tokens, and routing. A consumer physically cannot observe
  this. As the central local service, aitelier should carry the heavier load
  here: one good, queryable trace store benefits every consumer.
- **deepread owns the business operation** — which document, which outcome
  (generated / deferred / failed), validation result, backlog counts, and its
  own model-choice / retry / fallback *decisions*.
- **The correlation ID is the contract.** aitelier already returns `runId` /
  `traceId` on every `Result`; deepread should record it on every outcome so a
  failure can be pivoted straight into aitelier's trace.

**Ask (aitelier).**
1. **Always return a non-empty `runId`/`traceId`, especially on errors** — the
   error path is precisely when the consumer most needs to trace inward, and
   that's where it's currently empty (see evidence above).
2. **Make every `complete()` / `ollama/*` call queryable by that ID**, with:
   the resolved provider + model, the request params actually sent to the
   provider (so a consumer can confirm `num_ctx`/`reasoning_effort`/`format`
   were applied and see the final prompt size), the raw provider error / finish
   reason / status (e.g. the body of that Ollama `400`), latency, and token
   counts. Ideally `GET /v1/traces/{runId}` or `aitelier traces show <runId>`.

This is the missing half of the contract — deepread can record the ID, but
only aitelier can make it resolve to "here's exactly what happened at the
provider." Had it existed today, root-causing the `HTTP 400` above would have
been one lookup instead of an afternoon of black-box probing.

**deepread action (in progress).** Include `runId` / `traceId` / `errorType` /
`errorMsg` (and the chosen model) in every summarize outcome log, so a failure
carries the thread back into aitelier. The data is already on `Result`; it was
simply being dropped.

**Response (aitelier, 2026-06-09).** Agreed on the principle — aitelier owns the
inference boundary. Most of this already exists; verified against live runs:

- **The queryable-by-id trace is already there.** `GET /v1/runs/{runId}` is the
  durable record on success *and* failure, carrying `request_body` (your request
  as received — confirm `num_ctx`/`reasoning_effort`/`response_format` arrived),
  `rendered_messages`, `error_type`/`error_msg`, `model`, `finish_reason`,
  `input/output/total_tokens`, and `duration_ms`. Proven on the 504 above: that
  failed run's `request_body` shows `num_ctx:None` on a 122k-char payload — the
  whole diagnosis in one lookup. New "Debugging a call by `runId`" section in
  INTEGRATION.md. (Note: the `runs/` *directory* deepread inspected is not the
  trace store — that's Postgres, via the API.)
- **runId on errors — nuance, mostly already true.** For *post-dispatch*
  failures (provider error/timeout), `aitelier_run_id` **is** returned —
  verified: a bad-model run returned `f30d9ded…` with `error_type:ProviderError`.
  It's genuinely absent only for *pre-dispatch request rejections* (422/400
  validation, e.g. an unsupported option) — because **no run is created**, so
  there's nothing to trace at the provider. Those still carry an
  **`X-Correlation-Id` response header** (always minted). So the consumer-side
  contract is: record `aitelier_run_id` when present, else the `X-Correlation-Id`
  header.
- **"Request params actually sent to the provider" — intentionally not a
  separate field.** The provider translation is pure + deterministic +
  unit-tested (`request_body.num_ctx` → Ollama `options.num_ctx`;
  `reasoning_effort:"minimal"` → `think:false`; `response_format.json_schema` →
  `format:<schema>`). Persisting the translated payload would be redundant state
  derivable from `request_body` + the documented mappings, so we're declining it
  on principle rather than adding a column.
- **One real partial gap: the upstream error *body*.** aitelier scrubs upstream
  provider responses to status-only (`"Upstream provider error (HTTP 404)"`) —
  a hosted-mode safety default that also hides a locally-useful Ollama message
  (e.g. "model not found"). Surfacing/persisting more of the upstream body for
  local routes is a reasonable follow-up, but it's a scrubbing-policy decision
  (what's safe to retain) rather than a missing trace — flagging, not yet
  actioned.

Net: the queryable inference trace deepread asked for is **already shipped**;
record the id (runId or correlation header) and pivot into `GET /v1/runs/{id}`.

---

## What already works well (so this is balanced)

- `embed` (nomic-embed-text) is reliable and fast; deepread's retrieval,
  clustering, and the new "Ask your corpus" feature all depend on it.
- `runAgent` (Rivet sandbox) is a solid escape hatch where `complete()` was
  failing — it backed summarize/fact-check through the rough period.
- The shim already captures `reasoning_content` and `runId`/`traceId` from
  responses, so the consumer-side changes for items 1 and 4 are small.
- Plain `completeStream` works well — deepread's per-doc chat and "Ask your
  corpus" stream fine against local non-reasoning models (e.g. gemma3).
