"""HTTP endpoint modules grouped by resource.

Each module exposes a `router: APIRouter` that the main `server.py`
includes via `app.include_router(...)`. Helpers shared across endpoint
modules live in dedicated leaf modules — projections/redaction in
`serializers.py`, the in-flight registry + SSE/webhook infra in
`runtime.py`, inference execution in `inference_exec.py` — and are
re-exported through `server.py`. Handlers import them lazily from
`aitelier.server` so the router-registration import cycle stays broken.
"""
