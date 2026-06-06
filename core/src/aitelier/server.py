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


class _JsonFormatter(logging.Formatter):
    """One-line-per-record JSON formatter for AITELIER_LOG_FORMAT=json.

    Aggregator-friendly (Loki, Datadog, etc.). Includes correlation_id from
    the contextvar that the LogRecord factory stamped onto every record.
    """

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return _json.dumps(payload, default=str)


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
        h.setFormatter(_active_formatter())
        root.addHandler(h)
        root.setLevel(logging.INFO)

    _retag_uvicorn_handlers()


def _active_formatter() -> logging.Formatter:
    """Pick the formatter once based on AITELIER_LOG_FORMAT.

    `json` → one-line JSON per record (aggregator-friendly).
    anything else → human-readable with [correlation_id] prefix.
    """
    import os
    if os.environ.get("AITELIER_LOG_FORMAT", "").lower() == "json":
        return _JsonFormatter()
    return logging.Formatter(_AITELIER_LOG_FORMAT)


def _retag_uvicorn_handlers() -> None:
    """Override the formatters on uvicorn's loggers so access + error lines
    carry the same correlation_id prefix as aitelier's logs."""
    fmt = _active_formatter()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.setFormatter(fmt)


_install_correlation_logging()


async def _schedule_handler(entry: dict) -> None:
    """Run one fired schedule. Records trace + posts to webhook if set."""
    from aitelier.runner import execute as run_execute
    from aitelier.runner import make_run_id

    task = dict(entry.get("task") or {})
    run_id = make_run_id(entry.get("name", "scheduled"))
    started_at = _now_iso()
    meta = dict(task.get("metadata") or {})
    meta["schedule_id"] = entry["id"]
    task["metadata"] = meta
    try:
        result = await run_execute(task, run_id=run_id)
    except Exception as exc:
        logger.warning("Scheduled run %s failed: %s", run_id, exc)
        result = {
            "kind": task.get("kind", "agent"),
            "provider": task.get("model", ""),
            "status": "error",
            "run_id": run_id,
            "trace_id": run_id,
            "error_type": type(exc).__name__,
            "error_msg": str(exc),
            "finish_reason": "error",
        }
    try:
        record_trace(
            trace_id=run_id, started_at=started_at, result=result,
            system_prompt=task.get("system_prompt"),
            trace_tag=task.get("trace_tag"),
            metadata={"schedule_id": entry["id"]},
        )
    except Exception as exc:
        logger.warning("Failed to record schedule trace: %s", exc)
    if entry.get("webhook_url"):
        await _post_webhook(entry["webhook_url"], {
            "schedule_id": entry["id"],
            "run_id": run_id,
            "result": result,
        })


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

    # Start the persistent schedule tick loop
    from aitelier.schedules import start_tick_loop, stop_tick_loop
    start_tick_loop(_schedule_handler)

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

    # Stop the schedule tick loop.
    stop_tick_loop()

    # Release the shared HTTP client pool.
    from aitelier.providers.llm import close_shared_client
    await close_shared_client()


app = FastAPI(title="aitelier", version="0.1.0", lifespan=lifespan)


