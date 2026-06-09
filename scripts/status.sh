#!/usr/bin/env bash
# What's running, where logs are, are dependencies healthy.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX_AGENT_PID_FILE="$REPO_ROOT/runs/.sandbox-agent.pid"
SANDBOX_AGENT_LOG="$REPO_ROOT/runs/.sandbox-agent.log"
AITELIER_PID_FILE="$REPO_ROOT/runs/.aitelier.pid"
AITELIER_LOG="$REPO_ROOT/runs/logs/aitelier.log"

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
_check_http "http://localhost:2468/v1/agents"        "Sandbox Agent"

# Postgres lives behind docker; check via the container, not the wire.
if docker compose -f "$REPO_ROOT/docker/docker-compose.yml" exec -T postgres \
        pg_isready -U aitelier -d aitelier >/dev/null 2>&1; then
    _status_line "Postgres" "up" "container ready"
else
    _status_line "Postgres" "down" "container not responding"
fi

echo ""
echo "=== Processes ==="
for entry in "aitelier:$AITELIER_PID_FILE" "sandbox-agent:$SANDBOX_AGENT_PID_FILE"; do
    label="${entry%%:*}"
    pid_file="${entry#*:}"
    if [ -f "$pid_file" ]; then
        pid="$(cat "$pid_file")"
        if kill -0 "$pid" 2>/dev/null; then
            _status_line "$label" "up" "PID $pid"
        else
            _status_line "$label" "down" "stale PID file ($pid)"
        fi
    else
        _status_line "$label" "down" "no PID file"
    fi
done

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
