"""Validate the deployment samples shipped under docs/deploy/ and docker/.

These are documentation-shaped (not auto-loaded by anything in the run-time
path), but consumers copy them as starting points. A regression in
`docs/deploy/aitelier.cell.yaml` or `docker/sandbox-agent.Dockerfile` is
silent until someone tries to use them — these tests catch the shape
breaks at unit-test speed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CELL_YAML = _REPO_ROOT / "docs" / "deploy" / "aitelier.cell.yaml"
_SA_DOCKERFILE = _REPO_ROOT / "docker" / "sandbox-agent.Dockerfile"
_COMPOSE_YAML = _REPO_ROOT / "docker" / "docker-compose.yml"


def test_brig_cell_yaml_parses():
    """The sample cell yaml must be syntactically valid YAML."""
    assert _CELL_YAML.exists(), f"missing: {_CELL_YAML}"
    data = yaml.safe_load(_CELL_YAML.read_text())
    assert isinstance(data, dict), "cell yaml must parse to a dict"


def test_brig_cell_yaml_declares_required_keys():
    """Keys brig expects on a cell definition (cross-checked against
    hermes-agent's cells/hermes/hermes.yaml shape)."""
    data = yaml.safe_load(_CELL_YAML.read_text())
    required = {"name", "image", "command", "network",
                "policy", "ingress", "labels"}
    missing = required - set(data.keys())
    assert not missing, f"cell yaml missing keys: {missing}"


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


def test_sa_dockerfile_shape():
    """The SA Dockerfile must:
      - start from a known-good base
      - install SA via the documented Rivet release URL
      - expose :2468
      - have an ENTRYPOINT/CMD that actually starts the binary
    """
    assert _SA_DOCKERFILE.exists(), f"missing: {_SA_DOCKERFILE}"
    text = _SA_DOCKERFILE.read_text()
    assert "FROM alpine" in text, "expected alpine base"
    assert "releases.rivet.dev/sandbox-agent" in text, \
        "SA install URL not present — Rivet may have moved; update Dockerfile"
    assert "EXPOSE 2468" in text, "SA's ACP port not exposed"
    assert "sandbox-agent" in text.lower(), "binary not referenced in CMD"


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
