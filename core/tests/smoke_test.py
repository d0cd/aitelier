"""Smoke tests — validate wire format against real LiteLLM proxy and aitelier service.

These tests are skipped when the services aren't reachable.

Run all tests (unit + smoke):
    uv run pytest core/tests/ sdks/python/tests/ -v

Run only smoke tests:
    uv run pytest core/tests/smoke_test.py -v
"""

from __future__ import annotations

import os

import httpx
import pytest

LITELLM_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "sk-litellm-local")
AITELIER_URL = os.environ.get("AITELIER_BASE_URL", "http://localhost:7777")


def _litellm_reachable() -> bool:
    try:
        resp = httpx.get(
            f"{LITELLM_URL}/health",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            timeout=3,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _aitelier_reachable() -> bool:
    try:
        resp = httpx.get(f"{AITELIER_URL}/v1/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


litellm_available = pytest.mark.skipif(
    not _litellm_reachable(), reason=f"LiteLLM proxy not reachable at {LITELLM_URL}"
)
aitelier_available = pytest.mark.skipif(
    not _aitelier_reachable(), reason=f"aitelier service not reachable at {AITELIER_URL}"
)


# ---------------------------------------------------------------------------
# LiteLLM proxy
# ---------------------------------------------------------------------------


@litellm_available
class TestLiteLLMProxy:
    def test_health(self):
        resp = httpx.get(
            f"{LITELLM_URL}/health",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            timeout=5,
        )
        assert resp.status_code == 200

    def test_chat_completions(self):
        resp = httpx.post(
            f"{LITELLM_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"},
            json={
                "model": "claude-haiku",
                "messages": [{"role": "user", "content": "Say 'ping'"}],
                "max_tokens": 10,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        assert "choices" in data, f"Missing 'choices': {list(data.keys())}"
        assert len(data["choices"]) > 0, "Empty choices"
        assert "message" in data["choices"][0]
        assert "content" in data["choices"][0]["message"]
        assert "usage" in data, f"Missing 'usage': {list(data.keys())}"
        assert "prompt_tokens" in data["usage"]

    def test_embeddings(self):
        resp = httpx.post(
            f"{LITELLM_URL}/embeddings",
            headers={"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"},
            json={"model": "nomic-embed-text", "input": ["test"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        assert "data" in data, f"Missing 'data': {list(data.keys())}"
        assert len(data["data"]) > 0, "Empty data"
        assert "embedding" in data["data"][0]
        dims = len(data["data"][0]["embedding"])
        assert dims == 768, f"Expected 768 dimensions, got {dims}"


# ---------------------------------------------------------------------------
# aitelier service
# ---------------------------------------------------------------------------


@aitelier_available
class TestAitelierService:
    def test_health(self):
        resp = httpx.get(f"{AITELIER_URL}/v1/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "known_limitations" in data

    @litellm_available
    def test_chat_completions(self):
        """OpenAI-shape chat completion through the LiteLLM path. Replaces
        the retired `/v1/complete` smoke test — aitelier moved to
        `/v1/chat/completions` with OpenAI response shape."""
        resp = httpx.post(
            f"{AITELIER_URL}/v1/chat/completions",
            json={
                "model": "claude-haiku",
                "messages": [{"role": "user", "content": "Say 'pong'"}],
                "max_tokens": 10,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        assert data.get("choices"), f"missing choices: {list(data.keys())}"
        msg = data["choices"][0]["message"]
        assert msg.get("content"), "empty content"
        usage = data.get("usage", {})
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        # OpenAI invariant: aitelier preserves it on LLM and agent paths.
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    @litellm_available
    def test_embeddings_via_aitelier(self):
        """`/v1/embeddings` is the canonical embeddings endpoint. The old
        aitelier-native `/v1/embed` is retired (404s)."""
        resp = httpx.post(
            f"{AITELIER_URL}/v1/embeddings",
            json={"model": "nomic-embed-text", "input": "hello world"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        assert "data" in data, f"missing data: {list(data.keys())}"
        emb = data["data"][0]["embedding"]
        assert isinstance(emb, list) and len(emb) == 768, (
            f"expected 768-float vector, got len={len(emb) if hasattr(emb, '__len__') else 'n/a'} "
            f"type={type(emb).__name__}"
        )

    def test_metrics(self):
        """Runtime counters endpoint added in the CPU-leak audit. Sanity-
        checks that operators can hit it without auth and see shape."""
        resp = httpx.get(f"{AITELIER_URL}/v1/metrics", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        assert data["uptime_seconds"] >= 0
        assert "rss_mb" in data["process"]
        assert "in_flight" in data["runs"]
        assert "pending" in data["webhooks"]

    def test_traces(self):
        resp = httpx.get(f"{AITELIER_URL}/v1/traces?limit=1", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        assert isinstance(data, list)
