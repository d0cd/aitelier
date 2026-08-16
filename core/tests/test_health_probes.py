"""Kubernetes-style liveness vs readiness probes and shutdown draining."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from aitelier import runtime
from aitelier.server import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_livez_stays_ok_while_dependencies_are_down(client):
    """Liveness answers for the process, not its dependencies — a failing
    livez tells an orchestrator to restart the pod, which never fixes an
    unreachable LiteLLM."""
    async def all_down():
        return {"litellm": {"reachable": False, "reason": "refused"},
                "sandbox_agent": {"reachable": False, "reason": "refused"}}

    with patch("aitelier.server._probe_dependencies", side_effect=all_down):
        assert client.get("/v1/readyz").status_code == 503
        resp = client.get("/v1/livez")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_livez_stays_ok_while_draining(client):
    """A draining process is alive and must not be killed mid-run."""
    runtime.begin_draining()

    resp = client.get("/v1/livez")

    assert resp.status_code == 200


def test_readyz_reports_not_ready_while_draining(client):
    """Readiness is what pulls the instance out of the load balancer."""
    runtime.begin_draining()

    resp = client.get("/v1/readyz")

    assert resp.status_code == 503
    assert resp.json()["status"] == "draining"


def test_readyz_ok_when_dependencies_are_reachable(client):
    """Readiness is decided by a live probe, so the probe has to be controlled
    here — asserting a bare 200 would only pass on a machine that happens to
    have LiteLLM and Sandbox Agent running."""
    async def all_up():
        return {"litellm": {"reachable": True},
                "sandbox_agent": {"reachable": True}}

    with patch("aitelier.server._probe_dependencies", side_effect=all_up):
        resp = client.get("/v1/readyz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "ready": True}


def test_new_work_is_refused_while_draining():
    """Draining stops new work at the same gate that enforces the
    in-flight cap, so every inference entry point honors it."""
    from fastapi import HTTPException

    runtime.begin_draining()

    with pytest.raises(HTTPException) as excinfo:
        runtime._reject_if_saturated()

    assert excinfo.value.status_code == 503
    assert "draining" in str(excinfo.value.detail).lower()


@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_runs_to_finish():
    """In-flight work completes rather than being cancelled."""
    finished = []

    async def slow_run():
        await asyncio.sleep(0.05)
        finished.append(True)

    task = asyncio.create_task(slow_run())
    runtime._active_runs["drain-1"] = task

    await runtime.drain_active_runs(timeout=2.0)

    assert finished == [True]
    assert not task.cancelled()
    runtime._active_runs.pop("drain-1", None)


@pytest.mark.asyncio
async def test_drain_cancels_runs_that_outlast_the_grace_period():
    """The grace period is bounded — a stuck run can't block shutdown."""
    async def stuck_run():
        await asyncio.sleep(30)

    task = asyncio.create_task(stuck_run())
    runtime._active_runs["drain-2"] = task

    await runtime.drain_active_runs(timeout=0.05)

    assert task.cancelled() or task.done()
    runtime._active_runs.pop("drain-2", None)


@pytest.mark.asyncio
async def test_drain_waits_for_runs_registered_after_the_snapshot():
    """A background producer (the schedule tick) can register a run while the
    drain is already awaiting. Re-deriving the task list after the timeout is
    what keeps that run from being abandoned mid-flight."""
    finished = []

    async def late_run():
        await asyncio.sleep(0.08)
        finished.append('late')

    async def early_run():
        await asyncio.sleep(0.02)
        runtime._active_runs['late'] = asyncio.create_task(late_run())

    runtime._active_runs['early'] = asyncio.create_task(early_run())

    await runtime.drain_active_runs(timeout=2.0)

    assert finished == ['late'], 'a run registered mid-drain was abandoned'
    runtime._active_runs.clear()


def test_readyz_probes_when_the_dependency_cache_is_cold(client):
    """readyz must answer from a real probe. Reading a cache that only
    /v1/discovery fills means a cold process reports ready having checked
    nothing."""
    from aitelier.server import _discovery_cache

    _discovery_cache['value'] = None
    _discovery_cache['at'] = 0.0
    probed = []

    async def fake_probe():
        probed.append(True)
        return {'litellm': {'reachable': False, 'reason': 'refused'}}

    with patch('aitelier.server._probe_dependencies', side_effect=fake_probe):
        resp = client.get('/v1/readyz')

    assert probed, 'readyz answered without probing a cold cache'
    assert resp.status_code == 503, resp.text
    assert resp.json()['unreachable'] == ['litellm']
