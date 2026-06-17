"""Tests for sandbox_proxy.py — the install/commands/files/sidecars/artifacts
orchestration that backs `aitelier.prepare` + `aitelier.artifacts`.

These were previously only exercised indirectly through end-to-end tests.
The unit tests below stub `sa_proxy` so we can validate the orchestration
logic — abort-on-first-failure, sequential ordering, best-effort artifacts,
sidecar cleanup — without standing up a real Sandbox Agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aitelier.sandbox_proxy import (
    fetch_artifacts,
    prepare_failed_result,
    run_prepare,
    stop_sidecars,
)

# --- run_prepare ------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_prepare_returns_empty_skeleton_when_no_prep():
    """`prepare: None` and `prepare: {}` are both valid no-op inputs;
    the caller still gets a populated result dict to iterate over."""
    for empty in (None, {}):
        out = await run_prepare(empty)
        assert out == {
            "install_results": [],
            "command_results": [],
            "file_results": [],
            "sidecars": [],
            "error": None,
        }


@pytest.mark.asyncio
async def test_run_prepare_aborts_on_command_non_zero_exit():
    """A non-zero exit code from any command stops the pipeline before
    files/sidecars run. The whole chain depends on commands succeeding
    (e.g., dependency install before file seed)."""
    call_log: list[tuple[str, str]] = []

    async def fake_sa_proxy(method, path, **kwargs):
        call_log.append((method, path))
        if path == "/v1/processes/run":
            return {"exit_code": 2, "stdout": "", "stderr": "boom"}
        return {}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        out = await run_prepare({
            "commands": [{"command": ["false"]}],
            "files":    [{"path": "/work/x.txt", "content": "noop"}],
            "sidecars": [{"command": ["sleep", "9"]}],
        })

    assert out["error"] is not None
    assert "exit 2" in out["error"]
    assert out["command_results"][0]["exit_code"] == 2
    assert out["file_results"] == []
    assert out["sidecars"] == []
    assert call_log == [("POST", "/v1/processes/run")]


@pytest.mark.asyncio
async def test_run_prepare_aborts_on_command_exception():
    """If the SA call itself raises (network failure, 5xx → HTTPException),
    record the failure in command_results and abort. Don't proceed to
    files/sidecars on an undefined-state command."""
    async def fake_sa_proxy(method, path, **kwargs):
        if path == "/v1/processes/run":
            raise RuntimeError("connection refused")
        return {}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        out = await run_prepare({
            "commands": [{"command": ["cmd"]}],
            "files":    [{"path": "/work/y.txt", "content": "x"}],
        })

    assert out["error"] is not None
    assert "raised" in out["error"]
    assert out["command_results"][0]["error"] == "connection refused"
    assert out["file_results"] == []


@pytest.mark.asyncio
async def test_run_prepare_aborts_on_file_write_failure():
    """File seed is sequential: if one fails, the rest are skipped and
    sidecars don't start. This matches the caller's "files are seed data
    the agent needs before it runs" contract."""
    files_attempted: list[str] = []

    async def fake_sa_proxy(method, path, params=None, **kwargs):
        if path == "/v1/fs/file" and method == "PUT":
            files_attempted.append(params["path"])
            if params["path"] == "/work/second.txt":
                raise RuntimeError("disk full")
            return {}
        return {}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        out = await run_prepare({
            "files": [
                {"path": "/work/first.txt",  "content": "a"},
                {"path": "/work/second.txt", "content": "b"},
                {"path": "/work/third.txt",  "content": "c"},
            ],
        })

    assert files_attempted == ["/work/first.txt", "/work/second.txt"]
    assert out["error"] is not None
    assert "/work/second.txt" in out["error"]
    assert out["file_results"][-1]["ok"] is False


@pytest.mark.asyncio
async def test_run_prepare_passes_path_via_query_param_not_body():
    """SA's PUT /v1/fs/file expects `path` as a query-string param,
    not in the JSON body. Regression: a previous version sent path in
    the body and got `missing field path` from SA's deserializer."""
    captured: dict = {}

    async def fake_sa_proxy(method, path, json_body=None, params=None, **kwargs):
        if path == "/v1/fs/file":
            captured["params"] = params
            captured["body"] = json_body
        return {}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        await run_prepare({
            "files": [{"path": "/work/x.txt", "content": "hello",
                       "encoding": "utf-8"}],
        })

    assert captured["params"] == {"path": "/work/x.txt"}
    assert "path" not in (captured["body"] or {})
    assert captured["body"] == {"content": "hello", "encoding": "utf-8"}


