"""Tests for `aitelier status` — model filter and liveness probe targeting.

We mock httpx so the tests don't require a running LiteLLM. The point is
that the CLI hits `/health/liveness` (cheap, no auth, no upstream probing)
rather than `/health` (deep, auth-required, fails on upstream 429s).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aitelier.cli import _cmd_status


def _mock_liveness_resp():
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value={})
    return r


def _mock_models_resp(model_ids: list[str]):
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value={"data": [{"id": m} for m in model_ids]})
    return r


@pytest.fixture
def httpx_get():
    """Patch httpx.get so we can assert exactly which URLs the status
    command probes — and feed it canned responses."""
    with patch("httpx.get") as mock_get:
        yield mock_get


def test_status_probes_liveness_not_health(httpx_get, capsys):
    """Bug fix: the original code hit /health which triggers LiteLLM's deep
    upstream probe and 5xx's on any provider rate-limit. /health/liveness
    is the cheap no-auth check we actually want. (The aitelier service's
    own /v1/health is a different endpoint — only the LiteLLM probe should
    target /health/liveness.)"""
    httpx_get.return_value = _mock_liveness_resp()
    _cmd_status(all_models=False)
    urls = [call.args[0] for call in httpx_get.call_args_list]
    litellm_urls = [u for u in urls if ":4000" in u]
    assert any(u.endswith("/health/liveness") for u in litellm_urls), litellm_urls
    # /health is the deep probe — flaps on transient upstream issues.
    # Status must use /health/liveness instead.
    assert not any(u.endswith("/health") for u in litellm_urls), litellm_urls


def test_status_models_filtered_by_default(httpx_get, capsys):
    """Default view trims to curated aliases + pass-through markers. The
    full 200+ list goes behind --all-models. This is what makes the status
    output useful instead of a scroll-of-doom."""
    full = [
        "claude-sonnet", "claude-haiku", "local", "nomic-embed-text",
        "anthropic/*", "openai/*", "ollama/*",
    ] + [f"openai/gpt-{i}" for i in range(50)]

    def fake_get(url, **_kwargs):
        if "/models" in url:
            return _mock_models_resp(full)
        return _mock_liveness_resp()

    httpx_get.side_effect = fake_get
    _cmd_status(all_models=False)
    out = capsys.readouterr().out
    assert "claude-sonnet" in out
    assert "anthropic/*" in out
    # Curated set excludes raw OpenAI SKUs by default
    assert "openai/gpt-0" not in out
    # And we hint at the hidden count
    assert "more" in out


def test_status_all_models_shows_everything(httpx_get, capsys):
    full = ["claude-sonnet"] + [f"openai/gpt-{i}" for i in range(20)]

    def fake_get(url, **_kwargs):
        if "/models" in url:
            return _mock_models_resp(full)
        return _mock_liveness_resp()

    httpx_get.side_effect = fake_get
    _cmd_status(all_models=True)
    out = capsys.readouterr().out
    assert "claude-sonnet" in out
    assert "openai/gpt-0" in out
    assert "openai/gpt-19" in out
    # No "N more" hint when nothing is hidden.
    assert "more (run `aitelier status --all-models`" not in out
