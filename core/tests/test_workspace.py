"""Tests for workspace preparation and diffing."""

from __future__ import annotations

import shutil
from pathlib import Path

from aitelier.workspace import compute_diff, prepare_workspace, save_diff


def test_prepare_workspace_copy(tmp_path):
    # Set up a source directory with a file
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")

    work_dir, tmp_dir = prepare_workspace(str(source), mode="copy")

    assert tmp_dir is not None
    assert work_dir == tmp_dir
    assert Path(work_dir, "hello.txt").read_text() == "hello"

    # Clean up
    shutil.rmtree(tmp_dir)


def test_prepare_workspace_in_place(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    work_dir, tmp_dir = prepare_workspace(str(source), mode="in_place")

    assert tmp_dir is None
    assert work_dir == str(source)


def test_compute_diff(tmp_path):
    orig = tmp_path / "orig"
    orig.mkdir()
    (orig / "file.txt").write_text("original")

    modified = tmp_path / "modified"
    modified.mkdir()
    (modified / "file.txt").write_text("changed")

    diff = compute_diff(str(orig), str(modified))
    assert "original" in diff
    assert "changed" in diff


def test_save_diff(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    path = save_diff("some diff content", run_dir, "test-provider")
    assert path is not None
    assert path.read_text() == "some diff content"


def test_save_diff_empty(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    path = save_diff("", run_dir, "test-provider")
    assert path is None
