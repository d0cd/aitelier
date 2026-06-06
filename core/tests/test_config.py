"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

from aitelier.config import load_config


def test_load_defaults():
    cfg = load_config(Path("/nonexistent"))
    assert cfg.litellm.base_url == "http://localhost:4000"
    assert cfg.litellm.api_key == "sk-litellm-local"
    assert cfg.sandbox_agent.base_url == "http://localhost:2468"
    assert cfg.sandbox_agent.token is None
    assert cfg.service.port == 7777
    assert cfg.runs_dir == "runs"


def test_load_from_file(tmp_path):
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


def test_env_overrides_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[litellm]
base_url = "http://from-file:4000"
api_key = "sk-from-file"
""")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://from-env:4000")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-from-env")

    cfg = load_config(config_file)
    assert cfg.litellm.base_url == "http://from-env:4000"
    assert cfg.litellm.api_key == "sk-from-env"
