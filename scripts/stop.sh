#!/usr/bin/env bash
# Stop aitelier — kill service, stop infra.
#
# Usage:
#   ./scripts/stop.sh              # stop everything
#   ./scripts/stop.sh service      # stop aitelier service only
#   ./scripts/stop.sh infra        # stop docker infra only

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX_AGENT_PID_FILE="$REPO_ROOT/runs/.sandbox-agent.pid"
MODE="${1:-full}"

if [ "$MODE" = "full" ] || [ "$MODE" = "service" ]; then
    echo "=== Stopping aitelier service ==="
    # Find and kill any aitelier serve processes
    pkill -f "aitelier serve" 2>/dev/null && echo "  ✓ Stopped" || echo "  - Not running"
fi

if [ "$MODE" = "full" ] || [ "$MODE" = "infra" ]; then
    echo "=== Stopping Sandbox Agent ==="
    if [ -f "$SANDBOX_AGENT_PID_FILE" ]; then
        PID=$(cat "$SANDBOX_AGENT_PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" && echo "  ✓ Stopped (PID $PID)"
        else
            echo "  - PID file stale (process not running)"
        fi
        rm -f "$SANDBOX_AGENT_PID_FILE"
    else
        # Fallback if PID file is missing
        pkill -f "sandbox-agent server" 2>/dev/null && echo "  ✓ Stopped (via pkill)" || echo "  - Not running"
    fi

    echo "=== Stopping infrastructure ==="
    cd "$REPO_ROOT/docker"
    docker compose down 2>/dev/null && echo "  ✓ Stopped" || echo "  - Not running"
fi

echo "Done."
