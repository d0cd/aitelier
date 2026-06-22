# aitelier (TypeScript)

TypeScript SDK for [aitelier](https://github.com/d0cd/aitelier) — a
personal AI runtime that speaks **OpenAI shape for inference** and an
**aitelier-native control plane** for durable runs, traces, schedules,
and webhook delivery.

## Install

```bash
npm install aitelier               # control plane only
npm install aitelier openai        # add OpenAI for inference
```

`openai` is an optional peer dependency. Install it only if you'll call
`client.openai()`.

## Two layers, one client

```typescript
import { Aitelier } from "aitelier";

const ait = new Aitelier({ baseUrl: "http://localhost:7777", apiKey: "..." });

// Inference — pass-through to the OpenAI SDK.
const openai = await ait.openai();
const resp = await openai.chat.completions.create({
  model: "agent:claude/claude-sonnet-4-5",
  messages: [{ role: "user", content: "audit this repo" }],
  extra_body: { aitelier: { workspace: "/path/to/repo" } },
} as any);

// Control plane — methods on Aitelier itself.
const runs = await ait.listRuns({ traceTag: "audit", limit: 20 });
const traces = await ait.recentTraces({ status: "error" });
```

`ait.openai()` dynamically imports `openai` and returns a preconfigured
`OpenAI` instance pointed at aitelier. Streaming, retries, structured
outputs, tool semantics — all OpenAI SDK territory.

## Async agent runs

```typescript
const { run_id } = await ait.submitRun({
  model: "agent:claude/claude-sonnet-4-5",
  messages: [{ role: "user", content: "audit /workspace" }],
  aitelier: { workspace: "/path/to/repo", trace_tag: "audit-2026" },
  webhookUrl: "https://my.app/webhooks/aitelier",
});
const run = await ait.waitForRun(run_id, { timeoutSeconds: 300 });
console.log(run.result.content);
```

`waitForRun` polls server-side. With a `webhookUrl`, the terminal
payload (signed) lands at your endpoint automatically.

## Webhook verification

```typescript
import express from "express";
import { verifyWebhookSignature } from "aitelier";

const app = express();

app.post(
  "/webhooks/aitelier",
  express.raw({ type: "application/json" }),  // raw bytes — NOT json()
  (req, res) => {
    const sig = req.header("X-Aitelier-Signature");
    if (!verifyWebhookSignature(req.body, sig, process.env.WEBHOOK_SECRET!)) {
      return res.status(401).send("bad signature");
    }
    const payload = JSON.parse(req.body.toString("utf8"));
    // …handle payload, return 2xx fast.
    res.json({ ok: true });
  }
);
```

`verifyWebhookSignature` uses `crypto.timingSafeEqual` — constant-time
by construction.

## Configuration

The client reads `[service] host`/`port` from
`~/.config/aitelier/config.toml` if no `baseUrl` is passed.

## Examples

End-to-end recipes (Python) live in [`examples/`](../../examples). The
control-plane shape and JSON wire format are identical, so they
translate directly: `submit_run` → `submitRun`, `list_runs` → `listRuns`,
etc.

See [`docs/INTEGRATION.md`](../../docs/INTEGRATION.md) for the full
integration guide.
