"""Smoke tests — validate wire format against real LiteLLM proxy and aitelier service.

Strict mode: smoke tests require the infrastructure to be up. Set
`AITELIER_SKIP_SMOKE=1` to deselect the smoke file at collection time
when running on a machine without the infra. If collected, a missing
proxy/service is a hard test failure, not a skip.

Run all tests (unit + smoke):
    uv run pytest core/tests/ sdks/python/tests/ -v

Run only smoke tests:
    uv run pytest core/tests/smoke_test.py -v
"""

from __future__ import annotations

import os

import httpx

LITELLM_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "sk-litellm-local")
AITELIER_URL = os.environ.get("AITELIER_BASE_URL", "http://localhost:7777")


# ---------------------------------------------------------------------------
# LiteLLM proxy
# ---------------------------------------------------------------------------


class TestLiteLLMProxy:
    def test_health(self):
        # `/health/liveness` is the no-auth shallow check (used by the
        # compose healthcheck). `/health` is a deep probe that hits every
        # configured backend and flaps on missing provider keys / 429s.
        resp = httpx.get(f"{LITELLM_URL}/health/liveness", timeout=5)
        assert resp.status_code == 200

    def test_chat_completions(self):
        """LiteLLM proxy direct call against `local` (Ollama). Validates
        the OpenAI response envelope; content may be empty on reasoning
        models when the token budget went to hidden thinking."""
        resp = httpx.post(
            f"{LITELLM_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"},
            json={
                "model": "local",
                "messages": [{"role": "user", "content": "Say 'ping'"}],
                "max_tokens": 100,
            },
            timeout=60,
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


class TestAitelierService:
    def test_health(self):
        resp = httpx.get(f"{AITELIER_URL}/v1/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "known_limitations" in data

    def test_chat_completions(self):
        """OpenAI-shape chat completion through the LiteLLM path against
        `local` (Ollama). Validates the response shape — content may be
        empty on reasoning models (qwen3 etc.) when the budget went to
        hidden thinking; finish_reason must still be honest, and the
        usage invariant must hold."""
        resp = httpx.post(
            f"{AITELIER_URL}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": "Say 'pong'"}],
                "max_tokens": 100,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        assert data.get("choices"), f"missing choices: {list(data.keys())}"
        choice = data["choices"][0]
        msg = choice["message"]
        # Reasoning models may surface output as content, reasoning_content,
        # or neither (with finish_reason=length/stop). Any of those is a
        # well-formed response.
        assert (
            msg.get("content")
            or msg.get("reasoning_content")
            or choice.get("finish_reason") in ("stop", "length")
        ), f"response has no content, reasoning, or terminal finish: {choice}"
        usage = data.get("usage", {})
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        # OpenAI invariant: aitelier preserves it on LLM and agent paths.
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

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
