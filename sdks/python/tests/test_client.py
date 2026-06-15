"""Tests for the SDK client — verify control-plane methods hit the right
URL with the right body/headers. Server responses are mocked at the httpx layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aitelier_client import Aitelier


def _stub_http(client: Aitelier, fake_resp_dict, *, status_code=200):
    """Replace the client's internal httpx client with a mock returning fake_resp_dict."""
    fake = MagicMock()
    fake.is_closed = False
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value=fake_resp_dict)
    fake.post = AsyncMock(return_value=fake_resp)
    fake.get = AsyncMock(return_value=fake_resp)
    fake.delete = AsyncMock(return_value=fake_resp)
    client._client = fake
    return fake


# --- base_url resolution: explicit > config-file discovery > default --------

def test_base_url_explicit_arg_wins(tmp_path, monkeypatch):
    """Explicit `base_url=` beats config file and default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "aitelier"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        '[service]\nhost = "from-config"\nport = 9999\n'
    )
    sdk = Aitelier(base_url="http://explicit:1111")
    assert sdk.base_url == "http://explicit:1111"


def test_base_url_falls_back_to_user_config(tmp_path, monkeypatch):
    """No explicit arg → read ~/.config/aitelier/config.toml's [service]."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "aitelier"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        '[service]\nhost = "remote-host"\nport = 8080\n'
    )
    sdk = Aitelier()
    assert sdk.base_url == "http://remote-host:8080"


def test_base_url_default_when_no_config(tmp_path, monkeypatch):
    """No explicit, no config file → localhost:7777."""
    monkeypatch.setenv("HOME", str(tmp_path))  # empty home, no config file
    sdk = Aitelier()
    assert sdk.base_url == "http://localhost:7777"


def test_base_url_ignores_env_var(tmp_path, monkeypatch):
    """Principled invariant: SDK does not read AITELIER_BASE_URL or any
    other env var. Setting it must not change anything."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AITELIER_BASE_URL", "http://from-env:6666")
    sdk = Aitelier()
    # Default wins because there's no config file and no explicit arg.
    assert sdk.base_url == "http://localhost:7777"


# --- .openai() lazy construction --------------------------------------------


def test_openai_helper_raises_when_package_missing(monkeypatch):
    """Without the `openai` extra, `.openai()` should fail with a helpful hint."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("no module named openai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sdk = Aitelier()
    with pytest.raises(ImportError, match="aitelier-client\\[openai\\]"):
        sdk.openai()


def test_openai_helper_returns_async_client_when_installed():
    pytest.importorskip("openai")
    sdk = Aitelier(base_url="http://localhost:7777", api_key="bearer-x")
    client = sdk.openai()
    # Don't poke private internals; just confirm it's the right shape.
    assert hasattr(client, "chat")
    assert hasattr(client.chat, "completions")
    # Second call returns the same instance (cached).
    assert sdk.openai() is client


# --- Async run submission ---------------------------------------------------


@pytest.mark.asyncio
async def test_submit_run_hits_v1_runs_with_idempotency():
    sdk = Aitelier()
    fake = _stub_http(sdk, {"run_id": "r-abc", "status": "accepted"})
    result = await sdk.submit_run(
        model="agent:claude",
        messages=[{"role": "user", "content": "do it"}],
        webhook_url="https://hooks.example.com/done",
        idempotency_key="key-1",
        correlation_id="cid-1",
    )
    assert result["run_id"] == "r-abc"
    args, kwargs = fake.post.call_args
    assert args[0] == "/v1/runs"
    body = kwargs["json"]
    assert body["model"] == "agent:claude"
    assert body["webhook_url"] == "https://hooks.example.com/done"
    headers = kwargs["headers"]
    assert headers["Idempotency-Key"] == "key-1"
    assert headers["X-Correlation-Id"] == "cid-1"


# --- Control plane ----------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_run_hits_post_cancel_endpoint():
    sdk = Aitelier()
    fake = _stub_http(sdk, {"run_id": "abc", "cancelled": True})
    result = await sdk.cancel_run("abc")
    assert result.run_id == "abc"
    assert result.cancelled is True
    args, _ = fake.post.call_args
    assert args[0] == "/v1/runs/abc/cancel"


@pytest.mark.asyncio
async def test_list_active_runs():
    sdk = Aitelier()
    fake = _stub_http(sdk, {"active": ["r1", "r2"]})
    result = await sdk.list_active_runs()
    assert result.active == ["r1", "r2"]
    args, _ = fake.get.call_args
    assert args[0] == "/v1/runs/active"


@pytest.mark.asyncio
async def test_wait_for_run_posts_with_timeout_params():
    sdk = Aitelier()
    fake = _stub_http(sdk, {
        "run_id": "abc", "state": "completed", "kind": "agent",
    })
    run = await sdk.wait_for_run("abc", timeout=10, poll_interval=0.25)
    assert run.run_id == "abc"
    assert run.state == "completed"
    args, kwargs = fake.post.call_args
    assert args[0] == "/v1/runs/abc/wait"
    assert kwargs["params"] == {"timeout": 10, "poll_interval": 0.25}


@pytest.mark.asyncio
async def test_list_runs_passes_parent_run_id_filter():
    sdk = Aitelier()
    fake = _stub_http(sdk, [])
    await sdk.list_runs(parent_run_id="parent-1", limit=10)
    _, kwargs = fake.get.call_args
    assert kwargs["params"]["parent_run_id"] == "parent-1"
    assert kwargs["params"]["limit"] == 10


