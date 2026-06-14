"""`/v1/runs/*` endpoints — durable run state + lifecycle.

Routes registered on this module's `router` and included into the main
app in `server.py`. The handlers depend on helpers defined in
`server.py` (`_active_runs`, `_check_idempotency`, projection helpers,
…) which are imported lazily inside each function to avoid the
circular import that would result from a top-level
`from aitelier.server import …` (server.py registers this router; the
router can't import from a half-loaded server.py at module-init time).

Endpoints surfaced here:
- POST   /v1/runs                            — submit async agent run
- GET    /v1/runs                            — list runs (filtered)
- GET    /v1/runs/active                     — in-flight registry
- GET    /v1/runs/{run_id}                   — get one run + on-disk artifacts
- GET    /v1/runs/{run_id}/events            — paginated event timeline
- GET    /v1/runs/{run_id}/events/stream     — SSE live event feed
- POST   /v1/runs/{run_id}/wait              — block until terminal state
- POST   /v1/runs/{run_id}/cancel            — signal cancellation
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from aitelier.config import get_config
from aitelier.errors import classify_error
from aitelier.openai_compat import AsyncRunRequest, parse_model_route
from aitelier.runner import make_run_id
from aitelier.storage import RunFilter, get_store

router = APIRouter()


_TERMINAL_STATES_FOR_WAIT = frozenset({"completed", "failed", "cancelled", "orphaned"})


@router.post("/v1/runs")
async def submit_async_run(req: AsyncRunRequest, request: Request) -> dict:
    """Async agent run: returns immediately with a run_id; the final
    ChatCompletion (or error) is delivered via webhook when ready.

    LLM-path async isn't supported — LLM calls are short and stream-capable;
    use /v1/chat/completions. Async exists for long-running agent runs.
    """
    from aitelier.server import (
        _active_runs,
        _agent_chat_completion,
        _check_idempotency,
        _check_webhook_url_or_die,
        _enqueue_webhook,
        _record_idempotency,
        _reject_if_saturated,
        _release_idempotency_ctx,
        _validate_aitelier_opts,
    )

    route, agent_backend, inner_llm = parse_model_route(req.model)
    if route != "agent":
        raise HTTPException(
            status_code=400,
            detail="/v1/runs is for async agent runs only — set model to "
                   "'agent:<backend>[/<inner-llm>]', or use "
                   "/v1/chat/completions for LLM calls.",
        )
    await _validate_aitelier_opts(req, agent_path=True)
    _reject_if_saturated()

    cid = request.state.correlation_id
    idem = await _check_idempotency(request, "/v1/runs")
    if idem and idem.cached is not None:
        return idem.cached

    try:
        if req.webhook_url:
            await _check_webhook_url_or_die(req.webhook_url)

        run_id = make_run_id("chat_agent_async")
        webhook_url = req.webhook_url
        inner_req = req  # ChatCompletionRequest fields are a subset

        async def _run_and_callback() -> None:
            try:
                result = await _agent_chat_completion(
                    inner_req, request,
                    agent_backend=agent_backend, inner_llm=inner_llm, run_id=run_id,
                    webhook_url=webhook_url,
                )
            except Exception as exc:
                result = {
                    "error": {
                        "type": classify_error(exc), "message": str(exc),
                    },
                    "aitelier_run_id": run_id,
                }
            # Strip the rendering hint before persistence/delivery — it's only
            # meaningful to a synchronous HTTP responder, not to webhook consumers.
            result.pop("aitelier_status_code", None)
            if webhook_url:
                await _enqueue_webhook(webhook_url, result, run_id=run_id)

        outer_task = asyncio.create_task(_run_and_callback())
        # Pre-register so an immediate POST /v1/runs/{id}/cancel doesn't 404.
        # `_agent_chat_completion` swaps this entry for the inner run task once
        # it starts; the outer task is the safe placeholder.
        _active_runs[run_id] = outer_task
        accepted = {
            "run_id": run_id,
            "status": "accepted",
            "correlation_id": cid,
            "webhook_url": webhook_url,
        }
        await _record_idempotency(idem, accepted)
    except BaseException:
        # Release the idempotency lock without writing if anything above
        # raised — otherwise a retry under the same key is wedged.
        _release_idempotency_ctx(idem)
        raise
    return accepted


@router.get("/v1/runs")
async def list_runs_endpoint(
    state: str | None = None,
    kind: str | None = None,
    agent_id: str | None = None,
    trace_tag: str | None = None,
    correlation_id: str | None = None,
    parent_run_id: str | None = None,
    since: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """List runs from the durable store with optional filters.

    `state` ∈ {pending, running, completed, failed, cancelled, orphaned}.
    `parent_run_id` filters to children of a specific parent — the
    primary way to reconstruct a multi-agent workflow's tree.
    """
    from aitelier.server import _run_to_dict

    store = await get_store()
    since_dt = datetime.fromisoformat(since) if since else None
    runs = await store.list_runs(RunFilter(
        state=state, kind=kind, agent_id=agent_id,
        trace_tag=trace_tag, correlation_id=correlation_id,
        parent_run_id=parent_run_id,
        since=since_dt, limit=limit,
    ))
    return [_run_to_dict(r) for r in runs]


@router.get("/v1/runs/{run_id}/events")
async def list_run_events_endpoint(
    run_id: str,
    since_seq: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=5000),
) -> list[dict]:
    """Paginated event timeline for a single run."""
    from aitelier.server import _event_to_dict, _validate_path_component

    _validate_path_component(run_id, "run_id")
    store = await get_store()
    events = await store.list_events(run_id, since_seq=since_seq, limit=limit)
    return [_event_to_dict(e) for e in events]


@router.get("/v1/runs/{run_id}/events/stream")
async def stream_run_events_endpoint(run_id: str, request: Request):
    """SSE: live event feed for one run.

    Tails the run_events table — useful for dashboards rendering an active
    agent's progress. Streams every event as it's appended; for already-
    completed runs, simply yields the full backlog then closes.
    """
    from aitelier.server import (
        _event_to_dict,
        _sse_event,
        _sse_response,
        _validate_path_component,
    )

    _validate_path_component(run_id, "run_id")
    store = await get_store()
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    async def event_generator():
        last_seq = 0
        # Poll-based tail: cheap at our scale; LISTEN/NOTIFY is a Phase-9.5
        # upgrade for Postgres specifically. InMemoryStore polling works fine.
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            new = await store.list_events(run_id, since_seq=last_seq, limit=500)
            if new:
                for ev in new:
                    yield _sse_event(f"run.{ev.kind}", _event_to_dict(ev))
                    last_seq = max(last_seq, ev.seq)
                idle_ticks = 0
            else:
                idle_ticks += 1
            # If the run is terminal AND we've drained, close the stream.
            current = await store.get_run(run_id)
            if current and current.state in (
                "completed", "failed", "cancelled", "orphaned",
            ) and not new:
                break
            await asyncio.sleep(0.5)

    return _sse_response(event_generator())


@router.get("/v1/runs/active")
async def list_active_runs() -> dict:
    """List run_ids currently in-flight in this server process."""
    from aitelier.server import _active_runs

    return {"active": sorted(_active_runs.keys())}


@router.post("/v1/runs/{run_id}/wait")
async def wait_for_run(
    run_id: str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> dict:
    """Block until a run reaches a terminal state, then return it.

    Convenience over manual polling: a consumer that submits an async
    run via `POST /v1/runs` and doesn't want to set up a webhook
    receiver can call this and get the final Run row back when it
    settles.

    Polls the store every `poll_interval` seconds (default 0.5s) up to
    `timeout` seconds (default 60s, max 600s). Returns the Run as
    soon as state ∈ {completed, failed, cancelled, orphaned}. Returns
    HTTP 408 if the run is still pending/running at deadline — the
    consumer can call again to keep waiting.

    Returns 404 if the run id doesn't exist. Returns the same Run
    shape as `GET /v1/runs/{id}` (no on-disk artifacts folded in;
    fetch separately if needed).
    """
    from aitelier.server import _run_to_dict, _validate_path_component

    _validate_path_component(run_id, "run_id")
    if timeout <= 0 or timeout > 600:
        raise HTTPException(
            status_code=400,
            detail="timeout must be in (0, 600] seconds",
        )
    if poll_interval <= 0 or poll_interval > 10:
        raise HTTPException(
            status_code=400,
            detail="poll_interval must be in (0, 10] seconds",
        )

    store = await get_store()
    deadline = time.monotonic() + timeout
    while True:
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        if run.state in _TERMINAL_STATES_FOR_WAIT:
            return _run_to_dict(run)
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=408,
                detail=(
                    f"Run {run_id} still in state={run.state} after "
                    f"{timeout}s. Call again to keep waiting."
                ),
            )
        await asyncio.sleep(poll_interval)


@router.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    """Signal cancellation for an in-flight run.

    Returns 404 if the run isn't currently active (already finished or
    never existed). The owning request will receive a result with
    `status: "error"`, `error_type: "Cancelled"`.
    """
    from aitelier.server import _active_runs, _validate_path_component

    _validate_path_component(run_id, "run_id")
    task = _active_runs.get(run_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Run not active: {run_id}")
    task.cancel()
    return {"run_id": run_id, "cancelled": True}


@router.get("/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    """Fetch one run from the durable store. Same shape as `/v1/runs` list
    entries, plus on-disk artifacts (prompt, manifest) folded in when the
    run dir exists (agent runs with prepare/artifacts).
    """
    from aitelier.server import _run_to_dict, _validate_path_component

    _validate_path_component(run_id, "run_id")
    store = await get_store()
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    body = _run_to_dict(run)

    # Best-effort: fold in on-disk artifacts (prompt.txt, manifest.json) if
    # the agent path wrote them. Defense-in-depth on the path so a crafted
    # run_id can't escape the runs/ root. `os.sep` suffix forces the prefix
    # check to span a directory boundary so `runs_evil/` can't match `runs/`.
    runs_base = Path(get_config().runs_dir).resolve()
    run_dir = (runs_base / run_id).resolve()
    base_prefix = str(runs_base) + os.sep
    if (str(run_dir) == str(runs_base) or
            str(run_dir).startswith(base_prefix)) and run_dir.exists():
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                body["manifest"] = json.loads(manifest_path.read_text())
            except json.JSONDecodeError:
                pass
        prompt_path = run_dir / "prompt.txt"
        if prompt_path.exists():
            body["prompt"] = prompt_path.read_text()

    return body
