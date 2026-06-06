"""Configuration — single source for all connection info and settings."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_CONFIG_SEARCH_PATHS = [
    Path("aitelier.toml"),                          # repo-local
    Path.home() / ".config" / "aitelier" / "config.toml",  # user-global
]


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


@dataclass
class Config:
    litellm: LiteLLMConfig = field(default_factory=LiteLLMConfig)
    sandbox_agent: SandboxAgentConfig = field(default_factory=SandboxAgentConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    runs_dir: str = "runs"


def load_config(path: Path | None = None) -> Config:
    """Load config from file, then overlay env vars.

    Search order:
    1. Explicit path argument
    2. ./aitelier.toml (repo-local)
    3. ~/.config/aitelier/config.toml (user-global)
    4. Defaults

    Env vars override file values:
        LITELLM_BASE_URL, LITELLM_API_KEY,
        SANDBOX_AGENT_BASE_URL,
        AITELIER_HOST, AITELIER_PORT, AITELIER_RUNS_DIR
    """
    raw: dict = {}

    if path and path.exists():
        raw = tomllib.loads(path.read_text())
    else:
        for search_path in _CONFIG_SEARCH_PATHS:
            if search_path.exists():
                raw = tomllib.loads(search_path.read_text())
                break

    cfg = Config(
        litellm=LiteLLMConfig(
            base_url=raw.get("litellm", {}).get("base_url", LiteLLMConfig.base_url),
            api_key=raw.get("litellm", {}).get("api_key", LiteLLMConfig.api_key),
        ),
        sandbox_agent=SandboxAgentConfig(
            base_url=raw.get("sandbox_agent", {}).get("base_url", SandboxAgentConfig.base_url),
            token=raw.get("sandbox_agent", {}).get("token", SandboxAgentConfig.token),
        ),
        service=ServiceConfig(
            host=raw.get("service", {}).get("host", ServiceConfig.host),
            port=raw.get("service", {}).get("port", ServiceConfig.port),
        ),
        runs_dir=raw.get("runs_dir", "runs"),
    )

    # Env var overrides (secrets and per-machine values)
    if v := os.environ.get("LITELLM_BASE_URL"):
        cfg.litellm.base_url = v
    if v := os.environ.get("LITELLM_API_KEY"):
        cfg.litellm.api_key = v
    if v := os.environ.get("SANDBOX_AGENT_BASE_URL"):
        cfg.sandbox_agent.base_url = v
    if v := os.environ.get("SANDBOX_TOKEN"):
        cfg.sandbox_agent.token = v
    if v := os.environ.get("AITELIER_HOST"):
        cfg.service.host = v
    if v := os.environ.get("AITELIER_PORT"):
        cfg.service.port = int(v)
    if v := os.environ.get("AITELIER_RUNS_DIR"):
        cfg.runs_dir = v

    return cfg


# Module-level singleton — loaded once, used everywhere
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config
