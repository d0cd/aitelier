#!/usr/bin/env bash
# Preflight diagnostics for `make start`.
# Run this when start fails with a confusing error.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
issues=0

# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

_ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
_warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
_fail() { printf "  \033[31m✗\033[0m %s\n" "$*"; issues=$((issues + 1)); }

# Sandbox Agent mode drives which preflight checks apply (brig vs host).
cd "$REPO_ROOT" || exit 1
SA_MODE="$(uv run python -c '
import sys, tomllib
from pathlib import Path
for p in [Path("aitelier.toml"), Path.home()/".config"/"aitelier"/"config.toml"]:
    if p.exists():
        try:
            print(tomllib.loads(p.read_text()).get("sandbox_agent", {}).get("mode", "host"))
            sys.exit(0)
        except Exception:
            pass
print("host")
' 2>/dev/null || echo "host")"

_compose_service_for_port() {
    # If the port is forwarded from one of our docker compose services,
    # print "<service>:<container_name>"; otherwise print empty.
    local port="$1"
    docker ps --filter "publish=$port" --format '{{.Names}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}' 2>/dev/null \
        | awk -v p="$port" '$2 == "docker" { printf "%s:%s\n", $3, $1 }'
}

_aitelier_alive_on() {
    # Probe /v1/health on the given port. The HTTP response is authoritative —
    # `status` uses it, and doctor should agree. Returns 0 when /v1/health
    # answers 200, non-zero otherwise.
    local port="$1"
    curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:${port}/v1/health"
}


_check_port() {
    # $1 = port, $2 = what it's for, $3 = fix hint, $4 = optional pid file
    # for a host-process service we manage (e.g. runs/.aitelier.pid).
    local port="$1" what="$2" hint="$3" pidfile="${4:-}" holder ours pid
    if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        _ok "port $port ($what) free"
        return
    fi
    # If we have a pid file and the recorded process is alive, this port
    # is held by our own running service — not a conflict.
    if [ -n "$pidfile" ] && [ -f "$pidfile" ]; then
        pid="$(tr -d '[:space:]' < "$pidfile" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            _ok "port $port ($what) — bound by our running service (PID $pid)"
            return
        fi
    fi
    # No pidfile match — for the aitelier service port, fall back to the same
    # /v1/health probe `status` uses. Avoids the "doctor says held by python3.14
    # / status says not running" disagreement when aitelier was started outside
    # `make start` (e.g. `uv run aitelier serve` directly).
    if [ "$port" = "7777" ] && _aitelier_alive_on "$port"; then
        _ok "port $port ($what) — bound by aitelier (/v1/health answers ok)"
        return
    fi
    holder="$(_port_holder "$port")"
    ours="$(_compose_service_for_port "$port")"
    if [ -n "$ours" ]; then
        _ok "port $port ($what) — bound by our container ${ours#*:}"
    else
        # Could be a *different* docker container, or a host process.
        local other_container
        other_container="$(docker ps --filter "publish=$port" --format '{{.Names}}' 2>/dev/null | head -1)"
        if [ -n "$other_container" ]; then
            _fail "port $port ($what) — held by docker container \"$other_container\" (NOT ours)"
            printf "      → docker stop %s   (or change our host port in docker/docker-compose.yml)\n" "$other_container"
        else
            _fail "port $port ($what) — held by ${holder:-unknown}"
            printf "      → %s\n" "$hint"
        fi
    fi
}

echo "=== Ports ==="
_check_port 5433 "Postgres"           "docker stop <other-postgres>  or edit docker/docker-compose.yml host port"
_check_port 4000 "LiteLLM proxy"      "stop the conflicting process or override LITELLM_BASE_URL"
_check_port 2468 "Sandbox Agent"      "start.sh will pick a free port automatically; this is informational" \
                                      "$REPO_ROOT/runs/.sandbox-agent.pid"
_check_port 7777 "aitelier service"   "stop the conflicting process or set AITELIER_PORT" \
                                      "$REPO_ROOT/runs/.aitelier.pid"

echo ""
echo "=== Tools ==="
for cmd in uv docker curl lsof; do
    if command -v "$cmd" >/dev/null 2>&1; then
        _ok "$cmd: $(command -v "$cmd")"
    else
        _fail "$cmd not on PATH"
    fi
done

# sandbox-agent is installed by start.sh — note its absence, don't fail.
if command -v sandbox-agent >/dev/null 2>&1; then
    _ok "sandbox-agent: $(command -v sandbox-agent)"
else
    _warn "sandbox-agent not installed (start.sh will fetch it on first run)"
fi

