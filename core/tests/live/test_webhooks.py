"""Live tests for the durable webhook delivery worker.

Spawns a side aitelier configured with `allow_loopback_webhooks: true`
(so it can fire at our in-process receiver) plus an optional shared
secret. Fires real runs, asserts the receiver got the payload, verifies
the Bearer header when configured.

Retry-backoff math is unit-tested in test_webhook_worker.py; here we
just verify the integration: authenticated delivery, payload shape,
loopback safety toggle.
"""

from __future__ import annotations

import hmac

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


def test_webhook_authenticated_when_secret_configured(
    isolated_aitelier, webhook_receiver,
):
    """When `[service] webhook_secret` is set, aitelier authenticates
    each delivery with `Authorization: Bearer <secret>`. Receivers
    verify with a constant-time string compare."""
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

    auth_header = delivery["headers"].get("Authorization")
    assert auth_header, (
        f"Authorization header missing: {delivery['headers']}"
    )
    assert auth_header.startswith("Bearer "), auth_header
    token = auth_header.removeprefix("Bearer ")
    assert hmac.compare_digest(token, secret), (
        "Bearer token mismatch — receiver got a different secret than configured"
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
