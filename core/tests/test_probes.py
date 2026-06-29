"""Direct unit tests for probes.py.

The /v1/discovery and /v1/health endpoint tests patch the probe functions out
wholesale, so their real branching — the `mock`-backend filter, the
list-vs-{"agents": [...]} normalization, and the HTTP-error / transport-exception
reason paths — never runs there. These exercise it against fake transports.
"""

import asyncio
from types import SimpleNamespace

from aitelier.probes import (
    _normalize_agents_payload,
    _probe_litellm,
    _probe_sandbox_agent,
)


def _cfg():
    return SimpleNamespace(
        sandbox_agent=SimpleNamespace(base_url="http://sa:2468", token=None),
        litellm=SimpleNamespace(base_url="http://litellm:4000", api_key="k"),
    )


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _patch_client(monkeypatch, *, get):
    async def fake_get_shared_client():
        return SimpleNamespace(get=get)

    monkeypatch.setattr(
        "aitelier.providers.llm.get_shared_client", fake_get_shared_client,
    )


def test_normalize_agents_payload_accepts_both_shapes():
    assert _normalize_agents_payload([{"id": "claude"}]) == [{"id": "claude"}]
    assert _normalize_agents_payload({"agents": [{"id": "codex"}]}) == [{"id": "codex"}]
    assert _normalize_agents_payload({}) == []


def test_probe_sandbox_agent_filters_mock_backend(monkeypatch):
    async def fake_get(url, **kwargs):
        return _Resp(200, {"agents": [
            {"id": "claude"}, {"id": "mock"}, "codex", "mock",
        ]})

    _patch_client(monkeypatch, get=fake_get)
    out = asyncio.run(_probe_sandbox_agent(_cfg()))
    assert out["reachable"] is True
    assert out["agents"] == ["claude", "codex"]  # sorted, "mock" dropped


def test_probe_sandbox_agent_normalizes_bare_list(monkeypatch):
    async def fake_get(url, **kwargs):
        return _Resp(200, [{"id": "codex"}, {"id": "claude"}])

    _patch_client(monkeypatch, get=fake_get)
    out = asyncio.run(_probe_sandbox_agent(_cfg()))
    assert out["agents"] == ["claude", "codex"]


def test_probe_sandbox_agent_http_error_reason(monkeypatch):
    async def fake_get(url, **kwargs):
        return _Resp(503, {})

    _patch_client(monkeypatch, get=fake_get)
    out = asyncio.run(_probe_sandbox_agent(_cfg()))
    assert out["reachable"] is False
    assert out["reason"] == "HTTP 503"


def test_probe_sandbox_agent_transport_exception_reason(monkeypatch):
    async def fake_get(url, **kwargs):
        raise ConnectionError("refused")

    _patch_client(monkeypatch, get=fake_get)
    out = asyncio.run(_probe_sandbox_agent(_cfg()))
    assert out["reachable"] is False
    assert out["reason"] == "ConnectionError: refused"


def test_probe_litellm_lists_models_then_http_error(monkeypatch):
    async def fake_ok(url, **kwargs):
        return _Resp(200, {"data": [{"id": "b"}, {"id": "a"}, {"no_id": 1}]})

    _patch_client(monkeypatch, get=fake_ok)
    out = asyncio.run(_probe_litellm(_cfg()))
    assert out["reachable"] is True
    assert out["models"] == ["a", "b"]  # sorted, id-less entry skipped

    async def fake_500(url, **kwargs):
        return _Resp(500, {})

    _patch_client(monkeypatch, get=fake_500)
    out = asyncio.run(_probe_litellm(_cfg()))
    assert out["reachable"] is False
    assert out["reason"] == "HTTP 500"
