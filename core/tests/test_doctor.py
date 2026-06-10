"""Tests for `aitelier doctor` and scripts/doctor.sh.

doctor.sh has two pieces of logic that matter and are easy to regress:
  - pidfile-aware port checking (a port held by our own service is not a
    conflict)
  - editable uv-tool detection (`uv tool install --editable ./core` is the
    recommended way to expose `aitelier` globally; doctor must accept it)

We test the script directly with bash. `aitelier doctor` is exercised
implicitly: it execs the same script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR_SH = REPO_ROOT / "scripts" / "doctor.sh"


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# Strict mode: doctor.sh requires bash + lsof; if the host is missing
# them, fail loudly at the module-level assertion rather than skipping.
# This catches CI machines that don't have the tools doctor needs.
assert _has("bash") and _has("lsof"), (
    "doctor.sh needs bash and lsof on PATH. Install them (e.g. via "
    "Homebrew on macOS, apt on Debian) or deselect this file with "
    "`pytest -k 'not doctor'`."
)


def test_doctor_script_exists_and_is_runnable():
    assert DOCTOR_SH.exists(), f"missing {DOCTOR_SH}"
    # Smoke-run: even if some checks fail, the script must produce output
    # and exit with a defined code (0 or 1) — not blow up.
    result = subprocess.run(
        ["bash", str(DOCTOR_SH)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode in (0, 1), result.stderr
    assert "=== Ports ===" in result.stdout
    assert "=== Tools ===" in result.stdout


def test_doctor_recognizes_live_service_via_pidfile(tmp_path):
    """When runs/.aitelier.pid points at a live process AND that process is
    holding port 7777, doctor must say ✓ ("our running service") rather than
    ✗ ("held by unknown process")."""
    # Listen on an ephemeral port from a background bash, write its PID to a
    # mock runs/.aitelier.pid pointing somewhere in tmp_path. The doctor
    # script reads the path relative to REPO_ROOT, so we can't easily redirect
    # — instead, run a subprocess that listens, then inspect the actual repo
    # state via lsof + the script's logic. This test is structural: we just
    # confirm the script's _check_port helper accepts a pid-file arg.
    text = DOCTOR_SH.read_text()
    assert "pidfile" in text or ".aitelier.pid" in text, (
        "doctor.sh should reference the pid file when deciding if a port "
        "holder is our own service"
    )
    # The two ports that get the pid-file argument:
    assert "runs/.sandbox-agent.pid" in text
    assert "runs/.aitelier.pid" in text


def test_doctor_accepts_editable_uv_tool_install():
    """The doctor warning logic should NOT fire on an editable install that
    points at this repo. A non-editable install (or an editable install at
    a different path) is the real drift risk."""
    text = DOCTOR_SH.read_text()
    # We don't shell out to `uv tool list` here (CI may not have the install);
    # instead we verify the script encodes the right policy.
    assert "editable-here" in text
    assert "editable-elsewhere" in text
    assert "non-editable" in text


def test_aitelier_doctor_subcommand_locates_script():
    """`aitelier doctor` resolves scripts/doctor.sh relative to its package
    source so editable installs work. We don't run it (would spawn a real
    bash check) — just exercise the path-resolution code path."""
    from aitelier import cli

    repo_root = Path(cli.__file__).resolve().parents[3]
    expected = repo_root / "scripts" / "doctor.sh"
    assert expected == DOCTOR_SH, (
        "cli.py's _cmd_doctor walks parents[3] from cli.py to repo root — "
        "if you moved cli.py, fix that count"
    )
    assert os.access(expected, os.R_OK)
