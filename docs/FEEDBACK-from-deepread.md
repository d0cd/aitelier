# Feedback from deepread

> Consumer feedback from **deepread** (`~/projects/the-well/deepread`), which
> uses aitelier for `complete`, `embed`, and `runAgent`. Findings from a
> 2026-06-09 debugging session on why local-model summarization silently
> produces empty `doc_summaries`. Evidence is from direct probes against
> `localhost:7777/v1/chat/completions` and aitelier's OpenAI-compatible path.
>
> Contract reference on the deepread side: `deepread/docs/AITELIER_INTERFACE.md`.

## TL;DR

Two distinct issues at the deepread↔aitelier↔LiteLLM↔Ollama boundary make
local-model `complete()` unreliable for structured summarization:

1. **Reasoning-model output routing** — Ollama "thinking" models (qwen3,
   deepseek-r1, gpt-oss) return `content: ""` with the answer in
   `reasoning_content`/`thinking`. Callers reading `content` see empty output.
2. **`complete()` errors on real (large) payloads** — matches a previously
   documented LiteLLM-500-on-payload-size problem; summarize's full payload
   (priming exemplar + long document + JSON request) errors where a small
   probe succeeds.

Neither is a deepread bug or a model-quality problem. Both are best fixed at
the aitelier/LiteLLM layer. Workarounds exist on the deepread side but are
band-aids.

---

## 1. Reasoning models: answer lands in `reasoning_content`, `content` is empty

### Observed

```
POST /v1/chat/completions  { "model": "ollama/qwen3:8b",
  "messages": [{"role":"user","content":"Reply with the word OK"}] }

→ choices[0].message.content            = ""
  choices[0].message.reasoning_content  = "Okay, the user asked me to reply..."
```

Same with and without `response_format: {type: json_object}`. The model's
actual output is entirely in `reasoning_content`; `content` is empty. deepread
reads `content`, sees empty, and records a failed/empty summary. This is the
mechanism behind repeated "N empty doc_summaries" incidents.

This is a known LiteLLM/Ollama class of bug (LiteLLM #27956, #18922; Ollama
#10976): thinking-model tokens are routed to a separate field that the
OpenAI-compat mapping does not fold back into `content`.

### Why deepread can't fix it alone

The clean fix is to **disable thinking** so the answer goes to `content`
(`enable_thinking=false` / `think:false` / `chat_template_kwargs`). But
deepread cannot pass that through: aitelier's request schema is strict-mode
`extra="forbid"`, so any unknown top-level body field is 422'd, and
`extra_body` is rejected (documented in `deepread/src/aitelier-shim.ts`). The
only sanctioned channel today is the `aitelier` namespace field.

### Asks (any one resolves it)

- **(Preferred) Auto-disable thinking for Ollama models on `/v1/complete`**
  when the caller wants a final answer (i.e. default `think:false` for
  non-streaming completions), so `content` is always populated.
- **Or** accept a sanctioned option to control it, e.g.
  `aitelier: { ollama: { think: false } }` (fits the existing namespace and
  the `extra="forbid"` schema), and forward it to Ollama's `think`/
  `chat_template_kwargs`.
- **Or** when `content` is empty but `reasoning_content` is present, surface
  a clear signal (a non-`ok` finish reason / explicit flag) so callers can
  distinguish "model produced nothing" from "output went to the thinking
  field." (deepread already reads `reasoning_content`, so even exposing it
  consistently helps — but it's raw chain-of-thought, not clean structured
  output, so this is the weakest option.)

---

## 2. `complete()` errors on large payloads (the summarize path)

### Observed

- A *small* `complete()` with `response_format: json_object` to
  `ollama/gemma3:12b` (non-reasoning) returns valid JSON in ~7s. ✓
- The *full* deepread summarize payload (multi-turn priming exemplar + a long
  document + JSON request) to the same model fails with a provider error,
  recorded as "no output (error)". ✗

This matches a previously documented issue: LiteLLM returns HTTP 5xx for
deepread's payload sizes on `complete()`, which is why `summarize` and
`fact_check.classifyDocQuotes` historically used `runAgent` as a workaround.
deepread's summarize now has both paths; the `llm:ollama/...` branch routes
through `complete()` and hits this.

### Asks

- **Make the LiteLLM/Ollama `complete()` path robust to realistic payload
  sizes** (long document + system/priming context, a few thousand output
  tokens), or **document the hard limit** so callers can chunk.
- If there's a context-window/`num_ctx` mismatch causing silent CPU fallback
  or truncation for large inputs, surface it as a typed error rather than a
  generic provider 500.

---

## 3. Structured output: support JSON Schema constrained decoding

deepread currently sends `response_format: {type: "json_object"}` (JSON
*mode*). The more reliable mechanism for local models is **JSON Schema with
constrained decoding** — Ollama's `format` parameter (XGrammar under the
hood) guarantees schema-valid output regardless of model quality.

The deepread shim already supports the `json_schema` shape
(`toOpenAIResponseFormat`), so the gap is on aitelier's side:

### Ask

- For Ollama models, map `response_format: {type: "json_schema", json_schema:
  {schema}}` to Ollama's native `format: <schema>` (constrained decoding)
  rather than prompt-only JSON mode. This would make structured summarization
  reliable across models and largely sidesteps issue #1 for JSON outputs.

