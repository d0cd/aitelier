"""`/v1/schedules/*` endpoints — durable schedule registry.

Routes registered on this module's `router` and included into the main
app in `server.py`. The schedule tick loop itself lives in
`aitelier.schedules`; this module only exposes the CRUD surface.

Endpoints surfaced here:
- GET    /v1/schedules                   — list persisted schedules
- POST   /v1/schedules                   — register a schedule
- GET    /v1/schedules/{schedule_id}     — fetch one
- DELETE /v1/schedules/{schedule_id}     — delete (returns 404 if missing)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aitelier.openai_compat import ScheduleRequest
from aitelier.schedules import (
    create_schedule,
    delete_schedule,
    get_schedule,
    list_schedules,
)
from aitelier.security import validate_path_component

router = APIRouter()


@router.get("/v1/schedules")
async def list_schedules_endpoint() -> list[dict]:
    """List persisted schedules."""
    return await list_schedules()


@router.post("/v1/schedules")
async def create_schedule_endpoint(req: ScheduleRequest) -> dict:
    """Register a recurring or one-shot scheduled task."""
    from aitelier.server import _check_webhook_url_or_die

    if req.webhook_url:
        await _check_webhook_url_or_die(req.webhook_url)
    try:
        return await create_schedule(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/v1/schedules/{schedule_id}")
async def get_schedule_endpoint(schedule_id: str) -> dict:

    validate_path_component(schedule_id, "schedule_id")
    entry = await get_schedule(schedule_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
    return entry


@router.delete("/v1/schedules/{schedule_id}")
async def delete_schedule_endpoint(schedule_id: str) -> dict:

    validate_path_component(schedule_id, "schedule_id")
    if not await delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
    return {"id": schedule_id, "deleted": True}
