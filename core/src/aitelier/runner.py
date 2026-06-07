"""Task runner — dispatch, run directory management, persistence, tracing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aitelier.config import get_config
from aitelier.providers.agent import call_agent
from aitelier.providers.llm import complete, embed
from aitelier.runs import hash_system_prompt, record_run
from aitelier.storage import RunSpec


def make_run_id(task_name: str) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    return f"{ts}_{task_name}"


def make_run_dir(run_id: str, base: Path | None = None) -> Path:
    base = base or Path(get_config().runs_dir)
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


async def execute(task: dict, *, base_dir: Path | None = None, run_id: str | None = None) -> dict:
    """Execute a task spec. Dispatches based on kind: complete, embed, agent.

    Also supports legacy kind="llm" (mapped to complete).

    `run_id` may be provided by the caller (e.g. an HTTP handler that needs
    to register the run in a cancel registry before dispatching).
    """
    kind = task["kind"]
    if run_id is None:
        run_id = make_run_id(task["name"])

    # For complete/embed, no run dir needed unless we want persistence
    # For agent, run dir is used for events/diffs
    run_dir = make_run_dir(run_id, base_dir) if kind == "agent" else None

    if task.get("prompt"):
        if run_dir:
            (run_dir / "prompt.txt").write_text(task["prompt"])

    model = task.get("model") or _default_model(kind)
    timeout = task.get("timeout") or _default_timeout(kind)

    metadata = task.get("metadata") or {}
    spec = RunSpec(
        run_id=run_id,
        kind="complete" if kind == "llm" else kind,
        agent_id=model if kind == "agent" else None,
        model=model,
        trace_tag=task.get("trace_tag"),
        correlation_id=metadata.get("correlation_id"),
        workspace=task.get("workspace"),
        environment={
            "mcp_servers": task.get("mcp_servers") or [],
            "tool_allowlist": task.get("tool_allowlist") or [],
        },
        system_prompt_hash=hash_system_prompt(task.get("system_prompt")),
        metadata=metadata,
    )
    result = await record_run(
        spec,
        _dispatch(task, model=model, timeout=timeout,
                  run_dir=run_dir, run_id=run_id),
    )

    if run_dir:
        _persist_result(run_dir, result["provider"], result)
        _write_manifest(run_dir, task, [result])

    return result


async def _dispatch(
    task: dict, model: str, timeout: int, run_dir: Path | None, run_id: str,
) -> dict:
    kind = task["kind"]

    if kind in ("complete", "llm"):
        # Build messages from prompt or messages field
        messages = task.get("messages") or [{"role": "user", "content": task.get("prompt", "")}]
        return await complete(
            model=model,
            messages=messages,
            system_prompt=task.get("system_prompt"),
            temperature=task.get("temperature"),
            max_tokens=task.get("max_tokens"),
            response_format=task.get("response_format"),
            timeout=timeout,
            run_id=run_id,
            trace_tag=task.get("trace_tag"),
        )

    elif kind == "embed":
        texts = task.get("texts") or []
        return await embed(
            texts=texts,
            model=model,
            timeout=timeout,
            run_id=run_id,
        )

    elif kind == "agent":
        prompt = task.get("prompt", "")
        if task.get("messages"):
            # For agent, the initial message is the last user message
            user_msgs = [m for m in task["messages"] if m["role"] == "user"]
            if user_msgs:
                prompt = user_msgs[-1]["content"]

        return await call_agent(
            name=model,
            prompt=prompt,
            workspace=task.get("workspace"),
            workspace_mode=task.get("workspace_mode", "copy"),
            system_prompt=task.get("system_prompt"),
            mcp_servers=task.get("mcp_servers"),
            tool_allowlist=task.get("tool_allowlist"),
            response_format=task.get("response_format"),
            max_turns=task.get("max_turns"),
            agent_model=task.get("agent_model"),
            timeout=timeout,
            run_dir=run_dir,
            run_id=run_id,
            trace_tag=task.get("trace_tag"),
        )

    else:
        raise ValueError(f"Unknown task kind: {kind}")


def _persist_result(run_dir: Path, provider: str, result: dict) -> None:
    provider_dir = run_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    (provider_dir / "result.txt").write_text(result.get("content") or result.get("text") or "")


def _write_manifest(run_dir: Path, task: dict, results: list[dict]) -> None:
    manifest = {
        "task": {k: v for k, v in task.items() if k not in ("prompt", "system_prompt", "texts")},
        "run_id": results[0]["run_id"] if results else "",
        "results": [
            {"provider": r["provider"], "status": r["status"], "duration_s": r["duration_s"]}
            for r in results
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _default_model(kind: str) -> str:
    if kind in ("complete", "llm"):
        return "claude-sonnet"
    if kind == "embed":
        return "nomic-embed-text"
    return "claude-code"


def _default_timeout(kind: str) -> int:
    if kind in ("complete", "llm"):
        return 60
    if kind == "embed":
        return 30
    return 600
