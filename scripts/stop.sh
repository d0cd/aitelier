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
MODE="${1:-full}"

_kill_pid_file() {
    # $1 = pid file path, $2 = label, $3 = fallback pkill pattern.
    local pid_file="$1" label="$2" fallback_pattern="$3"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" && echo "  ✓ $label stopped (PID $pid)"
        else
            echo "  - $label PID file stale (process not running)"
        fi
        rm -f "$pid_file"
    elif pkill -f "$fallback_pattern" 2>/dev/null; then
        echo "  ✓ $label stopped (via pkill — no PID file)"
    else
        echo "  - $label not running"
    fi
}

if [ "$MODE" = "full" ] || [ "$MODE" = "service" ]; then
    echo "=== Stopping aitelier service ==="
    _kill_pid_file "$AITELIER_PID_FILE" "aitelier service" "aitelier serve"
fi

if [ "$MODE" = "full" ] || [ "$MODE" = "infra" ]; then
    echo "=== Stopping Sandbox Agent ==="
    _kill_pid_file "$SANDBOX_AGENT_PID_FILE" "sandbox-agent" "sandbox-agent server"

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
fi

echo "Done."