@pytest.mark.asyncio
async def test_discovery():
    sdk = Aitelier()
    fake_disc = {
        "service": "aitelier", "version": "0.1.0", "api_version": "v1",
        "timestamp": "2026-05-12T00:00:00Z",
        "endpoints": [],
        "capabilities": {},
        "dependencies": {
            "litellm": {"reachable": True, "base_url": "http://localhost:4000"},
            "sandbox_agent": {"reachable": True, "base_url": "http://localhost:2468"},
        },
        "schemas": {},
        "known_limitations": [],
    }
    fake = _stub_http(sdk, fake_disc)
    result = await sdk.discovery()
    assert result.service == "aitelier"
    assert result.dependencies.litellm.reachable is True
    args, _ = fake.get.call_args
    assert args[0] == "/v1/discovery"


@pytest.mark.asyncio
async def test_get_schema():
    sdk = Aitelier()
    fake_schema = {"$schema": "...", "type": "object"}
    fake = _stub_http(sdk, fake_schema)
    result = await sdk.get_schema("run")
    assert result["type"] == "object"
    args, _ = fake.get.call_args
    assert args[0] == "/v1/schemas/run"


# --- Run scores (eval framework write-back) --------------------------------


@pytest.mark.asyncio
async def test_add_run_score_posts_body_and_returns_typed_row():
    """add_run_score sends only the optional fields the caller provided —
    no `comment: None` noise on the wire — and returns a parsed RunScore."""
    sdk = Aitelier()
    server_row = {
        "id": 7, "run_id": "r-1", "name": "helpfulness", "value": 0.8,
        "evaluator": "gpt-4o-judge", "comment": None, "metadata": None,
        "created_at": "2026-05-28T12:00:00Z",
    }
    fake = _stub_http(sdk, server_row, status_code=201)
    score = await sdk.add_run_score(
        "r-1", name="helpfulness", value=0.8, evaluator="gpt-4o-judge",
    )
    assert score.id == 7 and score.value == 0.8
    args, kwargs = fake.post.call_args
    assert args[0] == "/v1/runs/r-1/scores"
    assert kwargs["json"] == {
        "name": "helpfulness", "value": 0.8, "evaluator": "gpt-4o-judge",
    }


@pytest.mark.asyncio
async def test_add_run_score_includes_comment_and_metadata_when_given():
    sdk = Aitelier()
    server_row = {
        "id": 8, "run_id": "r-2", "name": "factuality", "value": 1.0,
        "evaluator": "human:r3", "comment": "verified",
        "metadata": {"rubric_version": 2}, "created_at": None,
    }
    fake = _stub_http(sdk, server_row, status_code=201)
    await sdk.add_run_score(
        "r-2", name="factuality", value=1.0, evaluator="human:r3",
        comment="verified", metadata={"rubric_version": 2},
    )
    _, kwargs = fake.post.call_args
    assert kwargs["json"]["comment"] == "verified"
    assert kwargs["json"]["metadata"] == {"rubric_version": 2}


@pytest.mark.asyncio
async def test_list_run_scores_returns_typed_history():
    sdk = Aitelier()
    rows = {
        "object": "list",
        "data": [
            {"id": 1, "run_id": "r-1", "name": "h", "value": 0.5,
             "evaluator": "j", "comment": None, "metadata": None,
             "created_at": "2026-05-28T12:00:00Z"},
            {"id": 2, "run_id": "r-1", "name": "h", "value": 0.7,
             "evaluator": "j", "comment": None, "metadata": None,
             "created_at": "2026-05-28T12:01:00Z"},
        ],
    }
    fake = _stub_http(sdk, rows)
    scores = await sdk.list_run_scores("r-1")
    assert [s.value for s in scores] == [0.5, 0.7]
    args, _ = fake.get.call_args
    assert args[0] == "/v1/runs/r-1/scores"


@pytest.mark.asyncio
async def test_export_runs_streams_ndjson_and_parses_per_line():
    """export_runs is an async iterator over Run rows. The underlying
    HTTP call uses `client.stream(...)`; iterating `aiter_lines()` yields
    one Run per non-empty line."""
    import contextlib

    sdk = Aitelier()
    fake_client = MagicMock()
    fake_client.is_closed = False

    class _StreamResp:
        status_code = 200
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield '{"run_id": "r-1", "state": "completed", "kind": "complete"}'
            yield ""  # blank line: must be skipped
            yield '{"run_id": "r-2", "state": "failed", "kind": "complete"}'

    @contextlib.asynccontextmanager
    async def fake_stream(method, url, params=None):
        fake_stream.last_call = (method, url, params)
        yield _StreamResp()
    fake_client.stream = fake_stream
    sdk._client = fake_client

    runs = [r async for r in sdk.export_runs(trace_tag="audit", limit=50)]
    assert [r.run_id for r in runs] == ["r-1", "r-2"]
    method, url, params = fake_stream.last_call
    assert method == "GET"
    assert url == "/v1/runs/export"
    assert params["trace_tag"] == "audit"
    assert params["limit"] == 50


@pytest.mark.asyncio
async def test_export_runs_omits_unset_filters():
    """No filters → only `limit` in params. Avoids sending `since=None`
    string which the server would 400 on."""
    import contextlib

    sdk = Aitelier()
    fake_client = MagicMock()
    fake_client.is_closed = False

    class _StreamResp:
        status_code = 200
        def raise_for_status(self): pass
        async def aiter_lines(self):
            if False:
                yield ""

    captured = {}
    @contextlib.asynccontextmanager
    async def fake_stream(method, url, params=None):
        captured["params"] = params
        yield _StreamResp()
    fake_client.stream = fake_stream
    sdk._client = fake_client

    async for _ in sdk.export_runs():
        pass
    assert set(captured["params"].keys()) == {"limit"}
