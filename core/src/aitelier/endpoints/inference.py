"""`/v1/chat/completions`, `/v1/embeddings`, `/v1/models` — OpenAI-shape inference.

Three handlers register on this module's `router` and the main app
includes it in `server.py`. The agent + LLM execution helpers
(`_agent_chat_completion`, `_llm_chat_completion`, streaming variants,
idempotency wrappers, render helpers) live in `server.py` and are
imported lazily inside each handler — same pattern as the other
`endpoints/*.py` modules.

Endpoints surfaced here:
- POST /v1/chat/completions   — sync + streaming inference (LLM + agent)
- POST /v1/embeddings         — OpenAI-shape batch embeddings (LiteLLM)
- GET  /v1/models             — LLM + agent model inventory
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from aitelier.config import get_config
from aitelier.errors import scrub_error_text
from aitelier.openai_compat import (
    ChatCompletionRequest,
    EmbeddingsRequest,
    chat_completion_error_envelope,
    parse_model_route,
)
from aitelier.providers.llm import LLMError, embeddings, list_models
from aitelier.runner import make_run_id
from aitelier.runs import record_run
from aitelier.storage import RunSpec

logger = logging.getLogger("aitelier")
router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions_endpoint(req: ChatCompletionRequest, request: Request):
    """OpenAI-shape chat completions.

    Routing:
      - `model = "agent:<backend>[/<inner-llm>]"` → Sandbox Agent
      - any other `model` → LiteLLM passthrough

    `aitelier.*` options on the agent path; not accepted on the LLM path.
    """
    from aitelier.server import (
        _agent_chat_completion,
        _agent_chat_completion_stream,
        _check_idempotency,
        _llm_chat_completion,
        _llm_chat_completion_stream,
        _record_idempotency,
        _reject_if_saturated,
        _release_idempotency_ctx,
        _render_chat_completion,
        _replay_cached_stream,
        _validate_aitelier_opts,
    )

    route, agent_backend, inner_llm = parse_model_route(req.model)
    await _validate_aitelier_opts(req, agent_path=(route == "agent"))
    _reject_if_saturated()

    if route == "agent":
        idem = await _check_idempotency(request, "/v1/chat/completions")
        if idem and idem.cached is not None:
            cached = dict(idem.cached)
            if cached.get("_aitelier_stream"):
                return _replay_cached_stream(cached)
            return _render_chat_completion(cached)
        run_id = make_run_id("chat_agent")
        try:
            if req.stream:
                # Stream path's record happens in _agent_chat_completion_stream;
                # if it raises before the record call, fall through to the
                # error-path release below.
                return await _agent_chat_completion_stream(
                    req, request,
                    agent_backend=agent_backend, inner_llm=inner_llm,
                    run_id=run_id, idem=idem,
                )
            result = await _agent_chat_completion(
                req, request,
                agent_backend=agent_backend, inner_llm=inner_llm, run_id=run_id,
            )
            await _record_idempotency(idem, result)
            return _render_chat_completion(result)
        except BaseException:
            # The run failed before _record_idempotency could release the
            # per-key lock — release it explicitly so a retry under the
            # same Idempotency-Key isn't blocked.
            _release_idempotency_ctx(idem)
            raise

    # LLM path
    run_id = make_run_id("chat_llm")
    if req.stream:
        return await _llm_chat_completion_stream(req, request, run_id=run_id)
    result = await _llm_chat_completion(req, request, run_id=run_id)
    return _render_chat_completion(result)


@router.post("/v1/embeddings")
async def embeddings_endpoint(req: EmbeddingsRequest, request: Request):
    """OpenAI-shape embeddings (LiteLLM passthrough)."""
    from aitelier.server import _http_status_for_llm_error, _render_chat_completion

    cid = request.state.correlation_id
    run_id = make_run_id("embed")

    body: dict[str, Any] = {"model": req.model, "input": req.input}
    if req.encoding_format is not None:
        body["encoding_format"] = req.encoding_format
    if req.dimensions is not None:
        body["dimensions"] = req.dimensions
    if req.user is not None:
        body["user"] = req.user

    spec = RunSpec(
        run_id=run_id, kind="embed", model=req.model,
        correlation_id=cid, metadata={"correlation_id": cid},
    )

    async def _do() -> dict:
        try:
            resp = await embeddings(body)
        except LLMError as exc:
            return {
                "status": "error",
                "error_type": exc.error_type, "error_msg": scrub_error_text(str(exc)),
                "_aitelier_http_status": _http_status_for_llm_error(exc),
            }
        if req.encoding_format == "base64":
            _ensure_base64_embeddings(resp)
        resp["aitelier_run_id"] = run_id
        resp["correlation_id"] = cid
        return resp

    result = await record_run(spec, _do())
    if result.get("status") == "error":
        return _render_chat_completion(chat_completion_error_envelope(
            result, run_id=run_id, correlation_id=cid,
        ))
    return result


def _ensure_base64_embeddings(resp: dict) -> None:
    """Honor `encoding_format: "base64"` even when the upstream route
    (Ollama-via-LiteLLM, today) ignored the field and returned floats.
    OpenAI's contract is float32 little-endian packed bytes, base64-encoded.
    Mutates `resp` in place; no-op for entries already encoded."""
    import base64
    import struct
    for entry in resp.get("data") or []:
        emb = entry.get("embedding")
        if isinstance(emb, list) and emb and isinstance(emb[0], (int, float)):
            packed = struct.pack(f"<{len(emb)}f", *emb)
            entry["embedding"] = base64.b64encode(packed).decode("ascii")


@router.get("/v1/models")
async def list_models_endpoint() -> dict:
    """OpenAI-shape model list. Entries fall into two flavors:

    - **LLM**: standard OpenAI shape (`id`, `object: "model"`, `owned_by`).
      `response_format` annotates which `json_object`/`json_schema` modes
      the provider supports.
    - **Agent**: `id = "agent:<backend>"`, `aitelier_agent: true`. Lists
      `aitelier_inner_llms` (the LLM aliases the backend can drive) and
      `aitelier_capabilities` (a subset of Sandbox Agent's capability
      flags). Consumers can validate `agent:<backend>/<inner-llm>`
      strings upfront rather than after a failed run.
    """
    try:
        data = await list_models()
    except LLMError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from None
    cfg = get_config()
    agents = await _list_agent_models(cfg, llm_ids=[m["id"] for m in data])
    return {"object": "list", "data": data + agents}


async def _list_agent_models(cfg, *, llm_ids: list[str]) -> list[dict]:
    """Build agent-model entries by probing Sandbox Agent's /v1/agents.

    Returns an empty list when SA is unreachable — `/v1/models` shouldn't
    fail just because the sandbox is down; LLM models still work. Probe
    failures are logged at WARN so consumers seeing zero agent rows can
    diagnose without having to enable debug logging or read
    `/v1/discovery` — which already carries the structured reason via
    `_probe_sandbox_agent`.
    """
    try:
        from aitelier.providers.llm import get_shared_client
        client = await get_shared_client()
        headers = {}
        if cfg.sandbox_agent.token:
            headers["Authorization"] = f"Bearer {cfg.sandbox_agent.token}"
        resp = await client.get(
            f"{cfg.sandbox_agent.base_url}/v1/agents",
            headers=headers, timeout=3,
        )
        if resp.status_code != 200:
            logger.warning(
                "agent model enumeration: SA /v1/agents returned HTTP %s "
                "from %s — /v1/models will omit agent rows. Check "
                "/v1/discovery → dependencies.sandbox_agent for details.",
                resp.status_code, cfg.sandbox_agent.base_url,
            )
            return []
        raw = resp.json()
    except Exception as exc:
        logger.warning(
            "agent model enumeration: SA probe at %s failed (%s: %s) — "
            "/v1/models will omit agent rows. Check /v1/discovery → "
            "dependencies.sandbox_agent for details.",
            cfg.sandbox_agent.base_url, type(exc).__name__, exc,
        )
        return []

    agents_raw = raw if isinstance(raw, list) else raw.get("agents") or []
    out: list[dict] = []
    for a in agents_raw:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if not a.get("installed", True):
            # Don't advertise uninstalled backends; agent runs against them
            # would fail with NotInstalled. Consumers calling `/v1/discovery`
            # see the full advertised set.
            continue
        out.append({
            "id": f"agent:{a['id']}",
            "object": "model",
            "owned_by": "sandbox-agent",
            "aitelier_agent": True,
            # The backend can drive any chat-capable LLM LiteLLM advertises;
            # consumers can pair as `agent:<backend>/<llm_id>`. The raw
            # LiteLLM catalog includes TTS / image / embedding / moderation
            # routes that can't drive a code agent, so we filter those out
            # before exposing the inner-LLM picklist.
            "aitelier_inner_llms": _filter_chat_capable(llm_ids),
            "aitelier_capabilities": a.get("capabilities") or {},
            # Declarative request-field caps mirroring the agent-path gates
            # enforced by `_reject_agent_incompatible_fields`. Generic
            # consumers (model pickers, doctor probes) can pre-strip
            # request fields from the catalog instead of waiting for a 400.
            "aitelier_request_caps": {
                "tools": False,
                "tool_choice": False,
                "n_gt_1": False,
                "top_p": False,
                "streaming": True,
                "response_format": ["json_schema"],
            },
        })
    return sorted(out, key=lambda m: m["id"])


# Model-id substrings that mark a route as not a valid inner LLM for a
# code agent. Covers non-chat modalities (TTS, image, embeddings, audio
# transcription, moderation, video) plus chat models with constrained
# variants the agent harness can't drive (realtime voice / web-search
# preview endpoints that change the response shape).
_NON_CHAT_MODEL_MARKERS = (
    "tts", "whisper", "dall-e", "image", "embed", "moderation",
    "audio", "transcribe", "speech", "sora",
    "realtime", "-search-preview",
)


def _filter_chat_capable(model_ids: list[str]) -> list[str]:
    return [m for m in model_ids
            if not any(marker in m.lower() for marker in _NON_CHAT_MODEL_MARKERS)]
