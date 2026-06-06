#!/usr/bin/env bash
# Start aitelier — extract credentials from CLI logins, boot infra, launch service.
#
# Usage:
#   ./scripts/start.sh                              # full stack
#   ./scripts/start.sh infra                        # infra only (LiteLLM + Sandbox Agent)
#   ./scripts/start.sh service                      # aitelier service only
#   ./scripts/start.sh --sandbox-agent-port 3000    # override Sandbox Agent port
#
# Sandbox Agent port resolution:
#   --sandbox-agent-port <N>  >  $SANDBOX_AGENT_PORT  >  2468 (or dynamic if taken)
# The chosen URL is exported as SANDBOX_AGENT_BASE_URL so the aitelier service picks it up.
#
# Credentials are extracted from Claude Code and Codex CLI credential files.
# No manual API keys needed — just run `claude login` and `codex login` first.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/docker/.env"
SANDBOX_AGENT_PID_FILE="$REPO_ROOT/runs/.sandbox-agent.pid"
SANDBOX_AGENT_LOG="$REPO_ROOT/runs/.sandbox-agent.log"

# Sandbox Agent port resolution (in order):
#   1. --sandbox-agent-port <N> CLI flag
#   2. SANDBOX_AGENT_PORT env var
#   3. 2468 default; if taken, pick a free port dynamically
SANDBOX_AGENT_PORT_REQUESTED=""

# Parse named flags (other positional args still work)
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --sandbox-agent-port)
            SANDBOX_AGENT_PORT_REQUESTED="$2"
            shift 2
            ;;
        --sandbox-agent-port=*)
            SANDBOX_AGENT_PORT_REQUESTED="${1#*=}"
            shift
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

_port_in_use() {
    # Returns 0 (true) if a process is listening on the given port.
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        nc -z 127.0.0.1 "$1" >/dev/null 2>&1
    fi
}