---

## What already works well (so this is balanced)

- `embed` (nomic-embed-text) is reliable and fast; deepread's whole retrieval
  / clustering / Ask layer depends on it and it has been solid.
- The `runAgent` (Rivet sandbox) path works where `complete()` does not —
  it's the current escape hatch for both summarize and fact-check.
- The shim already captures `reasoning_content` from responses, so once
  aitelier resolves #1 the consumer side needs little change.
- Plain (non-structured) `completeStream` works well — deepread's per-doc
  chat and the new "Ask your corpus" feature stream fine against local
  non-reasoning models (e.g. gemma3).

---

## Priority from deepread's perspective

1. **#2 (large-payload `complete()` reliability)** — this is the active
   blocker for unattended summarization; the corpus stays unenriched without
   it (the substrate's core value depends on summaries existing).
2. **#3 (JSON Schema constrained decoding)** — the durable fix that makes
   structured output model-agnostic and mitigates #1.
3. **#1 (thinking-mode handling)** — needed only if reasoning models are in
   scope; deepread can avoid it today by using non-reasoning models, but a
   sanctioned `think:false` would remove a recurring footgun.

---

## Response from aitelier (2026-06-09)

Thanks — the empty-`doc_summaries` symptom is real and worth chasing. We
verified each item live against `localhost:7777` (local models, so no
credential dependency). Headline: **#1 and #3 are already supported today**
— the report's "aitelier can't do X" premises are out of date — and **#2 is
not a LiteLLM issue** on these routes. Concrete resolutions below; one new
knob shipped for #2.

> Routing note that underlies all three: `local` and `ollama/*` **do not go
> through LiteLLM**. aitelier has its own Ollama adapter
> (`core/src/aitelier/providers/ollama.py`) precisely because LiteLLM's
> Ollama adapter drops `message.thinking`. So the cited LiteLLM issues
> (#27956, #18922) and the "LiteLLM 500 on payload size" history don't
> apply to the `ollama/*` path — that path is all aitelier + Ollama.

### #1 — reasoning output in `reasoning_content` → **already solvable today**

Pass **`reasoning_effort: "minimal"`** — a first-class OpenAI field (not
`extra_body`, so `extra="forbid"` does not block it). aitelier maps it to
Ollama `think:false`, and `content` is populated. Verified:

```
ollama/qwen3:8b + reasoning_effort:"minimal"  →  content:"OK", reasoning_content:""   ✓
ollama/qwen3:8b  (no reasoning_effort)         →  thinking ON; under a tight
                                                  max_tokens the budget burns on
                                                  hidden CoT and content can be ""
```

So the proposed `aitelier:{ollama:{think:false}}` is unnecessary —
`reasoning_effort:"minimal"` is the sanctioned channel and ships now. The
requested empty-output signal also already exists: **`aitelier_exit:"empty"`**
on the choice (fires only when neither content nor reasoning_content nor
tool_calls landed). See INTEGRATION.md → "Reasoning models" (table +
mitigations).

### #3 — JSON Schema constrained decoding → **already implemented**

aitelier already maps `response_format:{type:"json_schema", json_schema:{schema}}`
to Ollama's native `format:<schema>` (constrained decoding via XGrammar) —
`providers/ollama.py`. Verified live against `ollama/gemma3:12b`: returned
schema-valid JSON. **There is no aitelier-side gap** — deepread's shim
already emits the `json_schema` shape; just send it instead of
`json_object`. This is also the most robust mitigation for #1 (constrained
decoding forces a structured `content` regardless of thinking).

### #2 — large-payload failures → **typed `Timeout`, not a 500; now also `num_ctx`**

Reproduced: a ~26k-char payload to `ollama/gemma3:12b` summarized fine in
~16s; a ~122k-char payload returned **HTTP 504 `Timeout`**
(`"Upstream transport failure (ReadTimeout)"`) after the default 60s — a
**typed error**, which is exactly the report's ask #2b. Two
consumer-controllable causes + fixes:

1. **Slow generation on a big context → timeout.** Raise the per-request
   **`timeout`** field (seconds). The default is 60.
2. **Silent truncation.** Ollama defaults `num_ctx` to a small window and
   silently drops input past it. We **shipped a sanctioned `num_ctx`
   field** (this change) — Ollama-routes only, ignored elsewhere. Verified:
   a ~84k-char request that previously 504'd now completes with
   `num_ctx:32768` + `timeout:240`.

Neither is LiteLLM, and there's no generic-500 path here to fix.

### Net

| Report ask | Status |
|---|---|
| #1 disable thinking / signal empty | ✅ already: `reasoning_effort:"minimal"` + `aitelier_exit:"empty"` |
| #3 json_schema constrained decoding | ✅ already implemented (verified) |
| #2 large payloads / typed error | ✅ already a typed `Timeout`; ✅ new `num_ctx` for truncation; raise `timeout` for slow gen |

Recommended deepread changes: send `reasoning_effort:"minimal"` +
`response_format:{type:"json_schema",…}` for summarize, and set
`num_ctx`/`timeout` on long documents. No aitelier blockers remain; the one
code change from this round is the `num_ctx` field. Docs updated in
INTEGRATION.md → "Large inputs to local models".
