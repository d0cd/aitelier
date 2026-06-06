"""Fan-out execution — run a task across multiple providers concurrently."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aitelier.runner import _dispatch, _persist_result, _write_manifest, make_run_dir, make_run_id


async def fanout(
    task: dict,
    providers: list[str] | None = None,
    *,
    max_concurrent: int = 4,
    base_dir: Path | None = None,
) -> list[dict]:
    """Execute a task across multiple providers concurrently.

    Returns a list of result dicts, one per provider.
    """
    providers = providers or task.get("preferred_providers", [])
    if not providers:
        raise ValueError("No providers specified for fan-out")

    run_id = make_run_id(task["name"])
    run_dir = make_run_dir(run_id, base_dir)

    if task.get("prompt"):
        (run_dir / "prompt.txt").write_text(task["prompt"])

    timeout = task.get("timeout") or (60 if task["kind"] in ("complete", "llm") else 600)

    sem = asyncio.Semaphore(max_concurrent)

    async def run_one(provider: str) -> dict:
        async with sem:
            task_copy = {**task, "model": provider}
            return await _dispatch(
                task_copy, model=provider, timeout=timeout,
                run_dir=run_dir, run_id=run_id,
            )

    results = await asyncio.gather(
        *(run_one(p) for p in providers),
        return_exceptions=True,
    )

    # Convert exceptions to error results
    final: list[dict] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            r = {
                "kind": task["kind"],
                "provider": providers[i],
                "content": "",
                "duration_s": 0,
                "status": "error",
                "cost_usd": None,
                "error_type": type(r).__name__,
                "error_msg": str(r),
                "run_id": run_id,
            }
        _persist_result(run_dir, providers[i], r)
        final.append(r)

    _write_manifest(run_dir, task, final)
    _write_comparison(run_dir, final)

    return final


def _write_comparison(run_dir: Path, results: list[dict]) -> None:
    lines = ["# Fan-out comparison\n"]
    for r in results:
        status_mark = "ok" if r["status"] == "ok" else "ERROR"
        lines.append(f"## {r['provider']} [{status_mark}] ({r['duration_s']}s)\n")
        text = r.get("content") or r.get("text") or ""
        lines.append(text[:2000])
        lines.append("\n\n---\n")
    (run_dir / "compare.md").write_text("\n".join(lines))