_pick_free_port() {
    # Ask the kernel for an ephemeral free port.
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

if [ -n "$SANDBOX_AGENT_PORT_REQUESTED" ]; then
    SANDBOX_AGENT_PORT="$SANDBOX_AGENT_PORT_REQUESTED"
elif [ -n "${SANDBOX_AGENT_PORT:-}" ]; then
    :  # user already set it via env
elif _port_in_use 2468; then
    SANDBOX_AGENT_PORT="$(_pick_free_port)"
    echo "  (port 2468 in use; picked free port $SANDBOX_AGENT_PORT)"
else
    SANDBOX_AGENT_PORT=2468
fi

# Export so the aitelier service (started later) reads the right URL.
export SANDBOX_AGENT_BASE_URL="http://127.0.0.1:${SANDBOX_AGENT_PORT}"

# ---------------------------------------------------------------------------
# 1. Extract credentials from CLI credential files
# ---------------------------------------------------------------------------

echo "=== Extracting credentials ==="

# Safety: ensure docker/.env is gitignored
if ! grep -q "docker/.env" "$REPO_ROOT/.gitignore" 2>/dev/null; then
    echo "docker/.env" >> "$REPO_ROOT/.gitignore"
    echo "  Added docker/.env to .gitignore"
fi

# Extract and validate credentials via Python (handles JSON + expiry check)
python3 - "$ENV_FILE" <<'PYEOF'
import json, sys, time
from pathlib import Path

env_file = sys.argv[1]
lines = []
ok = True

# --- Claude Code ---
claude_creds = Path.home() / ".claude" / ".credentials.json"
if claude_creds.exists():
    try:
        data = json.loads(claude_creds.read_text())
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken", "")
        expires = oauth.get("expiresAt", 0)

        if not token:
            print("  ✗ Claude: credentials file exists but no token found")
            ok = False
        elif expires and expires < time.time() * 1000:  # expiresAt is in ms
            print("  ✗ Claude: OAuth token expired — run 'claude login' to refresh")
            ok = False
        else:
            lines.append(f"ANTHROPIC_API_KEY={token}")
            remaining_h = (expires - time.time() * 1000) / 3_600_000 if expires else 0
            print(f"  ✓ Claude: token valid ({remaining_h:.0f}h remaining)")
    except Exception as e:
        print(f"  ✗ Claude: failed to read credentials: {e}")
        ok = False
else:
    print("  ✗ Claude: not logged in — run 'claude login'")
    ok = False

# --- Codex ---
codex_creds = Path.home() / ".codex" / "auth.json"
if codex_creds.exists():
    try:
        data = json.loads(codex_creds.read_text())
        # Codex stores tokens nested under "tokens"
        tokens = data.get("tokens", {})
        token = tokens.get("access_token") or data.get("access_token") or data.get("api_key", "")
        if token:
            lines.append(f"OPENAI_API_KEY={token}")
            print("  ✓ Codex: token found")
        else:
            print("  - Codex: auth.json exists but no token (non-critical)")
    except Exception as e:
        print(f"  - Codex: failed to read auth.json: {e} (non-critical)")
else:
    print("  - Codex: not logged in — run 'codex login' if needed (non-critical)")

# --- Write .env ---
lines.append("LITELLM_MASTER_KEY=sk-litellm-local")

Path(env_file).write_text("\n".join(lines) + "\n")
# Restrict permissions — tokens are sensitive
Path(env_file).chmod(0o600)

if not ok:
    sys.exit(1)
PYEOF

if [ $? -ne 0 ]; then
    echo ""
    echo "Fix credential issues above, then re-run."
    exit 1
fi

echo "  Written to docker/.env (mode 600)"

# ---------------------------------------------------------------------------
# 2. Start infrastructure
# ---------------------------------------------------------------------------

MODE="${1:-full}"

if [ "$MODE" = "full" ] || [ "$MODE" = "infra" ]; then
    echo ""
    echo "=== Starting infrastructure ==="

    cd "$REPO_ROOT/docker"
    # Always run up -d — idempotent, picks up new .env if credentials changed
    docker compose up -d

    if ! curl -sf -H "Authorization: Bearer sk-litellm-local" http://localhost:4000/health >/dev/null 2>&1; then
        echo "  Waiting for LiteLLM..."
        for i in {1..30}; do
            if curl -sf -H "Authorization: Bearer sk-litellm-local" http://localhost:4000/health >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    fi

    if curl -sf -H "Authorization: Bearer sk-litellm-local" http://localhost:4000/health >/dev/null 2>&1; then
        echo "  ✓ LiteLLM proxy ready on :4000"
    else
        echo "  ✗ LiteLLM proxy not responding after 30s"
    fi

    cd "$REPO_ROOT"

    # -----------------------------------------------------------------------
    # Sandbox Agent — Rivet's coding-agent runtime (claude-code, codex, ...)
    # -----------------------------------------------------------------------
    echo ""
    echo "=== Starting Sandbox Agent ==="

    mkdir -p "$REPO_ROOT/runs"

    if ! command -v sandbox-agent >/dev/null 2>&1; then
        echo "  Installing sandbox-agent (Rust binary)..."
        curl -fsSL https://releases.rivet.dev/sandbox-agent/0.4.x/install.sh | sh
        # The installer typically drops the binary into a user-local bin dir
        # (~/.local/bin or similar) and adds it to PATH for new shells.
        if ! command -v sandbox-agent >/dev/null 2>&1; then
            # Try common install locations
            for d in "$HOME/.local/bin" "$HOME/.rivet/bin" "/usr/local/bin"; do
                if [ -x "$d/sandbox-agent" ]; then
                    export PATH="$d:$PATH"
                    break
                fi
            done
        fi
        if ! command -v sandbox-agent >/dev/null 2>&1; then
            echo "  ✗ sandbox-agent install failed — not on PATH after install"
            echo "    Install manually: curl -fsSL https://releases.rivet.dev/sandbox-agent/0.4.x/install.sh | sh"
            exit 1
        fi
        echo "  ✓ Installed: $(command -v sandbox-agent)"
    fi

    if curl -sf "http://localhost:${SANDBOX_AGENT_PORT}/v1/agents" >/dev/null 2>&1; then
        echo "  ✓ sandbox-agent already running on :${SANDBOX_AGENT_PORT}"
    else
        # Spawn detached, log to file, store PID for stop.sh
        nohup sandbox-agent server \
            --host 127.0.0.1 \
            --port "${SANDBOX_AGENT_PORT}" \
            --no-token \
            > "$SANDBOX_AGENT_LOG" 2>&1 &
        echo $! > "$SANDBOX_AGENT_PID_FILE"
        echo "  Started (PID $(cat "$SANDBOX_AGENT_PID_FILE")) on :${SANDBOX_AGENT_PORT}"
        echo "  Logs: $SANDBOX_AGENT_LOG"

        for i in {1..20}; do
            if curl -sf "http://localhost:${SANDBOX_AGENT_PORT}/v1/agents" >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done

        if curl -sf "http://localhost:${SANDBOX_AGENT_PORT}/v1/agents" >/dev/null 2>&1; then
            echo "  ✓ sandbox-agent ready on :${SANDBOX_AGENT_PORT}"
        else
            echo "  ✗ sandbox-agent not responding after 20s — check $SANDBOX_AGENT_LOG"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. Start aitelier service
# ---------------------------------------------------------------------------

if [ "$MODE" = "full" ] || [ "$MODE" = "service" ]; then
    echo ""
    echo "=== Starting aitelier service ==="

    if curl -sf http://localhost:7777/v1/health >/dev/null 2>&1; then
        echo "  ✓ aitelier service already running on :7777"
    else
        cd "$REPO_ROOT"
        uv run aitelier serve &
        AITELIER_PID=$!
        echo "  Started (PID $AITELIER_PID) on :7777"

        for i in {1..10}; do
            if curl -sf http://localhost:7777/v1/health >/dev/null 2>&1; then
                echo "  ✓ aitelier service ready"
                break
            fi
            sleep 1
        done
    fi
fi

echo ""
echo "=== Ready ==="
echo "  LiteLLM proxy:    http://localhost:4000"
echo "  Sandbox Agent:    http://localhost:${SANDBOX_AGENT_PORT}"
echo "  aitelier service: http://localhost:7777"
