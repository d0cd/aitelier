"""Implement task — implement a feature or change in a codebase."""

from __future__ import annotations

from pathlib import Path


def implement(workspace: str, description: str, workspace_mode: str = "copy") -> dict:
    """Build a task spec for implementing a feature."""
    return {
        "name": "implement",
        "kind": "agent",
        "prompt": (
            f"Implement the following in this codebase:\n\n{description}\n\n"
            "Requirements:\n"
            "- Write clean, idiomatic code consistent with the existing style\n"
            "- Add tests for new functionality\n"
            "- Do not break existing tests\n"
            "- Explain what you changed and why"
        ),
        "workspace": str(Path(workspace).resolve()),
        "workspace_mode": workspace_mode,
        "preferred_providers": ["claude-code"],
    }
