"""Behavioral tests for the startup credential boundary."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "materialize_credentials.py"


def _run_materializer(tmp_path: Path, token: str):
    home = tmp_path / "home"
    home.mkdir()
    env_file = tmp_path / "docker" / ".env"
    token_file = tmp_path / "runs" / ".claude-oauth-token"
    fingerprint_file = tmp_path / "runs" / ".claude-oauth-token.sha256"
    env = os.environ.copy()
    env.update({"HOME": str(home), "CLAUDE_CODE_OAUTH_TOKEN": token})
    env.pop("AITELIER_OLLAMA_PROFILE", None)
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            str(env_file),
            str(token_file),
            str(fingerprint_file),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result, env_file, token_file, fingerprint_file


def test_materializer_writes_private_files_without_printing_secrets(tmp_path):
    token = "sk-ant-oat01-FAKE9Qx7Lm2Vn8Rk4Wp6Zt3Hs5Jc"
    result, env_file, token_file, fingerprint_file = _run_materializer(tmp_path, token)

    assert token not in result.stdout
    assert token not in result.stderr
    assert token_file.read_text() == token + "\n"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    assert fingerprint_file.read_text() == fingerprint + "\n"
    assert f"ANTHROPIC_API_KEY={token}\n" in env_file.read_text()
    assert f"CLAUDE_SETUP_TOKEN_FINGERPRINT={fingerprint}\n" in env_file.read_text()
    for path in (env_file, token_file, fingerprint_file):
        assert path.stat().st_mode & 0o777 == 0o600


def test_materializer_replaces_symlink_instead_of_writing_through_it(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("do not overwrite\n")
    env_link = tmp_path / "docker" / ".env"
    env_link.parent.mkdir()
    env_link.symlink_to(victim)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(home), "CLAUDE_CODE_OAUTH_TOKEN": ""})
    subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            str(env_link),
            str(tmp_path / "runs" / "token"),
            str(tmp_path / "runs" / "fingerprint"),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert victim.read_text() == "do not overwrite\n"
    assert not env_link.is_symlink()
    assert env_link.stat().st_mode & 0o777 == 0o600
