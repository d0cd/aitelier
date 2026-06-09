"""Sandbox-agent workflow helpers — install, commands, files, sidecars, artifacts.

Aitelier consolidates SA primitives into one agent workflow (install →
commands → file seed → sidecars → ACP agent run → artifact fetch).
The agent path of `/v1/chat/completions` composes them around the ACP
call via the `aitelier.prepare` + `aitelier.artifacts` request fields.

Edge cases beyond the workflow reach SA directly via the URL in
`/v1/discovery`; this module is the workflow, not a generic passthrough.
"""

from __future__ import annotations

from fastapi import HTTPException

from aitelier.config import get_config
from aitelier.security import validate_path_component


async def sa_proxy(
    method: str, path: str, *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict | bytes:
    """Thin pass-through to Sandbox Agent. Forwards method/path/body/params,
    auth, errors. Returns parsed JSON for application/json responses or raw
    bytes for everything else (binary file reads).

    Raises HTTPException with SA's status code on 4xx/5xx so the consumer
    sees the real error, not a wrapped 502.
    """
    # Late import so test patches on `aitelier.providers.llm.get_shared_client`
    # take effect even when this module was imported before the patch was applied.
    from aitelier.providers.llm import get_shared_client

    cfg = get_config().sandbox_agent
    client = await get_shared_client()
    headers: dict[str, str] = {}
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    try:
        resp = await client.request(
            method, f"{cfg.base_url}{path}",
            json=json_body, params=params,
            headers=headers, timeout=timeout,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Sandbox Agent unreachable: {type(exc).__name__}: {exc}",
        ) from None
    if resp.status_code >= 400:
        # Truncate upstream body — it may carry container paths, env hints,
        # or other internals we don't want to forward verbatim. Also scrub
        # the literal sandbox URL so it doesn't leak through the proxy
        # surface alongside the rest of the error vocabulary.
        from aitelier.providers.sandbox_agent import _scrub_sandbox_url
        text = _scrub_sandbox_url(resp.text[:200], cfg.base_url)
        detail = f"sandbox-agent {resp.status_code}: {text}"
        raise HTTPException(status_code=resp.status_code, detail=detail)
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        return resp.json()
    return resp.content


async def run_prepare(prep: dict | None) -> dict:
    """Run pre-flight setup. Returns a dict with per-phase results.

    Failure semantics:
      - install_agents: collect all outcomes; non-fatal (agent may already
        be installed — SA returns ok regardless).
      - commands:       sequential. First non-zero exit aborts; `error`
        is set on the result and the caller skips the agent run.
      - files:          sequential. First failure aborts.
      - sidecars:       all started; PIDs returned. Cleanup is the caller's
        responsibility (always tear down via stop_sidecars).
    """
    out: dict = {
        "install_results": [],
        "command_results": [],
        "file_results": [],
        "sidecars": [],
        "error": None,
    }
    if not prep:
        return out

    for agent in prep.get("install_agents") or []:
        try:
            validate_path_component(agent, "agent")
            r = await sa_proxy("POST", f"/v1/agents/{agent}/install",
                                json_body={}, timeout=300)
            out["install_results"].append({"agent": agent, "ok": True, "result": r})
        except Exception as exc:
            out["install_results"].append({
                "agent": agent, "ok": False, "error": str(exc),
            })

    for cmd in prep.get("commands") or []:
        try:
            r = await sa_proxy("POST", "/v1/processes/run",
                                json_body=cmd, timeout=120)
            exit_code = r.get("exit_code", 0) if isinstance(r, dict) else 0
            out["command_results"].append({
                "cmd": cmd, "exit_code": exit_code, "stdout": r.get("stdout"),
                "stderr": r.get("stderr"),
            })
            if exit_code != 0:
                out["error"] = f"command failed (exit {exit_code}): {cmd}"
                return out
        except Exception as exc:
            out["command_results"].append({"cmd": cmd, "error": str(exc)})
            out["error"] = f"command raised: {exc}"
            return out

    for f in prep.get("files") or []:
        try:
            await sa_proxy("PUT", "/v1/fs/file", json_body=f, timeout=30)
            out["file_results"].append({"path": f.get("path"), "ok": True})
        except Exception as exc:
            out["file_results"].append({
                "path": f.get("path"), "ok": False, "error": str(exc),
            })
            out["error"] = f"file write failed: {f.get('path')} ({exc})"
            return out

    for s in prep.get("sidecars") or []:
        try:
            r = await sa_proxy("POST", "/v1/processes", json_body=s, timeout=30)
            pid = r.get("id") or r.get("pid") if isinstance(r, dict) else None
            out["sidecars"].append({
                "name": s.get("name"), "id": pid, "state": "running",
            })
        except Exception as exc:
            out["sidecars"].append({
                "name": s.get("name"), "state": "failed", "error": str(exc),
            })

    return out


async def stop_sidecars(sidecars: list[dict]) -> None:
    """Best-effort sidecar shutdown. Called from a finally block."""
    for sc in sidecars:
        sid = sc.get("id")
        if not sid:
            continue
        try:
            await sa_proxy("POST", f"/v1/processes/{sid}/stop",
                            json_body={}, timeout=10)
            sc["state"] = "stopped"
        except Exception as exc:
            sc["state"] = f"stop_failed: {exc}"


async def fetch_artifacts(spec: dict | None) -> dict:
    """Pull files back from the sandbox after the agent run.

    Best-effort: missing files become None entries with an `error` key.
    """
    if not spec:
        return {}
    out: dict = {}
    for path in spec.get("fetch") or []:
        try:
            r = await sa_proxy("GET", "/v1/fs/file",
                                params={"path": path}, timeout=30)
            if isinstance(r, dict):
                out[path] = r.get("content")
            else:
                out[path] = r
        except Exception as exc:
            out[path] = {"error": str(exc)}
    return out


def prepare_failed_result(run_id: str, prepare_result: dict, cid: str) -> dict:
    """Build a Result-shaped dict when prepare aborted before the agent ran."""
    return {
        "kind": "agent",
        "provider": "",
        "status": "error",
        "duration_s": 0.0,
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "prepare_failed",
        "tool_calls": [],
        "cost_usd": None,
        "error_type": "PrepareFailed",
        "error_msg": prepare_result.get("error") or "prepare phase failed",
        "correlation_id": cid,
        "prepare": prepare_result,
    }
