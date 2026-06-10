"""Live tests for /v1/health and /v1/discovery.

Cheap shape-and-contract checks — `/v1/health` is what k8s liveness probes
hit, and `/v1/discovery` is the canonical capability surface other services
read at boot. Both should be schema-stable.
"""

from __future__ import annotations


def test_health_returns_ok_and_known_limitations(http):
    r = http.get("/v1/health")
    r.raise_for_status()
    body = r.json()
    # Phase K: status is "ok" when no deps have been probed (cold cache)
    # or all probed deps are reachable; "degraded" when a tracked dep
    # was unreachable on the last `/v1/discovery` probe. Both mean
    # aitelier itself is alive — which is the entire promise of /v1/health.
    assert body["status"] in ("ok", "degraded"), body
    assert body["version"]
    assert body["timestamp"]
    assert isinstance(body["known_limitations"], list)


def test_health_status_reflects_discovery_dep_state(http):
    """When `/v1/discovery` has populated the dep cache, the next
    `/v1/health` MUST reflect whether any dep is unreachable. This is the
    contract Phase K added — without it, k8s readiness probes can't
    distinguish 'aitelier itself is up' from 'aitelier is up and ready
    to serve.'"""
    # Force a fresh discovery probe so the cache is populated.
    d = http.get("/v1/discovery").json()
    any_down = any(
        not (info.get("reachable") if isinstance(info, dict) else True)
        for info in (d.get("dependencies") or {}).values()
    )
    h = http.get("/v1/health").json()
    # When all deps are reachable, status must be "ok". When any is
    # unreachable, status must be "degraded". The deps summary must be
    # present (cache is warm by definition after the discovery hit).
    expected = "degraded" if any_down else "ok"
    assert h["status"] == expected, (
        f"discovery says any_down={any_down} but /v1/health says "
        f"status={h['status']!r}. body={h}"
    )
    assert "dependencies" in h, h


def test_discovery_advertises_endpoints_and_dependencies(http):
    r = http.get("/v1/discovery")
    r.raise_for_status()
    body = r.json()
    # Top-level shape contract.
    assert body["service"] == "aitelier"
    assert body["api_version"] == "v1"
    assert isinstance(body["endpoints"], list)
    assert isinstance(body["capabilities"], dict)
    # Dependencies block carries reachability + base_urls so consumers can
    # diagnose unreachable upstreams without rolling their own probe.
    deps = body["dependencies"]
    assert "litellm" in deps
    assert "sandbox_agent" in deps
    for d in deps.values():
        assert "reachable" in d
        assert "base_url" in d


def test_discovery_lists_each_endpoint_with_method(http):
    """Endpoints should be a list of {method, path} entries — consumers
    iterate to render API browsers / generate clients."""
    endpoints = http.get("/v1/discovery").json()["endpoints"]
    pairs = {(e["method"], e["path"]) for e in endpoints}
    # Spot-check the routes that anchor the new contract.
    assert ("GET", "/v1/health") in pairs
    assert ("POST", "/v1/chat/completions") in pairs
    assert ("POST", "/v1/embeddings") in pairs
    assert ("GET", "/v1/models") in pairs
    assert ("POST", "/v1/runs") in pairs
    assert ("POST", "/v1/runs/{run_id}/cancel") in pairs
    for e in endpoints:
        assert e["method"] in ("GET", "POST", "DELETE", "PUT", "PATCH")
