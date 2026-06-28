"""Live dependency probes for /v1/discovery and /v1/health.

Reach out to LiteLLM, the durable store, and the Sandbox Agent and report
reachability. Extracted from server.py; server.py and endpoints/inference.py
import from here.
"""

from __future__ import annotations

from aitelier.storage import RunFilter, get_store


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


async def _probe_traces() -> dict:
    """Live probe: durable store queryable."""
    try:
        store = await get_store()
        await store.list_runs(RunFilter(limit=1))
        return {"available": True}
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


async def _sandbox_agents_request(cfg):
    """GET Sandbox Agent's /v1/agents. Returns the httpx response so callers
    apply their own status/error handling. Raises on transport failure."""
    from aitelier.providers.llm import get_shared_client
    headers = {}
    if cfg.sandbox_agent.token:
        headers["Authorization"] = f"Bearer {cfg.sandbox_agent.token}"
    client = await get_shared_client()
    return await client.get(
        f"{cfg.sandbox_agent.base_url}/v1/agents",
        headers=headers,
        timeout=3,
    )


def _normalize_agents_payload(data) -> list:
    """/v1/agents returns either a list or {"agents": [...]} — accept both."""
    return data if isinstance(data, list) else data.get("agents") or []


async def _probe_sandbox_agent(cfg) -> dict:
    """Live probe: Sandbox Agent reachability + available agent backends.

    Hits GET /v1/agents on the sandbox-agent server (Rivet). Returns the list
    of agent IDs the sandbox advertises (claude-code, codex, opencode, ...).
    """
    try:
        resp = await _sandbox_agents_request(cfg)
        if resp.status_code == 200:
            raw = _normalize_agents_payload(resp.json())
            # `mock` is filtered: the SA mock backend doesn't return a
            # sessionId on session/new, so any consumer who picks it up
            # as a test target gets a confusing handshake error. The
            # backend is still reachable via direct SA URL for SA-level
            # tests; aitelier just doesn't advertise it.
            agents = sorted(
                a["id"] if isinstance(a, dict) else a
                for a in raw
                if ((isinstance(a, dict) and a.get("id") and a["id"] != "mock")
                    or (isinstance(a, str) and a != "mock"))
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
