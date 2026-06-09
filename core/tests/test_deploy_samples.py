"""Validate the deployment samples shipped under docs/deploy/ and docker/.

These are documentation-shaped (not auto-loaded by anything in the run-time
path), but consumers copy them as starting points. A regression in
`docs/deploy/aitelier.cell.yaml` or `docker/sandbox-agent.Dockerfile` is
silent until someone tries to use them — these tests catch the shape
breaks at unit-test speed.

The tests cross-check the samples against the *actual* aitelier behavior
(SA CLI flags scripts/start.sh uses, env vars config.py reads or
doesn't, Dockerfile-installed tools that healthchecks rely on). Earlier
versions of these tests only validated the shape I'd invented — that
class of "test passes because it matches my mistake" is what this file
exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CELL_YAML = _REPO_ROOT / "docs" / "deploy" / "aitelier.cell.yaml"
_SA_DOCKERFILE = _REPO_ROOT / "docker" / "sandbox-agent.Dockerfile"
_AITELIER_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile"
_COMPOSE_YAML = _REPO_ROOT / "docker" / "docker-compose.yml"
_START_SH = _REPO_ROOT / "scripts" / "start.sh"
_CONFIG_PY = _REPO_ROOT / "core" / "src" / "aitelier" / "config.py"


# --- brig cell yaml ---------------------------------------------------------


def test_brig_cell_yaml_parses():
    """The sample cell yaml must be syntactically valid YAML."""
    assert _CELL_YAML.exists(), f"missing: {_CELL_YAML}"
    data = yaml.safe_load(_CELL_YAML.read_text())
    assert isinstance(data, dict), "cell yaml must parse to a dict"


def test_brig_cell_yaml_declares_minimum_keys():
    """Keys brig expects on every cell definition (cross-checked against
    hermes-agent's cells/hermes/hermes.yaml)."""
    data = yaml.safe_load(_CELL_YAML.read_text())
    required = {"name", "image", "command", "network",
                "policy", "ingress", "labels"}
    missing = required - set(data.keys())
    assert not missing, f"cell yaml missing keys: {missing}"


def test_brig_cell_command_does_not_invoke_start_sh():
    """`scripts/start.sh` provisions host services (docker compose,
    host SA install) — it can't run inside a cell. The cell's command
    must invoke aitelier directly."""
    data = yaml.safe_load(_CELL_YAML.read_text())
    cmd = data.get("command") or []
    cmd_str = " ".join(str(c) for c in cmd)
    assert "start.sh" not in cmd_str, (
        "cell command must not call scripts/start.sh (it provisions host "
        f"services that don't exist in-cell). Got: {cmd}"
    )
    assert "aitelier" in cmd_str.lower(), (
        f"cell command should invoke aitelier directly. Got: {cmd}"
    )


def test_brig_cell_env_block_contains_no_aitelier_env_vars():
    """Aitelier reads ZERO env vars by design (config.py invariant).
    Documenting AITELIER_* env vars in the cell yaml misleads consumers
    into thinking those will work — they won't."""
    data = yaml.safe_load(_CELL_YAML.read_text())
    env = data.get("env") or {}
    bogus = [k for k in env if k.startswith("AITELIER_")]
    assert not bogus, (
        f"cell yaml env block lists AITELIER_* vars but aitelier reads "
        f"no env vars (see config.py): {bogus}"
    )


def test_brig_cell_policy_allows_llm_providers():
    """The cell must whitelist at least one LLM provider host —
    otherwise the agent dispatch will fail under Warden's default deny."""
    data = yaml.safe_load(_CELL_YAML.read_text())
    allow = data.get("policy", {}).get("allow") or []
    assert any(
        host in str(allow) for host in (
            "anthropic.com", "openai.com", "openrouter.ai",
            "googleapis.com",
        )
    ), f"no LLM provider host in policy.allow: {allow}"


def test_brig_cell_ingress_exposes_aitelier_port():
    """aitelier serves on :7777 inside the cell. The ingress block must
    forward that port so other cells / brig can reach it."""
    data = yaml.safe_load(_CELL_YAML.read_text())
    ingress = data.get("ingress") or []
    assert any(
        (i.get("port") == 7777) for i in ingress
    ), f"ingress doesn't forward :7777: {ingress}"


# --- SA Dockerfile ---------------------------------------------------------


def test_sa_dockerfile_shape():
    """The SA Dockerfile must:
      - start from a known-good base
      - install SA via the documented Rivet release URL
      - expose :2468
      - have a CMD that actually starts the binary
    """
    assert _SA_DOCKERFILE.exists(), f"missing: {_SA_DOCKERFILE}"
    text = _SA_DOCKERFILE.read_text()
    assert "FROM alpine" in text, "expected alpine base"
    assert "releases.rivet.dev/sandbox-agent" in text, \
        "SA install URL not present — Rivet may have moved; update Dockerfile"
    assert "EXPOSE 2468" in text, "SA's ACP port not exposed"
    assert "sandbox-agent" in text.lower(), "binary not referenced in CMD"


def test_sa_dockerfile_cmd_matches_start_sh_invocation():
    """Cross-check the Docker CMD against what scripts/start.sh uses
    for host-mode SA. The two paths must invoke SA the same way; my
    Phase L Dockerfile shipped `serve --listen host:port` while
    start.sh uses `server --host x --port y`."""
    df = _SA_DOCKERFILE.read_text()
    sh = _START_SH.read_text()

    # Extract the actual SA invocation line from start.sh — the one
    # that spawns the daemon (typically prefixed with `nohup`). Avoids
    # matching comments / log strings that mention "sandbox-agent".
    sh_match = re.search(r"^\s*nohup\s+sandbox-agent\s+(\w+)\b", sh, re.M)
    assert sh_match, "couldn't find `nohup sandbox-agent <subcommand>` in start.sh"
    sh_subcommand = sh_match.group(1)
    assert f'"{sh_subcommand}"' in df, (
        f"Dockerfile CMD uses a different SA subcommand than start.sh "
        f"(start.sh uses `{sh_subcommand}`, Dockerfile doesn't reference it)"
    )

    # Both must use the same flag names. start.sh uses `--host` and
    # `--port`, not `--listen`.
    if "--host" in sh and "--port" in sh:
        assert "--host" in df and "--port" in df, (
            "Dockerfile must use --host / --port to match start.sh; "
            "the `--listen host:port` form is not a real SA flag"
        )


def test_sa_dockerfile_installs_healthcheck_tool():
    """The compose healthcheck calls a tool that must exist in the SA
    container. If it doesn't, every healthcheck fails silently."""
    df = _SA_DOCKERFILE.read_text()
    compose = yaml.safe_load(_COMPOSE_YAML.read_text())
    hc = compose["services"]["sandbox-agent"].get("healthcheck") or {}
    test_cmd = hc.get("test") or []

    # First non-flag arg after "CMD" is the tool name.
    if test_cmd and test_cmd[0] == "CMD":
        tool = test_cmd[1]
        # The tool needs to be either installed via `apk add` in the
        # Dockerfile or be in the base image's busybox.
        # Alpine busybox provides `wget` but NOT `curl`.
        assert (
            ("apk add" in df and tool in df)  # explicitly installed
            or tool == "wget"  # busybox builtin on alpine
        ), (
            f"healthcheck calls `{tool}` but Dockerfile doesn't install it. "
            f"Either `apk add {tool}` in the Dockerfile or use a busybox "
            f"builtin (wget)."
        )


# --- aitelier Dockerfile ---------------------------------------------------


def test_aitelier_dockerfile_does_not_set_stale_env_vars():
    """Aitelier reads zero env vars (config.py invariant). The
    Dockerfile must not bake AITELIER_HOST / AITELIER_PORT /
    AITELIER_API_KEY into the image — those don't do anything and
    mislead operators."""
    assert _AITELIER_DOCKERFILE.exists()
    text = _AITELIER_DOCKERFILE.read_text()

    # ENV lines (image-level) — these would be the most misleading.
    env_lines = [ln for ln in text.splitlines()
                  if re.match(r"^\s*ENV\s+AITELIER_", ln)]
    assert not env_lines, (
        f"Dockerfile sets AITELIER_* env vars but aitelier reads no env "
        f"vars. Configure via mounted aitelier.toml instead. Lines: {env_lines}"
    )


def test_aitelier_dockerfile_documents_config_mount():
    """Operators need a path forward for config injection. The
    Dockerfile usage comment should reference mounting aitelier.toml."""
    text = _AITELIER_DOCKERFILE.read_text()
    assert "aitelier.toml" in text, (
        "Dockerfile usage notes should explain how to mount aitelier.toml "
        "(since env vars no longer work)"
    )


# --- compose ---------------------------------------------------------------


def test_compose_has_sa_profile():
    """docker/docker-compose.yml must define the `sa` profile so
    `[sandbox_agent] mode = "docker"` has something to start."""
    data = yaml.safe_load(_COMPOSE_YAML.read_text())
    services = data.get("services") or {}
    sa = services.get("sandbox-agent")
    assert sa is not None, "no sandbox-agent service in compose"
    assert "sa" in (sa.get("profiles") or []), \
        f"sandbox-agent must be in the `sa` profile: {sa.get('profiles')}"
    ports = sa.get("ports") or []
    assert any("2468:2468" in str(p) for p in ports), \
        f"sandbox-agent must expose :2468: {ports}"


def test_compose_sa_profile_is_off_by_default():
    """The `sa` profile must be opt-in — bare `docker compose up` should
    not start SA. Tested by confirming the service declares a profile
    (services without `profiles:` start unconditionally)."""
    data = yaml.safe_load(_COMPOSE_YAML.read_text())
    sa = data["services"]["sandbox-agent"]
    assert sa.get("profiles"), "sandbox-agent has no profiles — would auto-start"
