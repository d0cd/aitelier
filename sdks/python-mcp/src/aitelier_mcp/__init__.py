"""aitelier-mcp — expose the aitelier control plane as MCP tools.

Designed for the "agent dispatches subagent through aitelier" pattern:
an inner agent loads this MCP server via `aitelier.mcp_servers` and
calls `submit_run` / `cancel_run` / `get_run` / `list_run_events` /
`list_runs` to fan out work to other aitelier-managed agents.

The server is a thin wrapper over `aitelier_client.Aitelier` — no
business logic, no opinion on workflow shape. Lineage flows via the
optional `parent_run_id` argument on `submit_run`.
"""

from aitelier_mcp.server import create_server, main

__all__ = ["create_server", "main"]
