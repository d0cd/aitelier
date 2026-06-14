"""HTTP endpoint modules grouped by resource.

Each module exposes a `router: APIRouter` that the main `server.py`
includes via `app.include_router(...)`. Helpers shared across endpoint
modules (projections, redaction, in-flight registry) currently live in
`server.py` and are imported lazily from there to avoid circular imports.
That asymmetry is fine for now — the goal of this split is to make the
runs/traces/schedules/inference surfaces each readable in isolation,
not to decouple them from shared state entirely.
"""
