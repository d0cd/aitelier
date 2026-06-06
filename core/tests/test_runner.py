"""Tests for the task runner."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aitelier.runner import execute, make_run_dir, make_run_id


def test_make_run_id():
    run_id = make_run_id("audit")
    assert "audit" in run_id
    assert run_id[0:2] == "20"


def test_make_run_dir(tmp_path):
    run_dir = make_run_dir("test-run", base=tmp_path)
    assert run_dir.exists()
    assert run_dir.name == "test-run"


@pytest.mark.asyncio
async def test_execute_complete_task(tmp_path):
    mock_result = {
        "kind": "complete",
        "provider": "claude-sonnet",
        "status": "ok",
        "duration_s": 1.5,
        "run_id": "",
        "trace_id": "",
        "content": "Test output",
        "parsed": None,
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        "finish_reason": "stop",
        "cost_usd": 0.01,
        "error_type": None,
        "error_msg": None,
    }

    task = {
        "name": "test_task",
        "kind": "complete",
        "prompt": "Hello, world!",
        "model": "claude-sonnet",
    }

    with (
        patch("aitelier.runner.complete", new_callable=AsyncMock, return_value=mock_result),
        patch("aitelier.runner.record_trace"),
    ):
        result = await execute(task, base_dir=tmp_path)

    assert result["status"] == "ok"
    assert result["content"] == "Test output"


@pytest.mark.asyncio
async def test_execute_error_result(tmp_path):
    mock_result = {
        "kind": "complete",
        "provider": "claude-sonnet",
        "status": "error",
        "duration_s": 0.5,
        "run_id": "",
        "trace_id": "",
        "content": "",
        "parsed": None,
        "usage": None,
        "finish_reason": "error",
        "cost_usd": None,
        "error_type": "APIError",
        "error_msg": "Rate limited",
    }

    task = {
        "name": "test_task",
        "kind": "complete",
        "prompt": "Hello",
    }

    with (
        patch("aitelier.runner.complete", new_callable=AsyncMock, return_value=mock_result),
        patch("aitelier.runner.record_trace"),
    ):
        result = await execute(task, base_dir=tmp_path)

    assert result["status"] == "error"
    assert result["error_type"] == "APIError"
