"""Audit task — security and quality audit of a codebase."""

from __future__ import annotations

from pathlib import Path


def audit(workspace: str, focus: str = "security") -> dict:
    """Build a task spec for auditing code."""
    return {
        "name": "audit",
        "kind": "agent",
        "prompt": (
            f"Audit the code in this directory for {focus} issues. "
            "Examine all source files systematically. For each issue found, report:\n"
            "1. File and line number\n"
            "2. Severity (critical/high/medium/low/info)\n"
            "3. Description of the issue\n"
            "4. Suggested fix\n\n"
            "Conclude with a summary of findings by severity."
        ),
        "workspace": str(Path(workspace).resolve()),
        "workspace_mode": "copy",
        "preferred_providers": ["claude-code", "codex"],
    }
