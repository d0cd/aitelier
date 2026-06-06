"""FastAPI HTTP service for aitelier."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aitelier.traces import record_trace

logger = logging.getLogger("aitelier")


# Correlation ID is set per-request by middleware and propagates through
# any logging done inside that request's task tree via contextvars.
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-",
)


_AITELIER_LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
)
# uvicorn.access passes its data as positional args inside the message
# template ('%s - "%s %s HTTP/%s" %d'), not as named record attributes.
# Use %(message)s — getMessage() folds the args in before formatting.


def _install_correlation_logging() -> None:
    """Install a LogRecord factory that stamps every record with the current
    request's correlation_id, then align uvicorn's handler formatters so
    their output carries the same prefix as aitelier's own logs.
    """
    if getattr(_install_correlation_logging, "_installed", False):
        # Even if already installed, re-apply uvicorn formatters in case
        # uvicorn was re-imported (e.g., during dev reload).
        _retag_uvicorn_handlers()
        return

    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        record.correlation_id = _correlation_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _install_correlation_logging._installed = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(_AITELIER_LOG_FORMAT))
        root.addHandler(h)
        root.setLevel(logging.INFO)

    _retag_uvicorn_handlers()


def _retag_uvicorn_handlers() -> None:
    """Override the formatters on uvicorn's loggers so access + error lines
    carry the same correlation_id prefix as aitelier's logs."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.setFormatter(logging.Formatter(_AITELIER_LOG_FORMAT))


_install_correlation_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-apply formatters to uvicorn loggers; they're configured by uvicorn
    # *after* this module is imported, so the import-time tag misses them.
    _retag_uvicorn_handlers()

    # Purge old traces on startup
    from aitelier.traces import purge_traces
    deleted = purge_traces(max_age_days=30)
    if deleted:
        logger.info("Purged %d traces older than 30 days", deleted)

    # Health check LiteLLM proxy
    from aitelier.config import get_config
    cfg = get_config()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{cfg.litellm.base_url}/health",
                headers={"Authorization": f"Bearer {cfg.litellm.api_key}"},
            )
            resp.raise_for_status()
            logger.info("LiteLLM proxy reachable at %s", cfg.litellm.base_url)
    except Exception as exc:
        logger.warning("LiteLLM proxy unreachable at %s: %s", cfg.litellm.base_url, exc)

    yield

    # Graceful shutdown: cancel any in-flight runs so SSE consumers see a
    # clean run.cancelled rather than a dropped connection. Best-effort —
    # we don't wait long for cleanup since the process is about to exit.
    if _active_runs:
        logger.info("Cancelling %d in-flight run(s) on shutdown", len(_active_runs))
        for task in list(_active_runs.values()):
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*_active_runs.values(), return_exceptions=True),
                timeout=2.0,
            )
        except TimeoutError:
            logger.warning("Some runs did not cleanly cancel within 2s")

    # Release the shared HTTP client pool.
    from aitelier.providers.llm import close_shared_client
    await close_shared_client()


