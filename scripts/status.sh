#!/usr/bin/env bash
# What's running, where logs are, are dependencies healthy.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX_AGENT_PID_FILE="$REPO_ROOT/runs/.sandbox-agent.pid"
SANDBOX_AGENT_LOG="$REPO_ROOT/runs/.sandbox-agent.log"
AITELIER_PID_FILE="$REPO_ROOT/runs/.aitelier.pid"
AITELIER_LOG="$REPO_ROOT/runs/logs/aitelier.log"

# Resolve the Sandbox Agent endpoint the way aitelier does — layer
# aitelier.toml < aitelier.secrets.toml < runs/.session.toml. In brig/docker/
# remote mode SA runs off-host: there's no local :2468 and no host PID file,
# so the old hardcoded localhost:2468 + PID-file checks reported a false
# "down". Read the real mode + base_url + token instead.
_sa_cfg() {
    python3 - "$REPO_ROOT" <<'PY' 2>/dev/null
import sys, tomllib, pathlib
root = pathlib.Path(sys.argv[1])
sa = {}
for name in ("aitelier.toml", "aitelier.secrets.toml", "runs/.session.toml"):
    p = root / name
    try:
        sa.update(tomllib.loads(p.read_text()).get("sandbox_agent", {}))
    except Exception:
        pass
print(sa.get("mode", "host"))
print(sa.get("base_url", "http://localhost:2468"))
print(sa.get("token", ""))
PY
}
{ read -r SA_MODE; read -r SA_BASE_URL; read -r SA_TOKEN; } < <(_sa_cfg)
SA_MODE="${SA_MODE:-host}"
SA_BASE_URL="${SA_BASE_URL:-http://localhost:2468}"

_status_line() {
    # $1 = label, $2 = "up"|"down"|"unknown", $3 = detail
    local mark
    case "$2" in
        up)      mark="✓" ;;
        down)    mark="✗" ;;
        *)       mark="?" ;;
    esac
    printf "  %s %-18s %s\n" "$mark" "$1" "$3"
}

_check_http() {
    # $1 = url, $2 = label, $3 = optional Authorization value
    local headers=()
    if [ -n "${3:-}" ]; then
        headers=(-H "Authorization: $3")
    fi
    if curl -sf "${headers[@]}" "$1" >/dev/null 2>&1; then
        _status_line "$2" "up" "$1"
    else
        _status_line "$2" "down" "$1 (unreachable)"
    fi
}

echo "=== Services ==="
_check_http "http://localhost:4000/health"           "LiteLLM proxy"     "Bearer sk-litellm-local"
_check_http "http://localhost:7777/v1/health"        "aitelier service"
_sa_auth=""
[ -n "$SA_TOKEN" ] && _sa_auth="Bearer $SA_TOKEN"
# `curl -sf` fails on non-2xx, so a 401 (bad/missing token) or 502 (cell
# ingress up but the SA process inside is dead) both correctly read "down".
_check_http "${SA_BASE_URL%/}/v1/agents"             "Sandbox Agent"    "$_sa_auth"

# Postgres lives behind docker; check via the container, not the wire.
if docker compose -f "$REPO_ROOT/docker/docker-compose.yml" exec -T postgres \
        pg_isready -U aitelier -d aitelier >/dev/null 2>&1; then
    _status_line "Postgres" "up" "container ready"
else
    _status_line "Postgres" "down" "container not responding"
fi

echo ""
echo "=== Agent logins ==="
# Per-backend credential health. aitelier probes each agent backend with a
# real ACP session/new handshake (surfaced in /v1/models) — that handshake
# touches the backend's auth, so when a login goes stale the probe fails and
# the backend's advertised-option arrays vanish. Reflects staleness within
# ~10min (probe success TTL); failing backends re-check every ~1min. This is
# the signal to watch: e.g. codex's ChatGPT-OAuth token rotates and can go
# stale between logins — when it does, its line flips to "login stale".
_agent_logins() {
    # /v1/models is fast on a warm probe cache (~10min TTL) but can be slow on
    # a cold cache (it re-probes every backend). Bounded so `make status` can't
    # hang; a timeout just yields the "unavailable" line below.
    local json
    json="$(curl -sf -m 15 "http://localhost:7777/v1/models" 2>/dev/null)" || return 0
    [ -n "$json" ] || return 0
    MODELS_JSON="$json" python3 <<'PY' 2>/dev/null
import os, json
try:
    data = json.loads(os.environ.get("MODELS_JSON", "")).get("data", [])
except Exception:
    raise SystemExit(0)
for m in sorted((x for x in data if str(x.get("id", "")).startswith("agent:")),
                key=lambda x: x["id"]):
    ok = bool(m.get("aitelier_inner_llms") and m.get("aitelier_approval_modes"))
    print(f"{m['id'].split(':', 1)[1]}\t{'ok' if ok else 'stale'}")
PY
}
_logins="$(_agent_logins)"
if [ -z "$_logins" ]; then
    _status_line "agent logins" "unknown" "unavailable (aitelier down or probe timed out)"
else
    while IFS="$(printf '\t')" read -r backend state; do
        [ -z "$backend" ] && continue
        if [ "$state" = "ok" ]; then
            _status_line "$backend" "up" "login ok"
        else
            _status_line "$backend" "down" "login stale/unconfigured — re-login"
        fi
    done <<EOF
$_logins
EOF
fi

echo ""
echo "=== Processes ==="
_check_pid() {
    # $1 = label, $2 = pid file
    if [ -f "$2" ]; then
        local pid; pid="$(cat "$2")"
        if kill -0 "$pid" 2>/dev/null; then
            _status_line "$1" "up" "PID $pid"
        else
            _status_line "$1" "down" "stale PID file ($pid)"
        fi
    else
        _status_line "$1" "down" "no PID file"
    fi
}
_check_pid "aitelier" "$AITELIER_PID_FILE"
# SA is only a host process in "host" mode; in brig/docker/remote it runs
# off-host with no local PID file — its liveness is the Services check above,
# so a PID-file check here would be a false negative.
if [ "$SA_MODE" = "host" ]; then
    _check_pid "sandbox-agent" "$SANDBOX_AGENT_PID_FILE"
else
    _status_line "sandbox-agent" "unknown" "$SA_MODE mode (off-host) — see Services"
fi

echo ""
echo "=== Logs ==="
for log in "$AITELIER_LOG" "$SANDBOX_AGENT_LOG"; do
    if [ -f "$log" ]; then
        size="$(wc -c < "$log" | tr -d ' ')"
        printf "  %s  (%s bytes)\n" "$log" "$size"
    else
        printf "  %s  (not yet created)\n" "$log"
    fi
done

echo ""
echo "Tip: \`make logs\` to tail them live, \`make doctor\` for preflight checks."
