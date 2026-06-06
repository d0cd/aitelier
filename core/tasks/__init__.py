"""Task definitions — discovered by name from this package."""

from __future__ import annotations

import importlib
from pathlib import Path

_TASKS_DIR = Path(__file__).parent


def get_task(name: str, **kwargs) -> dict:
    """Load a task definition by name and return its spec dict."""
    try:
        module = importlib.import_module(f"tasks.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"Unknown task: {name}. Available: {list_tasks()}")

    build_fn = getattr(module, name, None) or getattr(module, f"{name}_task", None)
    if not build_fn:
        raise ValueError(f"Task module 'tasks.{name}' has no '{name}' or '{name}_task' function")

    return build_fn(**kwargs)


def list_tasks() -> list[str]:
    """List all available task names."""
    return sorted(
        p.stem
        for p in _TASKS_DIR.glob("*.py")
        if p.stem != "__init__"
    )
