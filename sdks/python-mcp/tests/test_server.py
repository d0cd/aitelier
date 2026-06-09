"""Tests for aitelier-mcp.

The strategy: inject a fake Aitelier client into create_server, then
invoke each MCP tool through FastMCP's call_tool helper. We verify
the tool maps to the right SDK call with the right kwargs, and that
parent_run_id flows through the aitelier_opts block.

We don't test FastMCP itself — that's a third-party concern. We test
the wiring between our tool functions and the SDK.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Obj:
    """Plain object whose attributes survive `__dict__` introspection
    (MagicMock's `__dict__` is private bookkeeping, not the attrs we set)."""
    def __init__(self, **fields):
        self.__dict__.update(fields)


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.submit_run = AsyncMock(return_value={
        "run_id": "child-1", "status": "accepted", "webhook_url": None,
    })
    client.get_run = AsyncMock(return_value=_Obj(
        run_id="child-1", state="completed",
        parent_run_id="parent-1", status="ok",
    ))
    client.list_runs = AsyncMock(return_value=[
        _Obj(run_id="child-1", parent_run_id="parent-1"),
        _Obj(run_id="child-2", parent_run_id="parent-1"),
    ])
    client.list_run_events = AsyncMock(return_value=[
        _Obj(run_id="child-1", seq=1, kind="start"),
        _Obj(run_id="child-1", seq=2, kind="finish"),
    ])
    client.cancel_run = AsyncMock(return_value=_Obj(
        run_id="child-1", cancelled=True,
    ))
    return client


@pytest.fixture
def server(fake_client):
    from aitelier_mcp import create_server
    return create_server(client=fake_client)


async def _call_tool(server, name: str, **args: Any) -> Any:
    """Invoke an MCP tool through the FastMCP runtime. Returns the
    tool's raw result (not the wrapped MCP CallToolResult envelope)."""
    result = await server.call_tool(name, args)
    # FastMCP returns (content_list, structured_dict_or_None). The
    # structured payload is what our tools actually returned; the
    # content list is the textual rendering MCP clients display.
    if isinstance(result, tuple) and len(result) == 2:
        _content, structured = result
        if structured is not None:
            # FastMCP wraps non-dict primitive returns under "result".
            return structured.get("result", structured)
        return _content
    return result


@pytest.mark.asyncio
async def test_submit_run_forwards_to_sdk_with_aitelier_opts(server, fake_client):
    """parent_run_id, trace_tag, workspace all collect into the
    aitelier_opts block as the SDK expects."""
    out = await _call_tool(
        server, "submit_run",
        model="agent:claude",
        messages=[{"role": "user", "content": "hi"}],
        parent_run_id="parent-1",
        trace_tag="workflow-X",
        workspace="/tmp/sub",
    )
    assert out["run_id"] == "child-1"
    fake_client.submit_run.assert_awaited_once()
    kwargs = fake_client.submit_run.await_args.kwargs
    assert kwargs["model"] == "agent:claude"
    assert kwargs["aitelier_opts"] == {
        "parent_run_id": "parent-1",
        "trace_tag": "workflow-X",
        "workspace": "/tmp/sub",
    }


@pytest.mark.asyncio
async def test_submit_run_omits_aitelier_block_when_no_options(server, fake_client):
    """Bare submission without parent / trace / workspace shouldn't
    send an empty `aitelier: {}` block — let aitelier defaults apply."""
    await _call_tool(
        server, "submit_run",
        model="agent:claude",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert fake_client.submit_run.await_args.kwargs["aitelier_opts"] is None


@pytest.mark.asyncio
async def test_list_runs_filters_by_parent(server, fake_client):
    """The primary multi-agent query: list children of a parent run."""
    out = await _call_tool(server, "list_runs", parent_run_id="parent-1")
    assert len(out) == 2
    fake_client.list_runs.assert_awaited_once()
    assert fake_client.list_runs.await_args.kwargs["parent_run_id"] == "parent-1"


@pytest.mark.asyncio
async def test_get_run_returns_dict(server, fake_client):
    out = await _call_tool(server, "get_run", run_id="child-1")
    assert out["run_id"] == "child-1"
    assert out["parent_run_id"] == "parent-1"
    fake_client.get_run.assert_awaited_once_with("child-1")


@pytest.mark.asyncio
async def test_list_run_events_returns_event_dicts(server, fake_client):
    out = await _call_tool(server, "list_run_events", run_id="child-1")
    assert len(out) == 2
    assert out[0]["kind"] == "start"
    assert out[-1]["kind"] == "finish"


@pytest.mark.asyncio
async def test_cancel_run_acks_idempotently(server, fake_client):
    out = await _call_tool(server, "cancel_run", run_id="child-1")
    assert out["cancelled"] is True
    fake_client.cancel_run.assert_awaited_once_with("child-1")


@pytest.mark.asyncio
async def test_tool_catalog_complete(server):
    """The five tools an inner agent needs to drive a multi-agent
    workflow through aitelier."""
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "submit_run", "get_run", "list_runs",
        "list_run_events", "cancel_run",
    }
