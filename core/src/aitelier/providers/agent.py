"""Agent provider — all coding agents run via Sandbox Agent (Rivet).

Phase 4 (PLAN.md) replaced the direct claude/codex subprocess paths with the
Sandbox Agent server. Sandboxing, env scoping, MCP wiring, and event
normalization are now the Sandbox Agent's responsibility — aitelier just
speaks ACP to it.

This module is a thin compatibility shim. The actual implementation lives in
`providers/sandbox_agent.py`. `call_agent` is preserved as the public name
because `runner.py`, the providers `__init__`, and tests import it.
"""

from __future__ import annotations

from aitelier.providers.sandbox_agent import (
    _error_result,
    _timeout_result,
)
from aitelier.providers.sandbox_agent import (
    call_via_sandbox as call_agent,
)

__all__ = ["call_agent", "_error_result", "_timeout_result"]
