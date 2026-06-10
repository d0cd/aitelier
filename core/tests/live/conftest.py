"""Shared fixtures for the live test suite.

The live suite hits a running aitelier service. It is scope-selected via
the env var contract below, NOT skipped. If you collect a live test, it
must pass — environmental shortcomings fail the test, not skip it.

Test selection:
- `AITELIER_LIVE_URL` unset → live/ dir is not collected (collect_ignore).
- `AITELIER_LIVE_URL` set    → live tests run; everything they need must work.

Agent-test parameterization: tests that take an `agent_backend`
parameter are parameterized over SA backends. Default
`--agent-matrix=curated` runs the three backends that the local
deployment has credentials/CLI for (claude, codex, opencode).
`--agent-matrix=full` runs every backend advertised by /v1/discovery
— catches per-backend regressions but expects every backend to be
runnable, which on this machine isn't the case for amp/cursor/pi.

See ./README.md for the consumer contract.
"""

from __future__ import annotations

import http.server as _http_server
import json
import os
import threading
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


# ---------------------------------------------------------------------------
# Agent-backend parameterization (collection time)
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--agent-matrix",
        action="store",
        default="curated",
        choices=("curated", "full"),
        help=(
            "Which SA backends to parameterize agent tests over. "
            "`curated` = claude only (fast, default); "
            "`full` = every backend /v1/discovery advertises."
        ),
    )


_AGENT_BACKENDS_CACHE: list[str] | None = None


def _discover_sa_agents() -> list[str]:
    """One-shot discovery probe for the agent_backend parametrization.

    Called at collection time, before fixtures exist. Caches the result so
    repeated parametrize() calls don't repeatedly hit the service.
    """
    global _AGENT_BACKENDS_CACHE
    if _AGENT_BACKENDS_CACHE is not None:
        return _AGENT_BACKENDS_CACHE
    url = os.environ.get("AITELIER_LIVE_URL")
    if not url:
        # collect_ignore_glob already kicked in; this path shouldn't fire.
        return []
    bearer = os.environ.get("AITELIER_LIVE_BEARER")
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    try:
        r = httpx.get(f"{url}/v1/discovery", timeout=5, headers=headers)
        r.raise_for_status()
        d = r.json()
        agents = d.get("dependencies", {}).get("sandbox_agent", {}).get("agents") or []
    except Exception as exc:
        pytest.exit(
            f"Failed to discover SA backends at {url}/v1/discovery: {exc}\n"
            f"Cannot parameterize agent tests."
        )
    _AGENT_BACKENDS_CACHE = sorted(agents)
    return _AGENT_BACKENDS_CACHE


def pytest_generate_tests(metafunc):
    """Parameterize tests that take `agent_backend` over SA-advertised
    backends, gated by `--agent-matrix` for runtime control."""
    if "agent_backend" not in metafunc.fixturenames:
        return
    advertised = _discover_sa_agents()
    assert advertised, (
        "/v1/discovery reports no sandbox-agent backends — SA is "
        "misconfigured. Cannot parameterize agent tests."
    )
    matrix = metafunc.config.getoption("--agent-matrix")
    if matrix == "full":
        backends = advertised
    else:
        # Curated trio (host default). Deploy scripts set
        # AITELIER_LIVE_AGENT_BACKENDS to override per-mode — brig's
        # image only pre-bakes claude, docker only ships claude, etc.
        # Filter to what SA advertises so a missing backend silently
        # drops rather than failing all dependent tests.
        env_override = os.environ.get("AITELIER_LIVE_AGENT_BACKENDS")
        if env_override:
            curated = [b.strip() for b in env_override.split(",") if b.strip()]
        else:
            curated = ["claude", "codex", "opencode"]
        backends = [b for b in curated if b in advertised]
        if not backends:
            # No curated backend available — fall back to whatever SA
            # has so the suite still runs against *something*.
            backends = [advertised[0]]
    metafunc.parametrize("agent_backend", backends,
                         ids=lambda b: f"backend={b}")


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


