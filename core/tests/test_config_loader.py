"""Round-trip test for the generic config loader.

Every section's fields must be reachable from TOML — so a wiring regression (a
section or field dropped from load_config) fails CI instead of silently ignoring
the operator's TOML value.
"""

from __future__ import annotations

import textwrap

import pytest
from aitelier.config import load_config


@pytest.fixture(autouse=True)
def _no_session_overlay(tmp_path, monkeypatch):
    """load_config always merges runs/.session.toml relative to cwd; point it at
    a nonexistent path so a running dev session can't pollute these assertions."""
    monkeypatch.setattr("aitelier.config._SESSION_OVERLAY", tmp_path / "no-session.toml")


def test_every_section_round_trips_from_toml(tmp_path):
    toml = textwrap.dedent("""
        runs_dir = "custom-runs"
        [litellm]
        base_url = "http://litellm-test:9"
        api_key = "sk-test"
        [sandbox_agent]
        mode = "remote"
        token = "sa-tok"
        [service]
        port = 9999
        rate_limit_per_minute = 600
        allowed_workspace_roots = ["/srv/ws"]
        [ollama]
        default_model = "qwen-test"
        [database]
        url = "postgresql://test/db"
        [storage]
        max_metadata_bytes = 4096
        [purge]
        run_retention_days = 99
        [otel]
        enabled = true
    """)
    p = tmp_path / "aitelier.toml"
    p.write_text(toml)
    cfg = load_config(p)

    assert cfg.litellm.base_url == "http://litellm-test:9"
    assert cfg.litellm.api_key == "sk-test"
    assert cfg.sandbox_agent.mode == "remote"
    assert cfg.sandbox_agent.token == "sa-tok"
    assert cfg.service.port == 9999
    assert cfg.service.rate_limit_per_minute == 600
    assert cfg.service.allowed_workspace_roots == ["/srv/ws"]
    assert cfg.ollama.default_model == "qwen-test"
    assert cfg.database.url == "postgresql://test/db"
    assert cfg.storage.max_metadata_bytes == 4096
    assert cfg.purge.run_retention_days == 99
    assert cfg.otel.enabled is True
    assert cfg.runs_dir == "custom-runs"


def test_unknown_toml_keys_are_ignored_not_errors(tmp_path):
    """An unrecognized key under a section is filtered to the dataclass fields,
    not passed to the constructor (which would TypeError)."""
    p = tmp_path / "aitelier.toml"
    p.write_text("[service]\nport = 8080\nbogus_unknown_key = 1\n")
    cfg = load_config(p)
    assert cfg.service.port == 8080
