"""Shared fixtures + marker registration for the live test suite.

Skip every test in this directory unless `AITELIER_LIVE_URL` is set, since
they hit a running aitelier service. See ./README.md for the contract.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest


def pytest_collection_modifyitems(config, items):
    """Apply the `live` marker (and the conditional skip) to every test here."""
    live_url = os.environ.get("AITELIER_LIVE_URL")
    skip = pytest.mark.skip(
        reason="AITELIER_LIVE_URL unset — live tests require a running service",
    )
    for item in items:
        if "/live/" not in str(item.fspath):
            continue
        item.add_marker("live")
        if not live_url:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> str:
    url = os.environ.get("AITELIER_LIVE_URL", "http://localhost:7777")
    # Fail fast if the service isn't actually up — better than a cascade
    # of confusing connection errors per-test.
    try:
        r = httpx.get(f"{url}/v1/health", timeout=3)
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(f"AITELIER_LIVE_URL={url} not reachable: {exc}")
    return url


@pytest.fixture(scope="session")
def http(base_url):
    with httpx.Client(base_url=base_url, timeout=120) as c:
        yield c


@pytest.fixture
def trace_tag() -> str:
    """Unique per-test trace_tag so we can query back without collision."""
    return f"live-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def discovery(http) -> dict:
    """Cached /v1/discovery — used to gate tests on dependency reachability."""
    return http.get("/v1/discovery").json()


@pytest.fixture(scope="session")
def litellm_models(discovery) -> list[str]:
    return discovery.get("dependencies", {}).get("litellm", {}).get("models") or []


@pytest.fixture(scope="session")
def sa_agents(discovery) -> list[str]:
    return discovery.get("dependencies", {}).get("sandbox_agent", {}).get("agents") or []


@pytest.fixture(scope="session")
def picked_agent(sa_agents) -> str:
    """Pick an agent backend for tests that need a successful run.

    Sandbox Agent's `mock` backend echoes the request back rather than running
    a real session — useful for protocol probes but useless for end-to-end
    behavior. Prefer real backends; fall back to mock for cases that only
    need the request to reach SA.
    """
    for preferred in ("claude", "codex", "mock"):
        if preferred in sa_agents:
            return preferred
    if not sa_agents:
        pytest.skip("no sandbox-agent backends advertised")
    return sa_agents[0]


def wait_for_run_state(http: httpx.Client, run_id: str, target: str,
                        timeout: float = 30.0) -> dict:
    """Poll /v1/runs/{run_id} until its state matches `target` or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # /v1/runs is filterable; /v1/runs/{id} reads the on-disk manifest.
        runs = http.get("/v1/runs", params={"limit": 100}).json()
        for r in runs:
            if r["run_id"] == run_id and r["state"] == target:
                return r
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach state={target} within {timeout}s")