# Detect a `uv tool install aitelier` that could drift from the workspace.
# An editable install pointing at *this* repo's core/ is fine — that's the
# recommended way to expose `aitelier` globally while you're hacking on it.
# A non-editable install (or an editable install pointing at a different
# checkout) will shadow this repo's code on PATH and drift silently.
_uv_tool_aitelier_state() {
    # Prints: "absent" | "editable-here" | "editable-elsewhere:<path>" | "non-editable"
    if ! uv tool list 2>/dev/null | grep -qE '^aitelier'; then
        echo "absent"; return
    fi
    local meta want_url
    meta="$(ls "$HOME/.local/share/uv/tools/aitelier/lib/"python*/site-packages/aitelier-*.dist-info/direct_url.json 2>/dev/null | head -1)"
    [ -z "$meta" ] && { echo "non-editable"; return; }
    want_url="file://$REPO_ROOT/core"
    if grep -q '"editable":[[:space:]]*true' "$meta" 2>/dev/null; then
        if grep -qF "\"url\":\"$want_url\"" "$meta" 2>/dev/null; then
            echo "editable-here"
        else
            local actual
            actual="$(grep -oE '"url":"[^"]+"' "$meta" 2>/dev/null | head -1 | sed 's/"url":"//;s/"$//')"
            echo "editable-elsewhere:${actual:-unknown}"
        fi
    else
        echo "non-editable"
    fi
}
case "$(_uv_tool_aitelier_state)" in
    absent)
        # No global CLI. Fine, but suggest the principled setup once.
        _warn "no global 'aitelier' CLI — run 'uv tool install --editable ./core' from repo root for a global shim that tracks this workspace"
        ;;
    editable-here)
        _ok "uv tool 'aitelier' is editable and points at this workspace"
        ;;
    editable-elsewhere:*)
        _fail "uv tool 'aitelier' is editable but points at $(_uv_tool_aitelier_state | cut -d: -f2-) — not this repo"
        printf "      → 'uv tool uninstall aitelier' then 'uv tool install --editable ./core' from %s\n" "$REPO_ROOT"
        ;;
    non-editable)
        _fail "uv tool 'aitelier' is a non-editable install — will drift from this workspace"
        printf "      → 'uv tool uninstall aitelier' then 'uv tool install --editable ./core' from %s\n" "$REPO_ROOT"
        ;;
esac

echo ""
echo "=== Docker ==="
if docker info >/dev/null 2>&1; then
    _ok "Docker daemon reachable"
else
    _fail "Docker daemon unreachable — start Docker Desktop / OrbStack"
fi

echo ""
echo "=== Credentials ==="
# Advisory only: the control plane and the local/ollama LLM paths need no
# credential, so a missing or expired login never blocks startup — it just
# disables the matching model families. (agent:claude manages its own
# credential via the Sandbox Agent and is unaffected by the LiteLLM key here.)
_claude_cred_state() {
    # Prints: missing | no-token | expired | valid:<hours>
    local f="$HOME/.claude/.credentials.json"
    [ -f "$f" ] || { echo "missing"; return; }
    python3 - "$f" <<'PY'
import json, sys, time
try:
    o = json.load(open(sys.argv[1])).get("claudeAiOauth", {})
    token, expires = o.get("accessToken", ""), o.get("expiresAt", 0)
    if not token:
        print("no-token")
    elif expires and expires < time.time() * 1000:
        print("expired")
    else:
        print(f"valid:{(expires - time.time() * 1000) / 3_600_000:.0f}" if expires else "valid:?")
except Exception:
    print("no-token")
PY
}
claude_state="$(_claude_cred_state)"
case "$claude_state" in
    valid:*) _ok "Claude credentials valid (${claude_state#valid:}h remaining)" ;;
    expired) _warn "Claude OAuth token expired — \`claude login\` to refresh (claude-*/anthropic/* LLM models unavailable until then)" ;;
    no-token) _warn "Claude credentials file present but no token — \`claude login\` (claude-*/anthropic/* unavailable)" ;;
    *) _warn "Claude not logged in — \`claude login\` for claude-*/anthropic/* LLM models (optional)" ;;
esac

if [ -f "$HOME/.codex/auth.json" ]; then
    _ok "Codex credentials present (~/.codex/auth.json)"
else
    _warn "Codex not logged in (non-critical) — run \`codex login\` if you want OpenAI access"
fi

if [ "$SA_MODE" = "brig" ]; then
    echo ""
    echo "=== Brig (Sandbox Agent in a cell) ==="
    if command -v brig >/dev/null 2>&1; then
        _ok "brig: $(command -v brig)"
        if brig system doctor --quick >/dev/null 2>&1; then
            _ok "brig VM up (warden reachable)"
        else
            _warn "brig VM down — start.sh runs 'brig system up' automatically (first start waits for boot)"
        fi
    else
        _fail "[sandbox_agent] mode = \"brig\" but brig not on PATH"
        printf "      → uv tool install -e ~/projects/brig   (or switch mode in aitelier.toml)\n"
    fi
    brig_token="$HOME/.brig/secrets/sandbox-agent-ingress-token"
    if [ -f "$brig_token" ]; then
        _ok "brig ingress token registered"
    else
        _fail "brig ingress token missing ($brig_token)"
        printf "      → python3 -c 'import secrets;print(secrets.token_urlsafe(32))' | brig secrets add sandbox-agent-ingress-token\n"
    fi
    if [ -x "$REPO_ROOT/docker/prebaked-agents/claude/claude" ]; then
        _ok "prebaked claude binary present"
    else
        _fail "prebaked claude binary missing (docker/prebaked-agents/claude/claude)"
        printf "      → see docker/sandbox-agent.brig.Dockerfile for the one-time fetch command\n"
    fi
fi

echo ""
echo "=== Disk ==="
if [ -d "$REPO_ROOT/runs" ]; then
    runs_size="$(du -sh "$REPO_ROOT/runs" 2>/dev/null | awk '{print $1}')"
    _ok  "runs/ exists — $runs_size used"
else
    _ok  "runs/ does not yet exist (will be created on first run)"
fi

available="$(df -h "$REPO_ROOT" | awk 'NR==2 {print $4}')"
_ok "disk free on repo volume: $available"

echo ""
if [ "$issues" -eq 0 ]; then
    echo "All clear. \`make start\` should work."
    exit 0
else
    echo "$issues issue(s) found. Fix the ✗ items above, then re-run \`make doctor\`."
    exit 1
fi