app = FastAPI(title="aitelier", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _correlation_id_middleware(request: Request, call_next):
    """Echo or generate X-Correlation-Id so consumers can tie their logs to ours."""
    cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    request.state.correlation_id = cid
    token = _correlation_id_var.set(cid)
    try:
        response = await call_next(request)
    finally:
        _correlation_id_var.reset(token)
    response.headers["X-Correlation-Id"] = cid
    return response


# Per-process registry of in-flight runs, for cancellation.
# Single-process assumption; if aitelier ever scales horizontally this
# moves to a shared store.
_active_runs: dict[str, asyncio.Task] = {}


def _cancelled_result(run_id: str, kind: str) -> dict:
    """Result shape returned when a run is cancelled mid-flight."""
    return {
        "kind": kind,
        "provider": "",
        "status": "error",
        "duration_s": 0.0,
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "cancelled",
        "cost_usd": None,
        "error_type": "Cancelled",
        "error_msg": "run cancelled",
    }


# --- Request/Response models ---


class TaskSpec(BaseModel):
    name: str
    kind: str
    model: str | None = None
    system_prompt: str | None = None
    messages: list[dict] | None = None
    prompt: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict | None = None
    texts: list[str] | None = None
    mcp_servers: list[dict] | None = None
    tool_allowlist: list[str] | None = None
    max_turns: int | None = None
    workspace: str | None = None
    workspace_mode: str = "copy"
    preferred_providers: list[str] | None = None
    timeout: int | None = None
    trace_tag: str | None = None
    metadata: dict[str, Any] | None = None


class CompleteRequest(BaseModel):
    model: str
    system_prompt: str | None = None
    messages: list[dict]
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict | None = None
    timeout: int | None = None
    trace_tag: str | None = None


class EmbedRequest(BaseModel):
    texts: list[str]
    model: str | None = None
    timeout: int | None = None


class RunAgentRequest(BaseModel):
    model: str
    system_prompt: str | None = None
    initial_message: str | None = None
    mcp_servers: list[dict] | None = None
    tool_allowlist: list[str] | None = None
    response_format: dict | None = None
    max_turns: int | None = None
    timeout: int | None = None
    workspace: str | None = None
    workspace_mode: str = "copy"
    trace_tag: str | None = None
    metadata: dict[str, Any] | None = None


class FanoutRequest(BaseModel):
    task: TaskSpec
    providers: list[str]
    max_concurrent: int = Field(default=4, ge=1, le=16)


class TracesFilter(BaseModel):
    since: str | None = None
    trace_tag: str | None = None
    status: str | None = None
    limit: int = 50


# --- Primitive endpoints (deepread contract) ---


def _merge_correlation(metadata: dict | None, cid: str) -> dict:
    out = dict(metadata or {})
    out["correlation_id"] = cid
    return out


@app.post("/v1/complete")
async def complete_endpoint(req: CompleteRequest, request: Request) -> dict:
    """Single-shot chat completion."""
    from aitelier.providers.llm import complete
    from aitelier.runner import make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id("complete")
    started_at = _now_iso()

    result = await complete(
        model=req.model,
        messages=req.messages,
        system_prompt=req.system_prompt,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        response_format=req.response_format,
        timeout=req.timeout or 60,
        run_id=run_id,
        trace_tag=req.trace_tag,
    )
    result["correlation_id"] = cid
    record_trace(
        trace_id=run_id,
        started_at=started_at,
        result=result,
        system_prompt=req.system_prompt,
        trace_tag=req.trace_tag,
        metadata={"correlation_id": cid},
    )
    return result


@app.post("/v1/complete/stream")
async def complete_stream_endpoint(req: CompleteRequest, request: Request):
    """Streaming chat completion via Server-Sent Events.

    Events:
      complete.delta — incremental content piece
      complete.done  — final aggregated result with usage, finish_reason
      complete.error — terminal error
    """
    from aitelier.providers.llm import complete_stream
    from aitelier.runner import make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id("complete_stream")
    started_at = _now_iso()

    async def event_generator():
        final: dict | None = None
        try:
            async for event in complete_stream(
                model=req.model,
                messages=req.messages,
                system_prompt=req.system_prompt,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                response_format=req.response_format,
                timeout=req.timeout or 60,
                run_id=run_id,
            ):
                # Keep `type` in the payload so the data alone is a tagged
                # union per schemas/v1/complete_stream_event.schema.json.
                evt_type = event["type"]
                event["correlation_id"] = cid
                if evt_type == "done":
                    final = {
                        "kind": "complete",
                        "provider": req.model,
                        "status": "ok",
                        **{k: v for k, v in event.items() if k != "type"},
                    }
                yield _sse_event(f"complete.{evt_type}", event)
        except Exception as exc:
            final = {
                "kind": "complete",
                "provider": req.model,
                "status": "error",
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "finish_reason": "error",
            }
            yield _sse_event("complete.error", {
                "type": "error",
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "correlation_id": cid,
            })
        finally:
            if final is not None:
                record_trace(
                    trace_id=run_id,
                    started_at=started_at,
                    result=final,
                    system_prompt=req.system_prompt,
                    trace_tag=req.trace_tag,
                    metadata={"correlation_id": cid},
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/embed")
async def embed_endpoint(req: EmbedRequest, request: Request) -> dict:
    """Batch embedding."""
    from aitelier.providers.llm import embed
    from aitelier.runner import make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id("embed")
    started_at = _now_iso()

    result = await embed(
        texts=req.texts,
        model=req.model,
        timeout=req.timeout or 30,
        run_id=run_id,
    )
    result["correlation_id"] = cid
    record_trace(
        trace_id=run_id,
        started_at=started_at,
        result=result,
        metadata={"correlation_id": cid},
    )
    return result


@app.post("/v1/agent")
async def run_agent_endpoint(req: RunAgentRequest, request: Request) -> dict:
    """Run an agent with MCP tool loop."""
    from aitelier.runner import execute, make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id("agent_run")
    task = {
        "name": "agent_run",
        "kind": "agent",
        "model": req.model,
        "prompt": req.initial_message or "",
        "system_prompt": req.system_prompt,
        "mcp_servers": req.mcp_servers,
        "tool_allowlist": req.tool_allowlist,
        "response_format": req.response_format,
        "max_turns": req.max_turns,
        "timeout": req.timeout,
        "workspace": req.workspace,
        "workspace_mode": req.workspace_mode,
        "trace_tag": req.trace_tag,
        "metadata": _merge_correlation(req.metadata, cid),
    }

    run_task = asyncio.create_task(execute(task, run_id=run_id))
    _active_runs[run_id] = run_task
    try:
        result = await run_task
    except asyncio.CancelledError:
        if not run_task.cancelled():
            run_task.cancel()
        result = _cancelled_result(run_id, "agent")
    finally:
        _active_runs.pop(run_id, None)

    result["correlation_id"] = cid
    return result


@app.get("/v1/traces")
async def traces_endpoint(
    since: str | None = None,
    trace_tag: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query recent traces."""
    from aitelier.traces import recent_traces

    return recent_traces(
        since=since,
        trace_tag=trace_tag,
        status=status,
        limit=limit,
    )


@app.get("/v1/traces/{trace_id}")
async def get_trace_endpoint(trace_id: str) -> dict:
    """Get a single trace by ID."""
    from aitelier.traces import get_trace

    trace = get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
    return trace


# --- Task runner endpoints (fan-out, legacy) ---


@app.post("/v1/execute")
async def execute(task: TaskSpec, request: Request) -> dict:
    from aitelier.runner import execute as run_execute
    from aitelier.runner import make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id(task.name)
    task_dict = task.model_dump(exclude_none=True)
    task_dict["metadata"] = _merge_correlation(task_dict.get("metadata"), cid)

    run_task = asyncio.create_task(run_execute(task_dict, run_id=run_id))
    _active_runs[run_id] = run_task
    try:
        result = await run_task
    except asyncio.CancelledError:
        if not run_task.cancelled():
            run_task.cancel()
        result = _cancelled_result(run_id, task.kind)
    finally:
        _active_runs.pop(run_id, None)

    result["correlation_id"] = cid
    return result


@app.post("/v1/execute/stream")
async def execute_stream(task: TaskSpec, request: Request):
    from aitelier.runner import execute as run_execute
    from aitelier.runner import make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id(task.name)
    task_dict = task.model_dump(exclude_none=True)
    task_dict["metadata"] = _merge_correlation(task_dict.get("metadata"), cid)

    async def event_generator():
        yield _sse_event("run.started", {
            "task": task.name,
            "kind": task.kind,
            "run_id": run_id,
            "timestamp": _now_iso(),
            "correlation_id": cid,
        })
        run_task = asyncio.create_task(run_execute(task_dict, run_id=run_id))
        _active_runs[run_id] = run_task
        try:
            result = await run_task
            result["correlation_id"] = cid
            yield _sse_event("run.completed", result)
        except asyncio.CancelledError:
            if not run_task.cancelled():
                run_task.cancel()
            yield _sse_event("run.cancelled", {
                "run_id": run_id,
                "correlation_id": cid,
                "timestamp": _now_iso(),
            })
        except Exception as exc:
            yield _sse_event("run.error", {
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "timestamp": _now_iso(),
                "correlation_id": cid,
                "run_id": run_id,
            })
        finally:
            _active_runs.pop(run_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/fanout")
async def fanout_endpoint(req: FanoutRequest, request: Request) -> list[dict]:
    from aitelier.fanout import fanout

    cid = request.state.correlation_id
    task_dict = req.task.model_dump(exclude_none=True)
    task_dict["metadata"] = _merge_correlation(task_dict.get("metadata"), cid)
    results = await fanout(
        task_dict,
        providers=req.providers,
        max_concurrent=req.max_concurrent,
    )
    for r in results:
        if isinstance(r, dict):
            r["correlation_id"] = cid
    return results


def _validate_path_component(value: str, label: str) -> None:
    """Reject path traversal attempts in user-supplied path components."""
    import re
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value!r}")
    if ".." in value:
        raise HTTPException(status_code=400, detail=f"Invalid {label}: path traversal not allowed")


@app.get("/v1/runs/active")
async def list_active_runs() -> dict:
    """List run_ids currently in-flight in this server process."""
    return {"active": sorted(_active_runs.keys())}


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    """Signal cancellation for an in-flight run.

    Returns 404 if the run isn't currently active (already finished or
    never existed). The owning request will receive a result with
    `status: "error"`, `error_type: "Cancelled"`.
    """
    _validate_path_component(run_id, "run_id")
    task = _active_runs.get(run_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Run not active: {run_id}")
    task.cancel()
    return {"run_id": run_id, "cancelled": True}


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    _validate_path_component(run_id, "run_id")

    runs_base = Path("runs").resolve()
    run_dir = (runs_base / run_id).resolve()

    # Defense in depth: ensure resolved path is under runs/
    if not str(run_dir).startswith(str(runs_base)):
        raise HTTPException(status_code=400, detail="Invalid run_id")

    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail=f"No manifest for run: {run_id}")

    manifest = json.loads(manifest_path.read_text())
    results = []
    for entry in manifest.get("results", []):
        provider = entry.get("provider", "")
        # Sanitize provider name before using in path
        provider_safe = provider.replace("/", "_").replace("..", "_")
        result_path = run_dir / provider_safe / "result.json"
        if result_path.resolve().is_relative_to(run_dir) and result_path.exists():
            results.append(json.loads(result_path.read_text()))
        else:
            results.append(entry)
    manifest["results"] = results

    prompt_path = run_dir / "prompt.txt"
    if prompt_path.exists():
        manifest["prompt"] = prompt_path.read_text()

    return manifest


_KNOWN_LIMITATIONS = [
    "agent cost_usd is always null — only complete/embed track cost",
    "traces are purged after 30 days on server startup",
]


@app.get("/v1/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "timestamp": _now_iso(),
        "known_limitations": _KNOWN_LIMITATIONS,
    }


# --- Discovery ---


@functools.lru_cache(maxsize=1)
def _schemas_dir() -> Path:
    """Locate schemas/v1 dir. Prefer source-relative; fall back to cwd."""
    candidate = Path(__file__).resolve().parents[3] / "schemas" / "v1"
    if candidate.exists():
        return candidate
    return Path("schemas/v1").resolve()


@functools.lru_cache(maxsize=64)
def _load_schema(path: Path) -> dict:
    """Cached read+parse of a schema file. Schemas are immutable at runtime."""
    return json.loads(path.read_text())


def _list_endpoints() -> list[dict]:
    """Enumerate live HTTP endpoints from the FastAPI app — single source of truth."""
    from fastapi.routing import APIRoute
    out: list[dict] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            out.append({"method": method, "path": route.path})
    return sorted(out, key=lambda e: (e["path"], e["method"]))


def _list_schemas() -> dict[str, str]:
    """Map schema name → fetch URL. Empty dict if schemas dir is missing."""
    d = _schemas_dir()
    if not d.exists():
        return {}
    out: dict[str, str] = {}
    suffix = ".schema.json"
    for f in sorted(d.glob(f"*{suffix}")):
        name = f.name[: -len(suffix)]
        out[name] = f"/v1/schemas/{name}"
    return out


async def _probe_litellm(cfg) -> dict:
    """Live probe: LiteLLM /v1/models. Returns reachability + model list."""
    try:
        from aitelier.providers.llm import get_shared_client
        client = await get_shared_client()
        resp = await client.get(
            f"{cfg.litellm.base_url}/v1/models",
            headers={"Authorization": f"Bearer {cfg.litellm.api_key}"},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")
            )
            return {"reachable": True, "base_url": cfg.litellm.base_url, "models": models}
        return {
            "reachable": False,
            "base_url": cfg.litellm.base_url,
            "reason": f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "reachable": False,
            "base_url": cfg.litellm.base_url,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _probe_traces() -> dict:
    """Live probe: trace store queryable."""
    try:
        from aitelier.traces import recent_traces
        recent_traces(limit=1)
        return {"available": True}
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


async def _probe_sandbox_agent(cfg) -> dict:
    """Live probe: Sandbox Agent reachability + available agent backends.

    Hits GET /v1/agents on the sandbox-agent server (Rivet). Returns the list
    of agent IDs the sandbox advertises (claude-code, codex, opencode, ...).
    """
    try:
        from aitelier.providers.llm import get_shared_client
        headers = {}
        if cfg.sandbox_agent.token:
            headers["Authorization"] = f"Bearer {cfg.sandbox_agent.token}"
        client = await get_shared_client()
        resp = await client.get(
            f"{cfg.sandbox_agent.base_url}/v1/agents",
            headers=headers,
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            # /v1/agents returns either a list or {"agents": [...]} — accept both
            raw = data if isinstance(data, list) else data.get("agents") or []
            agents = sorted(
                a["id"] if isinstance(a, dict) else a
                for a in raw
                if (isinstance(a, dict) and a.get("id")) or isinstance(a, str)
            )
            return {
                "reachable": True,
                "base_url": cfg.sandbox_agent.base_url,
                "agents": agents,
            }
        return {
            "reachable": False,
            "base_url": cfg.sandbox_agent.base_url,
            "reason": f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {
            "reachable": False,
            "base_url": cfg.sandbox_agent.base_url,
            "reason": f"{type(exc).__name__}: {exc}",
        }


_DISCOVERY_TTL_SECONDS = 5.0
_discovery_cache: dict[str, Any] = {"at": 0.0, "value": None}
_discovery_lock = asyncio.Lock()


@app.get("/v1/discovery")
async def discovery() -> dict:
    """Capability + endpoint inventory for peer services.

    Probes dependencies live (parallel) with a short TTL cache so a polling
    peer doesn't repeatedly hit LiteLLM and Sandbox Agent. Keep /v1/health
    cheap for liveness; use this for self-discovery.
    """
    import time as _time

    from aitelier.config import get_config

    now = _time.monotonic()
    if _discovery_cache["value"] is not None and \
       now - _discovery_cache["at"] < _DISCOVERY_TTL_SECONDS:
        return _discovery_cache["value"]

    async with _discovery_lock:
        # Re-check after lock — another request may have refreshed it
        now = _time.monotonic()
        if _discovery_cache["value"] is not None and \
           now - _discovery_cache["at"] < _DISCOVERY_TTL_SECONDS:
            return _discovery_cache["value"]

        cfg = get_config()

        litellm_info, sandbox_info = await asyncio.gather(
            _probe_litellm(cfg),
            _probe_sandbox_agent(cfg),
        )
        traces_info = _probe_traces()

        litellm_ok = litellm_info["reachable"]
        sandbox_ok = sandbox_info["reachable"]

        def cap(ok: bool, reason: str) -> dict:
            return {"available": True} if ok else {"available": False, "reason": reason}

        result = {
            "service": "aitelier",
            "version": app.version,
            "api_version": "v1",
            "timestamp": _now_iso(),
            "endpoints": _list_endpoints(),
            "capabilities": {
                "complete": cap(litellm_ok, "LiteLLM proxy unreachable"),
                "embed": cap(litellm_ok, "LiteLLM proxy unreachable"),
                "agent": cap(sandbox_ok, "Sandbox Agent unreachable"),
                "traces": traces_info,
            },
            "dependencies": {
                "litellm": litellm_info,
                "sandbox_agent": sandbox_info,
            },
            "schemas": _list_schemas(),
            "known_limitations": _KNOWN_LIMITATIONS,
        }
        _discovery_cache["value"] = result
        _discovery_cache["at"] = _time.monotonic()
        return result


@app.get("/v1/schemas/{name}")
async def get_schema(name: str) -> dict:
    """Fetch a JSON Schema by name (without the `.schema.json` suffix)."""
    _validate_path_component(name, "schema name")
    d = _schemas_dir().resolve()
    f = (d / f"{name}.schema.json").resolve()
    # Defense in depth: ensure resolved path stays inside schemas dir
    if not str(f).startswith(str(d) + "/") and f != d:
        raise HTTPException(status_code=400, detail="Invalid schema name")
    try:
        return _load_schema(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Schema not found: {name}") from None


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
