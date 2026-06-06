"""Workspace preparation and diffing."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def prepare_workspace(source: str, mode: str = "copy") -> tuple[str, str | None]:
    """Prepare a workspace directory for an agent task.

    Returns (work_dir, tmp_dir). If mode="copy", work_dir is a tmpdir copy;
    caller must clean up tmp_dir. If mode="in_place", work_dir is source and
    tmp_dir is None.
    """
    if mode == "in_place":
        return source, None

    tmp_dir = tempfile.mkdtemp(prefix="aitelier_ws_")
    shutil.copytree(source, tmp_dir, dirs_exist_ok=True)
    return tmp_dir, tmp_dir


def compute_diff(original: str, modified: str) -> str:
    """Compute a unified diff between original and modified directories."""
    try:
        result = subprocess.run(
            ["diff", "-ruN", original, modified],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def save_diff(diff_text: str, run_dir: Path, provider_name: str) -> Path | None:
    """Save a diff to the run directory. Returns the path, or None if empty."""
    if not diff_text.strip():
        return None
    provider_dir = run_dir / provider_name
    provider_dir.mkdir(parents=True, exist_ok=True)
    path = provider_dir / "diff.patch"
    path.write_text(diff_text)
    return path
