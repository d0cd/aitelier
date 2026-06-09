"""Minimal FastAPI receiver that verifies aitelier webhook signatures.

Aitelier signs every webhook delivery with `X-Aitelier-Signature: sha256=<hex>`
when `[service] webhook_secret` is set on the server side. The receiver
must recompute HMAC-SHA256 over the **raw request body bytes** and
compare in constant time — re-serialized JSON breaks the signature.

The SDK's `verify_webhook_signature` is the one-liner you want. Don't
roll your own with `==`: that's a timing oracle.

Run:
    export AITELIER_WEBHOOK_SECRET="<same value as [service] webhook_secret>"
    uv run uvicorn 04_webhook_receiver:app --port 8000

Aitelier delivers terminal payloads here when a /v1/runs submission or
a /v1/schedules tick has finished. Pair with examples 01 or 03.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request

from aitelier_client import verify_webhook_signature

logger = logging.getLogger("webhook-receiver")
app = FastAPI()

SECRET = os.environ.get("AITELIER_WEBHOOK_SECRET", "")


@app.post("/webhooks/aitelier")
async def receive(request: Request) -> dict:
    # 1. Read the raw bytes BEFORE parsing JSON — re-serialization
    #    breaks the signature.
    body = await request.body()
    sig = request.headers.get("X-Aitelier-Signature")

    if SECRET and not verify_webhook_signature(body, sig, SECRET):
        # Reject without echoing the reason; a 401 is enough.
        raise HTTPException(status_code=401, detail="bad signature")

    # 2. NOW parse and act on the payload. Shape depends on the source:
    #    - /v1/runs submissions: top-level ChatCompletion + aitelier_run_id
    #    - /v1/schedules ticks:  {schedule_id, run_id, result: {...}}
    payload = await request.json()
    run_id = payload.get("aitelier_run_id") or payload.get("run_id")
    err = (payload.get("error")
           or (payload.get("result") or {}).get("error"))

    if err:
        logger.warning("run %s failed: %s — %s",
                       run_id, err.get("type"), err.get("message"))
    else:
        logger.info("run %s completed", run_id)

    # 3. Return 2xx fast. Aitelier will retry non-2xx with exponential
    #    backoff (1s / 5s / 30s / 5min / 1hr, 5 attempts). Long-running
    #    consumer work should be queued, not done inline.
    return {"ok": True}
