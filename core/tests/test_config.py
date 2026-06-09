"""Tests for layered configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from aitelier.config import load_config


def test_load_defaults(tmp_path, monkeypatch):
    # No file = pure defaults. Run from a clean cwd so the test doesn't pick
    # up the repo's aitelier.toml (which would shadow the defaults).
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.litellm.base_url == "http://localhost:4000"
    assert cfg.litellm.api_key == "sk-litellm-local"
    assert cfg.sandbox_agent.base_url == "http://localhost:2468"
    assert cfg.sandbox_agent.token is None
    assert cfg.service.port == 7777
    assert cfg.runs_dir == "runs"


def test_explicit_path_missing_raises():
    with pytest.raises(FileNotFoundError):
        load_config(Path("/definitely/not/here.toml"))


def test_load_from_file(tmp_path, monkeypatch):
    # Isolate from the repo's runs/.session.toml — load_config layers it on
    # top of every config when cwd contains a runs/ dir with that file.
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
runs_dir = "/tmp/runs"

[litellm]
base_url = "http://myhost:5000"
api_key = "sk-custom"

[sandbox_agent]
base_url = "http://myhost:9090"

[service]
port = 8888
""")
    cfg = load_config(config_file)
    assert cfg.litellm.base_url == "http://myhost:5000"
    assert cfg.litellm.api_key == "sk-custom"
    assert cfg.sandbox_agent.base_url == "http://myhost:9090"
    assert cfg.service.port == 8888
    assert cfg.runs_dir == "/tmp/runs"


def test_secrets_overlay_overrides_base(tmp_path, monkeypatch):
    """Secrets file alongside the base config wins on conflicting keys."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("""
[litellm]
api_key = "sk-from-base"
""")
    (tmp_path / "aitelier.secrets.toml").write_text("""
[litellm]
api_key = "sk-from-secrets"

[service]
api_key = "service-bearer-token"
""")
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.litellm.api_key == "sk-from-secrets"
    assert cfg.service.api_key == "service-bearer-token"


def test_session_overlay_wins_over_base_and_secrets(tmp_path, monkeypatch):
    """Layer 4 (runs/.session.toml) wins. start.sh writes it with dynamic values
    (chosen sandbox-agent port, dev DSN) that must override aitelier.toml."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aitelier.toml").write_text("""
[sandbox_agent]
base_url = "http://localhost:2468"

[database]
url = "postgresql://from-base/db"
""")
    (tmp_path / "aitelier.secrets.toml").write_text("""
[database]
url = "postgresql://from-secrets/db"
""")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / ".session.toml").write_text("""
[sandbox_agent]
base_url = "http://127.0.0.1:54321"

[database]
url = "postgresql://from-session/db"
""")
    cfg = load_config()
    assert cfg.sandbox_agent.base_url == "http://127.0.0.1:54321"
    assert cfg.database.url == "postgresql://from-session/db"


def test_env_vars_are_not_read(tmp_path, monkeypatch):
    """The principled invariant: load_config does not read os.environ. Setting
    historically-supported env vars must not affect the loaded Config."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "DATABASE_URL", "LITELLM_BASE_URL", "LITELLM_API_KEY",
        "SANDBOX_AGENT_BASE_URL", "SANDBOX_TOKEN",
        "AITELIER_HOST", "AITELIER_PORT", "AITELIER_RUNS_DIR",
        "AITELIER_API_KEY", "AITELIER_LOG_FORMAT", "AITELIER_BASE_URL",
    ):
        monkeypatch.setenv(name, f"value-from-{name.lower()}")
    cfg = load_config()
    # Every field stays at its dataclass default — env was ignored.
    assert cfg.database.url is None
    assert cfg.litellm.base_url == "http://localhost:4000"
    assert cfg.litellm.api_key == "sk-litellm-local"
    assert cfg.sandbox_agent.base_url == "http://localhost:2468"
    assert cfg.sandbox_agent.token is None
    assert cfg.service.host == "127.0.0.1"
    assert cfg.service.port == 7777
    assert cfg.service.log_format == "human"