# ---------------------------------------------------------------------------
# Webhook receiver fixture
# ---------------------------------------------------------------------------


class _WebhookReceiver:
    """Tiny in-process HTTP server that records POSTs for webhook tests.

    Tests fire an aitelier run with `webhook_url=<receiver.url>`, then call
    `receiver.wait_for(run_id=...)` to block until the matching delivery
    lands. Headers are preserved (so signature-verification tests can
    pluck `X-Aitelier-Signature` etc.).

    Listens on 127.0.0.1:<random-free-port>. Compatible with aitelier
    deployments that can reach the host loopback (host + brig with
    aitelier-on-host). Docker-mode tests need host.docker.internal —
    set `AITELIER_WEBHOOK_RECEIVER_HOST` to override the URL hostname.
    """

    def __init__(self):
        self.received: list[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        # Bind to 0.0.0.0 so docker containers can reach us via the host's
        # bridge IP; the .url property reports the resolvable hostname.
        self._server = _http_server.HTTPServer(("0.0.0.0", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever,
                                         daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]

    @property
    def url(self) -> str:
        host = os.environ.get("AITELIER_WEBHOOK_RECEIVER_HOST", "127.0.0.1")
        return f"http://{host}:{self.port}"

    def _handler(self):
        receiver = self

        class Handler(_http_server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — stdlib API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                with receiver._lock:
                    receiver.received.append({
                        "path": self.path,
                        "headers": {k: v for k, v in self.headers.items()},
                        "body": body,
                        "json": _maybe_json(body),
                        "received_at": time.monotonic(),
                    })
                receiver._wake.set()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args, **_kwargs):  # silence default logging
                pass

        return Handler

    def wait_for(self, *, run_id: str | None = None,
                 predicate=None, timeout: float = 30.0) -> dict:
        """Block until a matching POST arrives. Match by `run_id` (the
        webhook payload's aitelier_run_id) or by an arbitrary predicate."""
        if predicate is None and run_id is not None:
            def predicate(rec, _run_id=run_id):
                body = rec.get("json") or {}
                return body.get("aitelier_run_id") == _run_id
        assert predicate is not None, "wait_for needs run_id or predicate"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for rec in self.received:
                    if predicate(rec):
                        return rec
            self._wake.wait(0.1)
            self._wake.clear()
        with self._lock:
            seen = [r["json"] for r in self.received]
        raise AssertionError(
            f"no matching webhook within {timeout}s. "
            f"Received {len(seen)} so far: {seen}"
        )

    def clear(self) -> None:
        with self._lock:
            self.received.clear()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _maybe_json(body: bytes) -> dict | None:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


@pytest.fixture(scope="session")
def webhook_receiver():
    """Session-scoped webhook receiver. Tests should call `.clear()`
    before firing their webhook to keep `received` un-cluttered."""
    rcv = _WebhookReceiver()
    try:
        yield rcv
    finally:
        rcv.shutdown()


# ---------------------------------------------------------------------------
# isolated_aitelier — spawns an aitelier subprocess with custom config
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _discover_host_sa_url() -> str:
    """Read the host's actual SA base_url from runs/.session.toml (written
    by scripts/start.sh on dynamic-port pick). Falls back to the default
    2468 if the session overlay isn't present."""
    repo_root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    session_path = os.path.join(repo_root, "runs", ".session.toml")
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    try:
        with open(session_path, "rb") as f:
            data = tomllib.load(f)
        url = data.get("sandbox_agent", {}).get("base_url")
        if url:
            return url
    except (FileNotFoundError, OSError):
        pass
    return "http://127.0.0.1:2468"


class _IsolatedAitelier:
    """Subprocess aitelier on a random port with caller-supplied TOML
    config. Shares Postgres + LiteLLM with the main deployment but lets
    individual tests override [service] knobs like `api_key`,
    `webhook_secret`, `allow_loopback_webhooks`.

    Started in a temp cwd so the repo's `runs/.session.toml` overlay
    doesn't bleed in. Tears down on context exit.
    """

    def __init__(self, *, service_overrides: dict | None = None,
                 extra_toml: str = ""):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.service_overrides = dict(service_overrides or {})
        self.extra_toml = extra_toml
        self._proc = None
        self._config_path: str | None = None
        self._log_path: str | None = None
        self._cwd: str | None = None

    def _render_toml(self) -> str:
        # Default service stanza — port + host. Tests stack additional
        # service.* overrides on top.
        service_lines = [
            'host = "127.0.0.1"',
            f"port = {self.port}",
        ]
        for k, v in self.service_overrides.items():
            if isinstance(v, bool):
                service_lines.append(f"{k} = {str(v).lower()}")
            elif isinstance(v, (int, float)):
                service_lines.append(f"{k} = {v}")
            else:
                service_lines.append(f'{k} = "{v}"')
        # `[sandbox_agent].base_url` — the host machine's SA may be on a
        # dynamic port (`make start` writes the chosen port to
        # `runs/.session.toml`). The isolated aitelier runs in a temp
        # cwd, so we have to read that file ourselves and embed the
        # discovered URL directly.
        sa_url = _discover_host_sa_url()
        return f"""\
[database]
url = "postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier"

[litellm]
base_url = "http://127.0.0.1:4000"

[sandbox_agent]
base_url = "{sa_url}"

[service]
{chr(10).join(service_lines)}

{self.extra_toml}
"""

    def start(self) -> None:
        import subprocess
        import tempfile
        import textwrap as _tw  # noqa: F401  (silences import-not-used in some hooks)

        fd, self._config_path = tempfile.mkstemp(
            prefix="aitelier-isolated-", suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write(self._render_toml())
        log_fd, self._log_path = tempfile.mkstemp(
            prefix="aitelier-isolated-log-", suffix=".txt")
        os.close(log_fd)
        self._cwd = tempfile.mkdtemp(prefix="aitelier-isolated-cwd-")

        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        core_path = os.path.join(repo_root, "core")

        self._proc = subprocess.Popen(
            [
                "uv", "run", "--project", core_path,
                "aitelier", "--config", self._config_path,
                "serve", "--host", "127.0.0.1", "--port", str(self.port),
            ],
            cwd=self._cwd,
            stdout=open(self._log_path, "w"),
            stderr=subprocess.STDOUT,
        )

        # Poll /v1/health until ready or process dies.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                with open(self._log_path) as f:
                    log = f.read()
                raise RuntimeError(
                    f"aitelier subprocess died during startup. Log:\n{log}"
                )
            try:
                r = httpx.get(f"{self.base_url}/v1/health", timeout=2)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        with open(self._log_path) as f:
            log = f.read()
        self.stop()
        raise TimeoutError(
            f"isolated aitelier didn't come up within 30s. Log:\n{log}"
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        # Best-effort cleanup; ignore errors.
        for path in (self._config_path, self._log_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if self._cwd and os.path.exists(self._cwd):
            import shutil
            shutil.rmtree(self._cwd, ignore_errors=True)


@pytest.fixture
def isolated_aitelier():
    """Factory fixture. Tests call `isolated_aitelier(service={...})` to
    get a fresh aitelier subprocess with the given [service] overrides.
    Auto-torn-down at test end."""
    instances: list[_IsolatedAitelier] = []

    def _factory(service: dict | None = None,
                 extra_toml: str = "") -> _IsolatedAitelier:
        inst = _IsolatedAitelier(service_overrides=service,
                                  extra_toml=extra_toml)
        inst.start()
        instances.append(inst)
        return inst

    try:
        yield _factory
    finally:
        for inst in instances:
            inst.stop()


# ---------------------------------------------------------------------------


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