_AUTH_EXEMPT_PATHS = {"/v1/health"}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Gate every /v1/* endpoint on Authorization: Bearer <api_key> *if*
    service.api_key is configured. When unset (default), no auth is enforced
    — preserves the localhost-trust model.

    /v1/health is always public so liveness probes (k8s, load balancers)
    can hit it without a token.
    """
    from fastapi.responses import JSONResponse

    from aitelier.config import get_config

    if request.url.path not in _AUTH_EXEMPT_PATHS:
        configured = get_config().service.api_key
        if configured:
            auth = request.headers.get("Authorization") or ""
            if not auth.startswith("Bearer ") or auth[7:] != configured:
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401,
                )
    return await call_next(request)


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
    examples: list[dict] | None = None
    """Few-shot examples as a list of {user, assistant} pairs. The server
    folds them into the system prompt under an `## Examples` heading; the
    underlying ACP protocol has no examples primitive yet."""
    mcp_servers: list[dict] | None = None
    tool_allowlist: list[str] | None = None
    response_format: dict | None = None
    max_turns: int | None = None
    timeout: int | None = None
    workspace: str | None = None
    workspace_mode: str = "copy"
    trace_tag: str | None = None
    metadata: dict[str, Any] | None = None
    mode: str = "sync"   # "sync" (block + return result) | "async" (return run_id, webhook on done)
    webhook_url: str | None = None


class FanoutRequest(BaseModel):
    task: TaskSpec
    providers: list[str]
    max_concurrent: int = Field(default=4, ge=1, le=16)


class AgentPreviewRequest(BaseModel):
    mcp_servers: list[dict] | None = None
    tool_allowlist: list[str] | None = None


class ScheduleRequest(BaseModel):
    name: str = "scheduled"
    task: dict  # TaskSpec dict; validated downstream by the runner
    interval_seconds: int | None = None
    at_iso: str | None = None
    webhook_url: str | None = None


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


async def _post_webhook(url: str, payload: dict) -> None:
    """Best-effort POST to a consumer-supplied webhook URL. Never raises."""
    try:
        from aitelier.providers.llm import get_shared_client
        client = await get_shared_client()
        resp = await client.post(url, json=payload, timeout=10)
        if resp.status_code >= 400:
            logger.warning("Webhook %s returned %s", url, resp.status_code)
    except Exception as exc:
        logger.warning("Webhook POST to %s failed: %s", url, exc)


def _fold_examples(system_prompt: str | None, examples: list[dict] | None) -> str | None:
    """Server-side concat of few-shot examples into the system prompt.

    Each example is a dict with `user` and `assistant` keys. Returns None
    only if both inputs were None/empty.
    """
    if not examples:
        return system_prompt
    blocks: list[str] = []
    for ex in examples:
        u = ex.get("user", "") if isinstance(ex, dict) else ""
        a = ex.get("assistant", "") if isinstance(ex, dict) else ""
        blocks.append(f"User: {u}\nAssistant: {a}")
    section = "## Examples\n\n" + "\n\n".join(blocks)
    return f"{system_prompt}\n\n{section}" if system_prompt else section


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
    system_prompt = _fold_examples(req.system_prompt, req.examples)
    task = {
        "name": "agent_run",
        "kind": "agent",
        "model": req.model,
        "prompt": req.initial_message or "",
        "system_prompt": system_prompt,
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

    # --- Async mode: return run_id immediately, fire webhook on done ---
    if req.mode == "async":
        webhook_url = req.webhook_url

        async def _run_and_callback() -> None:
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
            if webhook_url:
                await _post_webhook(webhook_url, result)

        asyncio.create_task(_run_and_callback())
        return {
            "run_id": run_id,
            "trace_id": run_id,
            "status": "accepted",
            "correlation_id": cid,
            "webhook_url": webhook_url,
        }

    # --- Sync mode (default): block until done ---
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


@app.post("/v1/agent/stream")
async def run_agent_stream_endpoint(req: RunAgentRequest, request: Request):
    """Streaming agent run via Server-Sent Events.

    Mirrors /v1/complete/stream. Events:
      agent.delta       — incremental text chunk
      agent.tool_call   — agent invoked an MCP tool (server + tool + input)
      agent.tool_result — MCP tool returned (tool + output + elapsed_ms)
      agent.done        — final aggregated Result dict
      agent.error       — terminal error
    """
    from aitelier.providers.sandbox_agent import call_via_sandbox_stream
    from aitelier.runner import make_run_id

    cid = request.state.correlation_id
    run_id = make_run_id("agent_stream")
    started_at = _now_iso()
    system_prompt = _fold_examples(req.system_prompt, req.examples)

    async def event_generator():
        final: dict | None = None
        try:
            async for event in call_via_sandbox_stream(
                req.model,
                req.initial_message or "",
                workspace=req.workspace,
                system_prompt=system_prompt,
                mcp_servers=req.mcp_servers,
                response_format=req.response_format,
                timeout=req.timeout or 600,
                run_id=run_id,
            ):
                evt_type = event["type"]
                event["correlation_id"] = cid
                event["run_id"] = run_id
                if evt_type == "done":
                    final = {k: v for k, v in event.items() if k != "type"}
                yield _sse_event(f"agent.{evt_type}", event)
        except Exception as exc:
            final = {
                "kind": "agent",
                "provider": req.model,
                "status": "error",
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "finish_reason": "error",
            }
            yield _sse_event("agent.error", {
                "type": "error",
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "correlation_id": cid,
                "run_id": run_id,
            })
        finally:
            if final is not None:
                record_trace(
                    trace_id=run_id,
                    started_at=started_at,
                    result=final,
                    system_prompt=system_prompt,
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


async def _query_mcp_tools(server: dict) -> dict:
    """Hit one MCP server's tools/list. Returns {name, transport, tools|reason}."""
    name = server.get("name", "")
    transport = server.get("transport", "http")
    base = {"name": name, "transport": transport}

    if transport != "http":
        # stdio servers can't be previewed without spawning the process
        return {**base, "previewable": False,
                "reason": f"{transport} transport — preview unsupported"}

    url = server.get("url")
    if not url:
        return {**base, "previewable": False, "reason": "no url"}

    try:
        from aitelier.providers.llm import get_shared_client
        client = await get_shared_client()
        resp = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        if resp.status_code != 200:
            return {**base, "previewable": True, "reachable": False,
                    "reason": f"HTTP {resp.status_code}"}
        data = resp.json()
        if "error" in data:
            err = data["error"]
            return {**base, "previewable": True, "reachable": False,
                    "reason": f"MCP error {err.get('code')}: {err.get('message','')}"}
        tools = data.get("result", {}).get("tools") or []
        # Prefix each tool name with the server name to match aitelier's
        # allowlist convention: "server.tool"
        tool_names = sorted(f"{name}.{t['name']}" for t in tools if t.get("name"))
        return {**base, "previewable": True, "reachable": True, "tools": tool_names}
    except Exception as exc:
        return {**base, "previewable": True, "reachable": False,
                "reason": f"{type(exc).__name__}: {exc}"}


@app.post("/v1/agent/preview")
async def agent_preview_endpoint(req: AgentPreviewRequest) -> dict:
    """Dry-run resolution of MCP servers + tool_allowlist.

    Queries each HTTP MCP server's tools/list and reports:
      - per server: reachability + the tool names it advertises
      - which allowlist entries match a discovered tool (matches)
      - which allowlist entries match nothing (misses — likely typos)
      - which available tools are not in the allowlist (unused)

    stdio MCP servers can't be previewed without spawning the process;
    they're returned with previewable=false.
    """
    servers_info = await asyncio.gather(
        *(_query_mcp_tools(s) for s in (req.mcp_servers or []))
    ) if req.mcp_servers else []

    available: set[str] = set()
    for s in servers_info:
        for t in s.get("tools") or []:
            available.add(t)

    allowlist = list(req.tool_allowlist or [])
    matches = sorted(t for t in allowlist if t in available)
    misses = sorted(t for t in allowlist if t not in available)
    unused = sorted(available - set(allowlist)) if allowlist else []

    return {
        "servers": servers_info,
        "allowlist_matches": matches,
        "allowlist_misses": misses,
        "unused_tools": unused,
    }


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


@app.get("/v1/traces/aggregates")
async def traces_aggregates_endpoint(
    group_by: str = "trace_tag",
    since: str | None = None,
    until: str | None = None,
    trace_tag: str | None = None,
) -> dict:
    """Roll up trace stats. `group_by` ∈ {trace_tag, kind, model, status, error_type, day}."""
    from aitelier.traces import aggregate_traces
    try:
        return aggregate_traces(
            group_by=group_by, since=since, until=until, trace_tag=trace_tag,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


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


@app.get("/v1/schedules")
async def list_schedules_endpoint() -> list[dict]:
    """List persisted schedules."""
    from aitelier.schedules import list_schedules
    return list_schedules()


@app.post("/v1/schedules")
async def create_schedule_endpoint(req: ScheduleRequest) -> dict:
    """Register a recurring or one-shot scheduled task."""
    from aitelier.schedules import create_schedule
    try:
        return create_schedule(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/v1/schedules/{schedule_id}")
async def get_schedule_endpoint(schedule_id: str) -> dict:
    _validate_path_component(schedule_id, "schedule_id")
    from aitelier.schedules import get_schedule
    entry = get_schedule(schedule_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
    return entry


@app.delete("/v1/schedules/{schedule_id}")
async def delete_schedule_endpoint(schedule_id: str) -> dict:
    _validate_path_component(schedule_id, "schedule_id")
    from aitelier.schedules import delete_schedule
    if not delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
    return {"id": schedule_id, "deleted": True}


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
