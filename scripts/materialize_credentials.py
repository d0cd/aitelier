"""Materialize local provider credentials for ``scripts/start.sh``.

Credential discovery is intentionally isolated from service startup so the
secret-handling boundary is small and directly testable.  Nothing in this
module prints credential values.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _write_private_text(path: Path, value: str) -> None:
    """Atomically replace ``path`` with a mode-600 regular file.

    ``Path.write_text(...); chmod(...)`` briefly exposes a newly-created
    secret according to the caller's umask and follows an existing symlink.
    A private sibling temporary file plus ``os.replace`` avoids both issues.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def materialize(env_file: Path, setup_token_file: Path, fingerprint_file: Path) -> None:
    anthropic_key = ""
    openai_key = ""

    # A setup token is long-lived and is also what the Brig-hosted Claude
    # agent consumes. LiteLLM recognizes its sk-ant-oat prefix and sends it as
    # an OAuth bearer token, so direct and agent routes can share one source.
    setup_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    setup_source = "CLAUDE_CODE_OAUTH_TOKEN"
    brig_setup_token = Path.home() / ".brig" / "secrets" / "claude-oauth-token"
    if not setup_token and brig_setup_token.exists():
        try:
            setup_token = brig_setup_token.read_text().strip()
            setup_source = "brig secret claude-oauth-token"
        except Exception as exc:
            print(f"  - Claude: failed to read brig setup token: {exc}")

    if setup_token and setup_token.startswith("sk-ant-oat"):
        anthropic_key = setup_token
        print(f"  ✓ Claude: long-lived setup token found ({setup_source})")
    elif setup_token:
        print(f"  - Claude: ignored invalid setup token from {setup_source}")

    # Legacy fallback. New Claude Code versions may keep their refreshable
    # login in secure storage without updating this file.
    if not anthropic_key:
        claude_creds = Path.home() / ".claude" / ".credentials.json"
        if claude_creds.exists():
            try:
                oauth = _read_json(claude_creds).get("claudeAiOauth", {})
                token = oauth.get("accessToken", "") if isinstance(oauth, dict) else ""
                expires = oauth.get("expiresAt", 0) if isinstance(oauth, dict) else 0
                if not token:
                    print("  - Claude: credentials file exists but no token found")
                elif expires and expires < time.time() * 1000:
                    print("  - Claude: legacy OAuth snapshot expired")
                else:
                    anthropic_key = token
                    remaining_h = (
                        (expires - time.time() * 1000) / 3_600_000 if expires else 0
                    )
                    print(
                        "  ✓ Claude: legacy OAuth token valid "
                        f"({remaining_h:.0f}h remaining)"
                    )
            except Exception as exc:
                print(f"  - Claude: failed to read credentials: {exc}")

    if not anthropic_key:
        print(
            "  - Claude: no direct-model token; run 'claude setup-token' "
            "and register claude-oauth-token"
        )

    codex_creds = Path.home() / ".codex" / "auth.json"
    if codex_creds.exists():
        try:
            data = _read_json(codex_creds)
            tokens = data.get("tokens", {})
            nested_token = tokens.get("access_token") if isinstance(tokens, dict) else ""
            token = nested_token or data.get("access_token") or data.get("api_key", "")
            if token:
                openai_key = token
                print("  ✓ Codex: token found")
            else:
                print("  - Codex: auth.json exists but no token")
        except Exception as exc:
            print(f"  - Codex: failed to read auth.json: {exc}")
    else:
        print("  - Codex: not logged in — run 'codex login' if needed")

    runtime_setup_token = (
        setup_token if setup_token.startswith("sk-ant-oat") else ""
    )
    fingerprint = (
        hashlib.sha256(runtime_setup_token.encode()).hexdigest()
        if runtime_setup_token
        else "none"
    )
    ollama_base = (
        "http://ollama:11434"
        if os.environ.get("AITELIER_OLLAMA_PROFILE") == "1"
        else "http://host.docker.internal:11434"
    )
    env_lines = [
        f"ANTHROPIC_API_KEY={anthropic_key}",
        f"OPENAI_API_KEY={openai_key}",
        f"CLAUDE_SETUP_TOKEN_FINGERPRINT={fingerprint}",
        "LITELLM_MASTER_KEY=sk-litellm-local",
        f"OLLAMA_BASE_URL={ollama_base}",
    ]

    _write_private_text(env_file, "\n".join(env_lines) + "\n")
    _write_private_text(
        setup_token_file,
        runtime_setup_token + ("\n" if runtime_setup_token else ""),
    )
    _write_private_text(fingerprint_file, fingerprint + "\n")

    print("")
    print("  Available models:")
    print("    • local, nomic-embed-text, ollama/*  (no credential required)")
    if anthropic_key:
        print("    • claude-sonnet, claude-haiku, anthropic/*  (LLM path)")
    else:
        print(
            "    ✗ claude-sonnet, claude-haiku, anthropic/*  "
            "(no Anthropic LLM credential)"
        )
    if openai_key:
        print("    • openai/*  (LLM path)")
    else:
        print("    ✗ openai/*  (no OpenAI LLM credential)")
    print("    • agent:claude / agent:codex / …  (via Sandbox Agent's own logins)")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: materialize_credentials.py ENV_FILE TOKEN_FILE FINGERPRINT_FILE",
            file=sys.stderr,
        )
        return 2
    materialize(*(Path(value) for value in argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
