#!/usr/bin/env bash
# Stop aitelier — kill service, stop infra.
#
# Usage:
#   ./scripts/stop.sh              # stop everything (default)
#   ./scripts/stop.sh service      # stop aitelier service only (keep infra hot)
#   ./scripts/stop.sh infra        # stop Sandbox Agent + docker containers only
#
# Postgres data volume is NEVER dropped here — use `make reset` for that.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX_AGENT_PID_FILE="$REPO_ROOT/runs/.sandbox-agent.pid"
AITELIER_PID_FILE="$REPO_ROOT/runs/.aitelier.pid"
SESSION_TOML="$REPO_ROOT/runs/.session.toml"
CLAUDE_SETUP_TOKEN_FILE="$REPO_ROOT/runs/.claude-oauth-token"
CLAUDE_SETUP_TOKEN_FINGERPRINT_FILE="$REPO_ROOT/runs/.claude-oauth-token.sha256"
SANDBOX_AGENT_TOKEN_FINGERPRINT_FILE="$REPO_ROOT/runs/.sandbox-agent-claude-token.sha256"
MODE="${1:-full}"

# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

# Sandbox Agent mode — decides how to stop SA (local PID vs brig cell).
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

_kill_pid_file() {
    # $1 = pid file path, $2 = label, $3 = literal command fragment.
    # Never fall back to pkill: a broad process-name match can terminate a
    # service owned by another checkout or by the user directly.
    local pid_file="$1" label="$2" expected_command="$3"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file")"
        if _process_matches "$pid" "$expected_command"; then
            kill "$pid" 2>/dev/null || true
            # Confirm exit before removing the PID file — otherwise a process
            # that ignores SIGTERM keeps running while we drop its PID file,
            # orphaning it. Wait ~5s, then escalate to SIGKILL.
            local waited=0
            while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 50 ]; do
                sleep 0.1
                waited=$((waited + 1))
            done
            if _process_matches "$pid" "$expected_command"; then
                kill -9 "$pid" 2>/dev/null || true
                echo "  ✓ $label force-killed (PID $pid ignored SIGTERM)"
            elif kill -0 "$pid" 2>/dev/null; then
                echo "  ! $label PID $pid changed identity; refusing to force-kill it"
            else
                echo "  ✓ $label stopped (PID $pid)"
            fi
        elif kill -0 "$pid" 2>/dev/null; then
            echo "  ! $label PID file points at a different process; refusing to stop PID $pid"
        else
            echo "  - $label PID file stale (process not running)"
        fi
        rm -f "$pid_file"
    else
        echo "  - $label not managed by this checkout (no PID file)"
    fi
}

if [ "$MODE" = "full" ] || [ "$MODE" = "service" ]; then
    echo "=== Stopping aitelier service ==="
    # If a supervisor (e.g. process-compose) manages the service, it respawns
    # the moment we kill it — so a plain stop looks like it didn't work. Warn.
    if pgrep -f "process-compose up -f" >/dev/null 2>&1; then
        echo "  ! process-compose is running — if it supervises aitelier it will"
        echo "    respawn the service after this kill. To stop it: pc stop aitelier"
    fi
    _kill_pid_file "$AITELIER_PID_FILE" "aitelier service" "aitelier serve"
fi

if [ "$MODE" = "full" ] || [ "$MODE" = "infra" ]; then
    echo "=== Stopping Sandbox Agent ==="
    if [ "$SA_MODE" = "brig" ]; then
        _brig_cell_down && echo "  ✓ brig SA cell stopped + removed"
    else
        _kill_pid_file "$SANDBOX_AGENT_PID_FILE" "sandbox-agent" "sandbox-agent server"
    fi

    echo "=== Stopping infrastructure ==="
    cd "$REPO_ROOT/docker"
    docker compose down 2>/dev/null && echo "  ✓ Stopped" || echo "  - Not running"
fi

# Remove the runtime config overlay only when the infra it describes is
# being torn down (SA port, Postgres DSN). `stop.sh service` leaves SA
# running, so its session file is still authoritative — preserve it.
if [ "$MODE" = "full" ] || [ "$MODE" = "infra" ]; then
    if [ -f "$SESSION_TOML" ]; then
        rm -f "$SESSION_TOML"
        echo "  ✓ removed runs/.session.toml"
    fi
    rm -f "$CLAUDE_SETUP_TOKEN_FILE" \
          "$CLAUDE_SETUP_TOKEN_FINGERPRINT_FILE" \
          "$SANDBOX_AGENT_TOKEN_FINGERPRINT_FILE"
fi

echo "Done."
