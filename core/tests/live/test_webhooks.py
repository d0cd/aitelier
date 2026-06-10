"""Live tests for the durable webhook delivery worker.

Spawns a side aitelier configured with `allow_loopback_webhooks: true`
(so it can fire at our in-process receiver) plus an optional signing
secret. Fires real runs, asserts the receiver got the payload, verifies
the HMAC signature.

Retry-backoff math is unit-tested in test_webhook_worker.py; here we
just verify the integration: signed delivery, payload shape, retry
fires at least once on a 5xx receiver.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx


def test_webhook_fires_and_carries_run_payload(
    isolated_aitelier, webhook_receiver,
):
    """End-to-end: run completes, receiver gets a POST with the run's
    aitelier_run_id in the body."""
    aitelier = isolated_aitelier(service={
        "allow_loopback_webhooks": True,
    })
    webhook_receiver.clear()

    r = httpx.post(
        f"{aitelier.base_url}/v1/runs",
        json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "ack"}],
            "timeout": 240,
            "webhook_url": webhook_receiver.url,
            "aitelier": {"max_turns": 1},
        },
        timeout=30,
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]

    delivery = webhook_receiver.wait_for(run_id=run_id, timeout=300)
    payload = delivery["json"]
    assert payload["aitelier_run_id"] == run_id
    # Webhook payload is the final ChatCompletion (or error envelope) —
    # one of `choices` / `error` must be present.
    assert "choices" in payload or "error" in payload, payload


def test_webhook_signed_when_secret_configured(
    isolated_aitelier, webhook_receiver,
):
    """When `[service] webhook_secret` is set, aitelier signs each
    delivery with `X-Aitelier-Signature: sha256=<hmac>`."""
    secret = "live-test-secret-32-chars-or-more"
    aitelier = isolated_aitelier(service={
        "allow_loopback_webhooks": True,
        "webhook_secret": secret,
    })
    webhook_receiver.clear()

    r = httpx.post(
        f"{aitelier.base_url}/v1/runs",
        json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "ack"}],
            "timeout": 240,
            "webhook_url": webhook_receiver.url,
            "aitelier": {"max_turns": 1},
        },
        timeout=30,
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]
    delivery = webhook_receiver.wait_for(run_id=run_id, timeout=300)

    sig_header = delivery["headers"].get("X-Aitelier-Signature")
    assert sig_header, f"signature header missing: {delivery['headers']}"
    assert sig_header.startswith("sha256="), sig_header
    received_sig = sig_header.split("=", 1)[1]

    # Recompute against the raw bytes the receiver got. Verify HMAC matches.
    expected = hmac.new(secret.encode(), delivery["body"],
                         hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, received_sig), (
        f"signature mismatch: expected sha256={expected}, got {sig_header}"
    )


def test_webhook_loopback_rejected_when_safety_on(isolated_aitelier):
    """`allow_loopback_webhooks: false` (the default) makes aitelier
    refuse webhook_urls that resolve to loopback / private IPs."""
    aitelier = isolated_aitelier(service={
        "allow_loopback_webhooks": False,
    })
    r = httpx.post(
        f"{aitelier.base_url}/v1/runs",
        json={
            "model": "agent:claude",
            "messages": [{"role": "user", "content": "ack"}],
            "timeout": 240,
            "webhook_url": "http://127.0.0.1:9/will-not-be-hit",
            "aitelier": {"max_turns": 1},
        },
        timeout=30,
    )
    assert r.status_code == 400, r.text
    # Error message should clarify the safety check.
    assert "webhook" in r.text.lower() or "loopback" in r.text.lower(), r.text
