"""Observability — Langfuse integration for LLM and agent tracing."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_langfuse = None


def setup_langfuse() -> None:
    """Configure LiteLLM's Langfuse callback and initialize the Langfuse client."""
    global _langfuse

    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        logger.info("LANGFUSE_PUBLIC_KEY not set, skipping Langfuse setup")
        return

    try:
        import litellm
        litellm.callbacks = ["langfuse"]
        litellm.success_callback = ["langfuse"]
        litellm.failure_callback = ["langfuse"]

        from langfuse import Langfuse
        _langfuse = Langfuse()

        logger.info("Langfuse observability enabled")
    except ImportError:
        logger.warning("langfuse package not installed, skipping")
    except Exception as exc:
        logger.warning("Failed to initialize Langfuse: %s", exc)


def get_langfuse():
    """Return the Langfuse client instance, or None if not configured."""
    return _langfuse


def trace_agent_call(
    task_name: str,
    agent_name: str,
    prompt: str,
    run_id: str,
):
    """Create a Langfuse trace for an agent call. Returns (trace, generation) or (None, None)."""
    if not _langfuse:
        return None, None

    trace = _langfuse.trace(name=task_name, metadata={"run_id": run_id})
    gen = trace.generation(name=f"agent:{agent_name}", model=agent_name, input=prompt)
    return trace, gen


def end_agent_trace(gen, text: str, session_id: str | None, run_id: str) -> None:
    """End a Langfuse generation for an agent call."""
    if gen:
        gen.end(output=text, metadata={"session_id": session_id, "run_id": run_id})


def is_langfuse_connected() -> bool:
    """Check if Langfuse is configured and reachable."""
    return _langfuse is not None
