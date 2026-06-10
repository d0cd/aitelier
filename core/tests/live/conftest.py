"""Shared fixtures for the live test suite.

The live suite hits a running aitelier service. It is scope-selected via
the env var contract below, NOT skipped. If you collect a live test, it
must pass — environmental shortcomings fail the test, not skip it.

Test selection:
- `AITELIER_LIVE_URL` unset → live/ dir is not collected (collect_ignore).
- `AITELIER_LIVE_URL` set    → live tests run; everything they need must work.

See ./README.md for the consumer contract.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

# Test selection. Pytest reads `collect_ignore` from conftest at collection
# time; this is not a skip — pytest simply doesn't see these files. The
# unit suite (`make test`) doesn't set AITELIER_LIVE_URL, so the live tests
# never appear there. The live targets (`make test-live`,
# `make test-brig-mode-e2e`) DO set it and require everything to work.
if not os.environ.get("AITELIER_LIVE_URL"):
    collect_ignore_glob = ["*"]


@pytest.fixture(scope="session")
def base_url() -> str:
    url = os.environ.get("AITELIER_LIVE_URL", "http://localhost:7777")
    # Fail loudly + early if the service isn't up. We use pytest.exit so
    # the entire session aborts with a clean message rather than each test
    # producing a confusing connection-error stack.
    try:
        r = httpx.get(f"{url}/v1/health", timeout=3, headers=_live_auth_headers())
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(f"AITELIER_LIVE_URL={url} not reachable: {exc}")
    return url


def _live_auth_headers() -> dict[str, str]:
    """Headers injected on every live-test request.

    `AITELIER_LIVE_BEARER` is the brig ingress bearer token (brig's
    reverse proxy requires `Authorization: Bearer <token>` on every
    request). Unset for Docker/host deploys where the service is hit
    directly.
    """
    bearer = os.environ.get("AITELIER_LIVE_BEARER")
    return {"Authorization": f"Bearer {bearer}"} if bearer else {}


@pytest.fixture(scope="session")
def http(base_url):
    with httpx.Client(base_url=base_url, timeout=120,
                      headers=_live_auth_headers()) as c:
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

    Sandbox Agent's `mock` backend echoes the request back rather than
    running a real session — useful for protocol probes but useless for
    end-to-end behavior. Prefer real backends; fall back to mock for
    cases that only need the request to reach SA. If SA advertises no
    backends at all, the live deployment is misconfigured — fail the
    fixture loudly rather than skipping every dependent test.
    """
    assert sa_agents, (
        "/v1/discovery reports no sandbox-agent backends — SA is misconfigured "
        "or unreachable. Confirm SA is running and at least one agent is "
        "installable (claude, codex, mock, ...)."
    )
    for preferred in ("claude", "codex", "mock"):
        if preferred in sa_agents:
            return preferred
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


def assert_upstream_ok(r) -> None:
    """Replacement for the old `skip_on_upstream_unavailable`. If the live
    target is collected, every upstream the test exercises must work —
    401/403/429/503/504 indicate misconfigured creds, exhausted rate
    limits, or genuine upstream outages, all of which should fail the
    test in this strict-mode suite. Provides a more useful failure
    message than the bare assertion."""
    if r.status_code != 200:
        raise AssertionError(
            f"upstream returned HTTP {r.status_code}: {r.text}\n"
            f"In strict mode the live suite treats this as a real failure. "
            f"Common causes:\n"
            f"  401/403 → missing or invalid provider API key "
            f"(check aitelier.secrets.toml / docker/.env / `claude login`)\n"
            f"  429     → rate-limited by the provider (retry, or use a "
            f"different account)\n"
            f"  500     → aitelier bug — check the service logs\n"
            f"  502/504 → upstream timeout / gateway error\n"
        )
