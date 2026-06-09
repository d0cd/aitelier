"""Configuration — single source for all connection info and settings.

Loading is layered (each layer overrides keys in earlier layers):

  1. Defaults (dataclass field defaults)
  2. Base config — explicit `--config <path>`, else the first of:
       ./aitelier.toml                       (repo-local)
       ~/.config/aitelier/config.toml        (user-global)
  3. Secrets overlay — `aitelier.secrets.toml` in the same directory as the
     base config (gitignored). Optional. Same TOML shape; keys present here
     override base. Use this for api_keys, tokens, and other secrets.
  4. Session overlay — `runs/.session.toml` (gitignored, ephemeral). Used by
     scripts/start.sh to communicate dynamic values (e.g. the actual port
     sandbox-agent picked) without polluting the environment. Cleaned up
     by scripts/stop.sh.

The codebase reads `get_config()`. No `os.environ` reads anywhere — that's
the principled invariant. If you need to override config per-invocation,
use a CLI flag or write to one of the overlays.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_SEARCH_PATHS = [
    Path("aitelier.toml"),                                # repo-local
    Path.home() / ".config" / "aitelier" / "config.toml",  # user-global
]
_SESSION_OVERLAY = Path("runs/.session.toml")
_SECRETS_FILENAME = "aitelier.secrets.toml"


@dataclass
class LiteLLMConfig:
    base_url: str = "http://localhost:4000"
    api_key: str = "sk-litellm-local"


@dataclass
class SandboxAgentConfig:
    base_url: str = "http://localhost:2468"
    token: str | None = None


@dataclass
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 7777
    api_key: str | None = None
    """If set, every /v1/* endpoint (except /v1/health) requires
    Authorization: Bearer <api_key>. Unset = localhost-trust mode."""
    webhook_secret: str | None = None
    """Shared secret for signing outbound webhook deliveries with
    X-Aitelier-Signature: sha256=<hmac>. Unset = unsigned. Set this in
    aitelier.secrets.toml in hosted-mode deployments."""
    log_format: str = "human"
    """`json` → one-line JSON per record (aggregator-friendly).
    anything else → human-readable with [correlation_id] prefix."""
    max_in_flight_runs: int = 32
    """Per-process cap on concurrent agent + LLM runs. Requests beyond
    this return HTTP 503 with `error.type: "ProviderUnavailable"`. Set
    to 0 to disable the cap (single-tenant dev deployments)."""
    allow_loopback_webhooks: bool = False
    """When false (default), `webhook_url` values pointing at loopback /
    private / link-local addresses are rejected — independent of
    `api_key`. Flip on only for dev workflows where aitelier and the
    webhook target both live on `localhost`. Setting it disables the
    SSRF guard entirely, so flip it off again before exposing the port."""
    max_request_body_bytes: int = 4 * 1024 * 1024
    """Per-request body size cap. Requests with Content-Length above
    this return 413 before any handler runs, blocking memory-exhaustion
    DoS via large POST bodies into idempotency hashing or JSON parsing.
    Set to 0 to disable (only safe when a reverse proxy enforces a cap)."""
    rate_limit_per_minute: int = 0
    """In-process token bucket keyed by (api_key or remote_addr). 0 =
    disabled (default; appropriate for localhost-trust mode). For hosted
    deployments to multiple callers, set to a reasonable per-key budget
    (e.g. 600). Returns 429 with Retry-After when exceeded. Excludes
    /v1/health."""


@dataclass
class DatabaseConfig:
    url: str | None = None
    """Postgres DSN, e.g. postgresql://aitelier:aitelier_local@localhost:5433/aitelier.
    None = fall back to InMemoryStore (dev only — no durable state)."""


@dataclass
class PurgeConfig:
    interval_seconds: int = 3600
    """How often the background purge worker wakes up to clean expired
    idempotency keys, terminal webhook deliveries, and old run events.
    Set to 0 to disable the worker entirely (the startup purge of
    `runs` older than 30 days still runs)."""
    webhook_retention_days: int = 7
    """Drop webhook_deliveries rows in terminal states (delivered,
    failed) older than this. Pending rows are kept regardless — they're
    still waiting to be sent or have given up retrying."""
    event_retention_days: int = 30
    """Drop run_events older than this. Independent of the run row
    purge; events for runs that survive the purge window still age out
    on this clock."""
    run_retention_days: int = 30
    """Drop runs older than this on aitelier startup. Independent from
    event retention so operators can keep events around longer for
    auditing than the rows that pointed at them."""


@dataclass
class StorageConfig:
    max_metadata_bytes: int = 64 * 1024
    """Cap on the JSON-encoded size of run metadata. Aitelier persists the
    whole dict as JSONB in Postgres; without a bound, a buggy consumer can
    bloat the runs table. 64 KB is well above typical use (correlation_id +
    a few tags ~= <500 bytes)."""


@dataclass
class OllamaConfig:
    mode: str = "host"
    """Where Ollama runs:
      - "host":   `brew install ollama` / `ollama serve` on the dev machine.
                  LiteLLM reaches it at host.docker.internal:11434. The
                  Mac default — needed for Metal/MPS GPU access.
      - "docker": containerized Ollama as a compose service (profile=ollama).
                  CPU-only on Mac (no GPU passthrough). Linux+NVIDIA needs
                  the deploy.resources block uncommented in compose.
    """
    base_url: str | None = None
    """Override the resolved API base. Defaults follow `mode`:
       host   → http://host.docker.internal:11434
       docker → http://ollama:11434
    aitelier itself runs on host, so for our bypass adapter we use
    `host_base_url` which substitutes `host.docker.internal` → `127.0.0.1`.
    """
    default_model: str = "qwen3:8b"
    """Which Ollama model the `local` alias resolves to in our direct
    adapter (bypassing LiteLLM). Keep aligned with the `local` mapping in
    docker/litellm/config.yaml so `local` behaves consistently regardless
    of which path serves it.
    """

    def host_base_url(self) -> str:
        """Resolve a base URL aitelier itself can reach. `host.docker.internal`
        only works inside a container; from the host process we use
        127.0.0.1 instead.
        """
        if self.base_url:
            # Honor an explicit override, but rewrite the docker-internal
            # hostname when aitelier isn't running inside a container.
            return self.base_url.replace(
                "host.docker.internal", "127.0.0.1",
            ).rstrip("/")
        return "http://127.0.0.1:11434"


@dataclass
class Config:
    litellm: LiteLLMConfig = field(default_factory=LiteLLMConfig)
    sandbox_agent: SandboxAgentConfig = field(default_factory=SandboxAgentConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    purge: PurgeConfig = field(default_factory=PurgeConfig)
    runs_dir: str = "runs"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base (overlay wins). Returns a new dict."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"failed to parse {path}: {e}") from e


def load_config(path: Path | None = None) -> Config:
    """Load layered config. See module docstring for layer order."""
    # Layer 2: base config
    base_path: Path | None = None
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"--config path does not exist: {path}")
        base_path = path
    else:
        for candidate in _DEFAULT_SEARCH_PATHS:
            if candidate.exists():
                base_path = candidate
                break

    raw: dict = {}
    if base_path is not None:
        raw = _read_toml(base_path)
        # Layer 3: secrets overlay alongside base
        secrets_path = base_path.parent / _SECRETS_FILENAME
        if secrets_path.exists():
            raw = _deep_merge(raw, _read_toml(secrets_path))

    # Layer 4: session overlay (always at runs/.session.toml relative to cwd)
    if _SESSION_OVERLAY.exists():
        raw = _deep_merge(raw, _read_toml(_SESSION_OVERLAY))

    def _section(name: str) -> dict:
        return raw.get(name, {}) if isinstance(raw.get(name), dict) else {}

    litellm = _section("litellm")
    sandbox_agent = _section("sandbox_agent")
    service = _section("service")
    ollama = _section("ollama")
    database = _section("database")
    storage = _section("storage")

    return Config(
        litellm=LiteLLMConfig(
            base_url=litellm.get("base_url", LiteLLMConfig.base_url),
            api_key=litellm.get("api_key", LiteLLMConfig.api_key),
        ),
        sandbox_agent=SandboxAgentConfig(
            base_url=sandbox_agent.get("base_url", SandboxAgentConfig.base_url),
            token=sandbox_agent.get("token", SandboxAgentConfig.token),
        ),
        service=ServiceConfig(
            host=service.get("host", ServiceConfig.host),
            port=service.get("port", ServiceConfig.port),
            api_key=service.get("api_key", ServiceConfig.api_key),
            webhook_secret=service.get("webhook_secret", ServiceConfig.webhook_secret),
            log_format=service.get("log_format", ServiceConfig.log_format),
            max_in_flight_runs=service.get(
                "max_in_flight_runs", ServiceConfig.max_in_flight_runs,
            ),
            allow_loopback_webhooks=service.get(
                "allow_loopback_webhooks",
                ServiceConfig.allow_loopback_webhooks,
            ),
            max_request_body_bytes=service.get(
                "max_request_body_bytes", ServiceConfig.max_request_body_bytes,
            ),
            rate_limit_per_minute=service.get(
                "rate_limit_per_minute", ServiceConfig.rate_limit_per_minute,
            ),
        ),
        ollama=OllamaConfig(
            mode=ollama.get("mode", OllamaConfig.mode),
            base_url=ollama.get("base_url", OllamaConfig.base_url),
            default_model=ollama.get("default_model", OllamaConfig.default_model),
        ),
        database=DatabaseConfig(
            url=database.get("url", DatabaseConfig.url),
        ),
        storage=StorageConfig(
            max_metadata_bytes=storage.get("max_metadata_bytes", StorageConfig.max_metadata_bytes),
        ),
        purge=PurgeConfig(
            interval_seconds=_section("purge").get(
                "interval_seconds", PurgeConfig.interval_seconds,
            ),
            webhook_retention_days=_section("purge").get(
                "webhook_retention_days", PurgeConfig.webhook_retention_days,
            ),
            event_retention_days=_section("purge").get(
                "event_retention_days", PurgeConfig.event_retention_days,
            ),
            run_retention_days=_section("purge").get(
                "run_retention_days", PurgeConfig.run_retention_days,
            ),
        ),
        runs_dir=raw.get("runs_dir", "runs"),
    )


# Module-level singleton — loaded once, used everywhere
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    """Replace the singleton. Called by the CLI after parsing --config."""
    global _config
    _config = cfg


def reset_config() -> None:
    """Force the next get_config() call to reload from disk. For tests."""
    global _config
    _config = None