@pytest.mark.asyncio
async def test_run_prepare_sidecar_failure_is_recorded_but_not_fatal():
    """Sidecar startup failures are surfaced in the result but don't abort
    the pipeline. Unlike files/commands, sidecars are best-effort: the
    agent run can proceed without them (a missing observability sidecar
    shouldn't block the actual workload)."""
    async def fake_sa_proxy(method, path, json_body=None, **kwargs):
        if path == "/v1/processes":
            raise RuntimeError("port in use")
        return {}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        out = await run_prepare({
            "sidecars": [{"name": "metrics", "command": ["serve"]}],
        })

    assert out["error"] is None  # not fatal
    assert out["sidecars"][0]["state"] == "failed"
    assert "port in use" in out["sidecars"][0]["error"]


@pytest.mark.asyncio
async def test_run_prepare_validates_agent_name_before_install():
    """`install_agents: ["../etc"]` shouldn't reach SA — `validate_path_component`
    rejects path-traversal-shaped names. Failure is captured in install_results
    and the chain continues (install is best-effort)."""
    called_paths: list[str] = []

    async def fake_sa_proxy(method, path, **kwargs):
        called_paths.append(path)
        return {}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        out = await run_prepare({"install_agents": ["../etc"]})

    assert called_paths == []  # validation rejected before the call
    assert out["install_results"][0]["ok"] is False


# --- stop_sidecars ----------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sidecars_calls_stop_for_each_with_id():
    """Sidecars without an `id` (e.g., ones that failed to start) are
    skipped — there's nothing to stop. State is updated in-place so
    the caller can report final disposition."""
    stop_calls: list[str] = []

    async def fake_sa_proxy(method, path, **kwargs):
        stop_calls.append(path)
        return {}

    sidecars = [
        {"name": "a", "id": "proc-1", "state": "running"},
        {"name": "b", "id": None, "state": "failed"},          # never started
        {"name": "c", "id": "proc-3", "state": "running"},
    ]
    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        await stop_sidecars(sidecars)

    assert stop_calls == ["/v1/processes/proc-1/stop", "/v1/processes/proc-3/stop"]
    assert sidecars[0]["state"] == "stopped"
    assert sidecars[1]["state"] == "failed"  # untouched
    assert sidecars[2]["state"] == "stopped"


@pytest.mark.asyncio
async def test_stop_sidecars_swallows_errors():
    """`stop_sidecars` runs from a `finally` block; raising would mask
    the original error. Errors are captured into the sidecar's state."""
    async def fake_sa_proxy(method, path, **kwargs):
        raise RuntimeError("network gone")

    sidecars = [{"name": "x", "id": "proc-1", "state": "running"}]
    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        await stop_sidecars(sidecars)  # must not raise

    assert sidecars[0]["state"].startswith("stop_failed")


# --- fetch_artifacts --------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_artifacts_returns_empty_when_spec_missing():
    assert await fetch_artifacts(None) == {}
    assert await fetch_artifacts({}) == {}


@pytest.mark.asyncio
async def test_fetch_artifacts_collects_per_file_results_best_effort():
    """One missing file shouldn't prevent fetching the others.
    Missing/failed reads come back as `{error: ...}` entries; successful
    reads carry their content. This mirrors how artifact fetching works
    after the agent run — operators want partial output, not "all or nothing."""
    async def fake_sa_proxy(method, path, params=None, **kwargs):
        target = params["path"]
        if target == "/work/missing.txt":
            raise RuntimeError("no such file")
        return {"content": f"<content of {target}>"}

    with patch("aitelier.sandbox_proxy.sa_proxy", new=AsyncMock(side_effect=fake_sa_proxy)):
        out = await fetch_artifacts({"fetch": [
            "/work/result.json",
            "/work/missing.txt",
            "/work/log.txt",
        ]})

    assert out["/work/result.json"] == "<content of /work/result.json>"
    assert out["/work/log.txt"] == "<content of /work/log.txt>"
    assert out["/work/missing.txt"] == {"error": "no such file"}


# --- prepare_failed_result --------------------------------------------------


def test_prepare_failed_result_shape():
    """The result envelope returned when prepare aborts must have the
    same shape as a real agent failure — `record_run` consumers and the
    OpenAI-compat error path depend on it."""
    prepare = {"error": "command failed (exit 1)", "command_results": []}
    out = prepare_failed_result("run-123", prepare, cid="corr-xyz")

    assert out["status"] == "error"
    assert out["kind"] == "agent"
    assert out["run_id"] == "run-123"
    assert out["trace_id"] == "run-123"
    assert out["error_type"] == "PrepareFailed"
    assert "command failed" in out["error_msg"]
