"""Agent-as-orchestrator: parent agent dispatches subagents via aitelier-mcp.

Instead of writing a Python `gather` loop, hand the orchestration job to
an inner agent and load `aitelier-mcp` as an MCP server. The parent's
tool-use loop now sees `submit_run`, `list_runs`, `get_run`, `cancel_run`
as first-class tools and decides — turn-by-turn — what shape the
workflow should take (fan-out, sequential, retry-on-fail, whatever).

Aitelier is the substrate. The parent agent is the conductor.

Prereqs: `pipx install aitelier-mcp` (or `uv tool install aitelier-mcp`)
so the inner agent can spawn it over stdio. Then `make start` and run
this file: `uv run python 02_mcp_orchestrator.py`.
"""

from __future__ import annotations

import asyncio
import uuid

from aitelier_client import Aitelier


async def run_orchestrator() -> str:
    ait = Aitelier(base_url="http://localhost:7777")
    workflow_tag = f"mcp-orch-{uuid.uuid4().hex[:8]}"

    # 1. Submit the parent. The agent receives `aitelier-mcp` as a stdio
    #    MCP server, so its tool list now includes submit_run / list_runs.
    #    We hand the parent its own workflow_tag so it can pass it down
    #    to children, letting `/v1/traces?trace_tag=...` roll the whole
    #    fan-out into one query later.
    parent_prompt = (
        "Audit this repository three ways in parallel: security, "
        "dependencies, and docstrings. Use the `aitelier` MCP tools "
        "to dispatch three children with model=agent:claude. Pass "
        f"trace_tag='{workflow_tag}' and parent_run_id=<your own run id> "
        "on each submit_run call. Poll list_runs(parent_run_id=...) until "
        "all three are terminal, then summarize their results."
    )

    submission = await ait.submit_run(
        model="agent:claude",
        messages=[{"role": "user", "content": parent_prompt}],
        aitelier_opts={
            "trace_tag": workflow_tag,
            "mcp_servers": [
                {"name": "aitelier", "transport": "stdio",
                 "command": "aitelier-mcp"},
            ],
            "max_turns": 50,
        },
        timeout=600,
    )
    parent_run_id = submission["run_id"]

    # 2. Wait for the parent. Its inner reasoning handles the children;
    #    aitelier just records each submitted run and its result.
    parent = await ait.wait_for_run(parent_run_id, timeout=600)
    summary = parent.result.get("content", "(empty)")

    # 3. After the fact, recover the whole subtree two ways:
    #    parent_run_id gives the direct children; trace_tag gives the
    #    whole workflow (grandchildren too, if the agent decided to nest).
    children = await ait.list_runs(parent_run_id=parent_run_id, limit=100)
    workflow_runs = await ait.list_runs(trace_tag=workflow_tag, limit=100)

    print(f"Parent run:        {parent_run_id}  state={parent.state}")
    print(f"Direct children:   {len(children)} runs")
    print(f"Whole workflow:    {len(workflow_runs)} runs")
    return summary


if __name__ == "__main__":
    print(asyncio.run(run_orchestrator()))
