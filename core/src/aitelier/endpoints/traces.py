"""`/v1/traces/*` endpoints — observability projection over the runs table.

Routes registered on this module's `router` and included into the main
app in `server.py`. Same lazy-import pattern as `endpoints/runs.py` for
server-side helpers (_run_to_trace_dict, _validate_path_component).

Endpoints surfaced here:
- GET    /v1/traces                   — list runs as TraceRecord summaries
- GET    /v1/traces/aggregates        — roll up stats by trace_tag/kind/…
- GET    /v1/traces/{trace_id}        — single trace by id
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from aitelier.storage import RunFilter, get_store

router = APIRouter()


@router.get("/v1/traces")
async def traces_endpoint(
    since: str | None = None,
    trace_tag: str | None = None,
    parent_run_id: str | None = None,
    status: str | None = None,
    state: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """Query recent runs as TraceRecord summaries (counts, tokens, cost).

    Both filter axes are supported: `status` ∈ {ok, error, cancelled} is
    the terminal outcome (None while in flight, so `?status=running`
    returns nothing — use `state` for that); `state` ∈ {pending, running,
    completed, …} is the lifecycle position. `parent_run_id` narrows to
    children of a specific parent — useful for rendering a multi-agent
    workflow's subtree as a flat trace list.
    """
    from aitelier.server import _parse_iso_param, _run_to_trace_dict

    store = await get_store()
    since_dt = _parse_iso_param("since", since)
    flt = RunFilter(
        trace_tag=trace_tag, parent_run_id=parent_run_id,
        status=status, state=state, since=since_dt, limit=limit,
    )
    runs = await store.list_runs(flt)
    return [_run_to_trace_dict(r) for r in runs]


@router.get("/v1/traces/aggregates")
async def traces_aggregates_endpoint(
    group_by: str = "trace_tag",
    since: str | None = None,
    until: str | None = None,
    trace_tag: str | None = None,
) -> dict:
    """Roll up run stats.

    `group_by` ∈ {trace_tag, kind, model, agent_id, status, error_type, day}.
    """
    from aitelier.server import _parse_iso_param

    store = await get_store()
    since_dt = _parse_iso_param("since", since)
    until_dt = _parse_iso_param("until", until)
    try:
        return await store.aggregate_runs(
            group_by=group_by, since=since_dt, until=until_dt, trace_tag=trace_tag,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/v1/traces/{trace_id}")
async def get_trace_endpoint(trace_id: str) -> dict:
    """Get a single trace by ID. Same data as /v1/runs/{id} in TraceRecord shape."""
    from aitelier.server import _run_to_trace_dict, _validate_path_component

    _validate_path_component(trace_id, "trace_id")
    store = await get_store()
    run = await store.get_run(trace_id)
    if not run:
        raise HTTPException(status_code=404, detail="Trace not found")
    return _run_to_trace_dict(run)
