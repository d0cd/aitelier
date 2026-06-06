"""Lint task — review code for style, correctness, and best practices."""

from __future__ import annotations

from pathlib import Path


def lint(workspace: str, focus: str = "all") -> dict:
    """Build a task spec for linting code."""
    return {
        "name": "lint",
        "kind": "agent",
        "prompt": (
            f"Review the code in this directory for issues (focus: {focus}). "
            "Check for:\n"
            "- Code style and consistency\n"
            "- Potential bugs and logic errors\n"
            "- Performance issues\n"
            "- Best practice violations\n"
            "- Dead code and unused imports\n\n"
            "Report each issue with file, line, severity, and suggested fix."
        ),
        "workspace": str(Path(workspace).resolve()),
        "workspace_mode": "copy",
        "preferred_providers": ["claude-code"],
    }
