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
    def test_complete(self):
        resp = httpx.post(
            f"{AITELIER_URL}/v1/complete",
            json={
                "model": "claude-haiku",
                "messages": [{"role": "user", "content": "Say 'pong'"}],
                "max_tokens": 10,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        assert data["kind"] == "complete", f"kind={data['kind']}"
        assert data["status"] == "ok", f"status={data['status']}, error={data.get('error_msg')}"
        assert data.get("content"), "Empty content"
        assert data.get("usage"), "Missing usage"
        assert "input_tokens" in data["usage"]

    @litellm_available
    def test_embed(self):
        resp = httpx.post(
            f"{AITELIER_URL}/v1/embed",
            json={"texts": ["hello world"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        assert data["kind"] == "embed", f"kind={data['kind']}"
        assert data["status"] == "ok", f"status={data['status']}, error={data.get('error_msg')}"
        assert data.get("embeddings"), "Missing embeddings"
        assert data.get("dimensions") == 768, f"dimensions={data.get('dimensions')}"

    def test_traces(self):
        resp = httpx.get(f"{AITELIER_URL}/v1/traces?limit=1", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        assert isinstance(data, list)
