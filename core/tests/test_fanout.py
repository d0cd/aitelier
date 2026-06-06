"""Tests for fan-out execution."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aitelier.fanout import fanout


@pytest.mark.asyncio
async def test_fanout_multiple_providers(tmp_path):
    call_count = 0

    async def mock_dispatch(task, model, timeout, run_dir, run_id):
        nonlocal call_count
        call_count += 1
        return {
            "kind": "complete",
            "provider": model,
            "status": "ok",
            "duration_s": 1.0,
            "run_id": run_id,
            "content": f"Output from {model}",
            "cost_usd": 0.01,
            "error_type": None,
            "error_msg": None,
        }

    task = {
        "name": "test_fanout",
        "kind": "complete",
        "prompt": "Test prompt",
    }

    with patch("aitelier.fanout._dispatch", side_effect=mock_dispatch):
        results = await fanout(
            task,
            providers=["provider-a", "provider-b", "provider-c"],
            base_dir=tmp_path,
        )

    assert len(results) == 3
    assert call_count == 3
    assert all(r["status"] == "ok" for r in results)

    # Verify comparison file
    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    compare_path = run_dirs[0] / "compare.md"
    assert compare_path.exists()


@pytest.mark.asyncio
async def test_fanout_handles_provider_failure(tmp_path):
    async def mock_dispatch(task, model, timeout, run_dir, run_id):
        if model == "bad-provider":
            raise ConnectionError("Provider unreachable")
        return {
            "kind": "complete",
            "provider": model,
            "status": "ok",
            "duration_s": 1.0,
            "run_id": run_id,
            "content": "OK",
            "cost_usd": 0.01,
            "error_type": None,
            "error_msg": None,
        }

    task = {
        "name": "test_fanout",
        "kind": "complete",
        "prompt": "Test",
    }

    with patch("aitelier.fanout._dispatch", side_effect=mock_dispatch):
        results = await fanout(
            task,
            providers=["good-provider", "bad-provider"],
            base_dir=tmp_path,
        )

    assert len(results) == 2
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "error"
    assert results[1]["error_type"] == "ConnectionError"
