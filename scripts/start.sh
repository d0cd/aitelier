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
# The chosen URL is written to runs/.session.toml so the aitelier service picks it up.
#
# Credentials are extracted from Claude Code and Codex CLI credential files.
# No manual API keys needed — just run `claude login` and `codex login` first.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/docker/.env"
SANDBOX_AGENT_PID_FILE="$REPO_ROOT/runs/.sandbox-agent.pid"
SANDBOX_AGENT_LOG="$REPO_ROOT/runs/.sandbox-agent.log"
AITELIER_LOG_DIR="$REPO_ROOT/runs/logs"
AITELIER_LOG="$AITELIER_LOG_DIR/aitelier.log"
AITELIER_PID_FILE="$REPO_ROOT/runs/.aitelier.pid"

# Ollama mode: aitelier.toml [ollama] mode = "host" | "docker" is the
# canonical source. Legacy: `make start ollama` positional arg still
# forces docker mode for backwards compat.
for _arg in "$@"; do
    if [ "$_arg" = "ollama" ]; then
        export AITELIER_OLLAMA_PROFILE=1
        break
    fi
done

# Read aitelier.toml — if it says ollama.mode = "docker", flip the profile.
# Uses uv-run Python to read TOML (no jq/yq dep).
if [ -z "${AITELIER_OLLAMA_PROFILE:-}" ]; then
    OLLAMA_MODE="$(uv run python -c '
import sys, tomllib
from pathlib import Path
for p in [Path("aitelier.toml"), Path.home()/".config"/"aitelier"/"config.toml"]:
    if p.exists():
        try:
            print(tomllib.loads(p.read_text()).get("ollama", {}).get("mode", "host"))
            sys.exit(0)
        except Exception:
            pass
print("host")
' 2>/dev/null || echo "host")"
    if [ "$OLLAMA_MODE" = "docker" ]; then
        export AITELIER_OLLAMA_PROFILE=1
    fi
fi

# Sandbox Agent mode: aitelier.toml [sandbox_agent] mode = "host" |
# "docker" | "remote". "docker" flips the compose `sa` profile and skips
# the host binary install. "remote" is auto-detected later by checking
# whether base_url points off-localhost (preserves current behavior).
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
if [ "$SA_MODE" = "docker" ]; then
    export AITELIER_SA_PROFILE=1
fi

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

# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

_check_port_or_die() {
    # $1 = port, $2 = label, $3 = remediation hint for non-container holders.
    if ! _port_in_use "$1"; then
        return 0
    fi
    if _compose_owns_port "$1"; then
        return 0   # our own container; docker compose up -d will reuse it.
    fi
    local holder other_container
    holder="$(_port_holder "$1")"
    other_container="$(_other_container_on_port "$1")"
    echo ""
    if [ -n "$other_container" ]; then
        echo "  ✗ port $1 ($2) held by docker container \"$other_container\" (not ours)"
        echo "    → docker stop $other_container   (or change our host port in docker/docker-compose.yml)"
    else
        echo "  ✗ port $1 ($2) held by ${holder:-unknown}"
        echo "    → $3"
    fi
    return 1
}

SESSION_TOML="$REPO_ROOT/runs/.session.toml"

# If a previous .session.toml exists AND its sandbox-agent URL is still
# reachable, reuse its port. Otherwise the file is stale (SA was killed
# without scripts/stop.sh running, or restarted out of band) — overwrite.
# Without this, we'd pick a fresh port while a running aitelier service
# still has the stale one cached in its config singleton.
_existing_sa_url() {
    if [ ! -f "$SESSION_TOML" ]; then return 1; fi
    awk '/^\[sandbox_agent\]/{f=1; next} /^\[/{f=0} f && /^base_url/' "$SESSION_TOML" \
        | head -1 | sed -E 's/.*"([^"]+)".*/\1/'
}

if [ -n "$SANDBOX_AGENT_PORT_REQUESTED" ]; then
    SANDBOX_AGENT_PORT="$SANDBOX_AGENT_PORT_REQUESTED"
elif [ -n "${SANDBOX_AGENT_PORT:-}" ]; then
    :  # user already set it via env
elif _existing_url="$(_existing_sa_url)" && [ -n "$_existing_url" ] \
        && curl -sf "${_existing_url}/v1/agents" >/dev/null 2>&1; then
    # Reuse the port from a reachable previous session.
    SANDBOX_AGENT_PORT="${_existing_url##*:}"
    echo "  (reusing reachable SA from .session.toml: $_existing_url)"
elif _port_in_use 2468; then
    SANDBOX_AGENT_PORT="$(_pick_free_port)"
    echo "  (port 2468 in use; picked free port $SANDBOX_AGENT_PORT)"
else
    SANDBOX_AGENT_PORT=2468
fi

# Communicate runtime-only values (chosen sandbox-agent port, dev Postgres
# DSN) to the aitelier service via runs/.session.toml — a gitignored overlay
# loaded on top of aitelier.toml. Static values belong in aitelier.toml; this
# file is for things start.sh discovers at runtime that the user can't write
# ahead of time. stop.sh removes it.
mkdir -p "$REPO_ROOT/runs"
cat > "$SESSION_TOML" <<EOF
# Written by scripts/start.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ"). Ephemeral.
# Removed by scripts/stop.sh. Do not edit by hand — your changes will be
# overwritten on next start. Put persistent config in aitelier.toml instead.

[sandbox_agent]
base_url = "http://127.0.0.1:${SANDBOX_AGENT_PORT}"

[database]
url = "postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier"
EOF

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

# Ollama API base. Default to the host install via host.docker.internal;
# `make start ollama` (or AITELIER_OLLAMA_PROFILE=1) flips this to the
# in-compose service.
import os as _os
if _os.environ.get("AITELIER_OLLAMA_PROFILE") == "1":
    lines.append("OLLAMA_BASE_URL=http://ollama:11434")
else:
    lines.append("OLLAMA_BASE_URL=http://host.docker.internal:11434")

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

# "ollama" positional arg activates the containerized-Ollama profile.
# Compose only starts services in active profiles, so default `make start`
# leaves Ollama OFF (use host install).
COMPOSE_PROFILE_ARGS=()
if [ "$MODE" = "ollama" ] || [ "${AITELIER_OLLAMA_PROFILE:-}" = "1" ]; then
    export AITELIER_OLLAMA_PROFILE=1
    COMPOSE_PROFILE_ARGS+=("--profile" "ollama")
    MODE="full"  # ollama is a flavor of full, not a separate mode
fi
if [ "${AITELIER_SA_PROFILE:-}" = "1" ]; then
    COMPOSE_PROFILE_ARGS+=("--profile" "sa")
fi

if [ "$MODE" = "full" ] || [ "$MODE" = "infra" ]; then
    echo ""
    echo "=== Preflight ==="
    preflight_ok=1
    # _check_port_or_die treats our own running container as fine, so we can
    # call it unconditionally — `docker compose up -d` is idempotent.
    _check_port_or_die 5433 "Postgres" \
        "stop the conflicting process or change docker/docker-compose.yml host port" \
        || preflight_ok=0
    _check_port_or_die 4000 "LiteLLM proxy" \
        "stop the conflicting process or override LITELLM_BASE_URL" \
        || preflight_ok=0
    if [ $preflight_ok -eq 0 ]; then
        echo ""
        echo "  Fix the port conflict(s) above, then re-run \`make start\`."
        exit 1
    fi
    echo "  ✓ ports clear"

    echo ""
    echo "=== Starting infrastructure ==="

    cd "$REPO_ROOT/docker"
    # Always run up -d — idempotent, picks up new .env if credentials changed
    docker compose "${COMPOSE_PROFILE_ARGS[@]}" up -d

    echo "  Waiting for Postgres..."
    for i in {1..30}; do
        if docker compose exec -T postgres pg_isready -U aitelier -d aitelier >/dev/null 2>&1; then
            echo "  ✓ Postgres ready"
            break
        fi
        sleep 1
    done

    # Use /health/liveness — no auth, no upstream-provider probing. /health
    # would 5xx on transient upstream issues (e.g. OpenAI 429) and we'd
    # falsely think the proxy is down.
    if ! curl -sf http://localhost:4000/health/liveness >/dev/null 2>&1; then
        echo "  Waiting for LiteLLM..."
        for i in {1..30}; do
            if curl -sf http://localhost:4000/health/liveness >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    fi

    if curl -sf http://localhost:4000/health/liveness >/dev/null 2>&1; then
        echo "  ✓ LiteLLM proxy ready on :4000"
    else
        echo "  ✗ LiteLLM proxy not responding after 30s — check 'docker logs docker-litellm-1'"
        exit 1
    fi

    cd "$REPO_ROOT"

    # -----------------------------------------------------------------------
    # Sandbox Agent — Rivet's coding-agent runtime (claude-code, codex, ...)
    #
    # Remote mode: if [sandbox_agent] base_url in aitelier.toml points at a
    # non-local URL, skip the local binary install. Closed-laptop tolerance:
    # aitelier on your machine, agent runs in the cloud (E2B, Daytona, ...).
    # -----------------------------------------------------------------------
    echo ""
    echo "=== Sandbox Agent ==="

    # Resolve the URL the aitelier service WILL use, by asking the config
    # loader (so we honor whatever layering of aitelier.toml + secrets +
    # session is in effect). We need to read this without trusting any env.
    # Strip the session overlay we just wrote so we see the user-declared
    # base_url (which may be remote) rather than the local one start.sh
    # would have used.
    RESOLVED_SANDBOX_URL="$(uv run --project core python -c '
from pathlib import Path
from aitelier.config import load_config
# Temporarily move the session overlay aside so we see user intent.
session = Path("runs/.session.toml")
backup = None
if session.exists():
    backup = session.read_text()
    session.unlink()
try:
    print(load_config().sandbox_agent.base_url)
finally:
    if backup is not None:
        session.write_text(backup)
' 2>/dev/null || echo "http://localhost:2468")"

    if [ "${AITELIER_SA_PROFILE:-}" = "1" ]; then
        echo "  Docker: SA runs in the compose `sa` profile container"
        echo "  → docker compose --profile sa up -d (handled above)"
        for i in {1..30}; do
            if curl -sf "http://localhost:2468/v1/agents" >/dev/null 2>&1; then
                echo "  ✓ sandbox-agent reachable on :2468 (docker)"
                break
            fi
            sleep 1
        done
        if ! curl -sf "http://localhost:2468/v1/agents" >/dev/null 2>&1; then
            echo "  ✗ docker sandbox-agent not responding after 30s"
            echo "    Check: docker compose --profile sa logs sandbox-agent"
        fi
    elif [[ "$RESOLVED_SANDBOX_URL" != *"localhost"* ]] \
       && [[ "$RESOLVED_SANDBOX_URL" != *"127.0.0.1"* ]]; then
        echo "  Remote: $RESOLVED_SANDBOX_URL (skipping local install)"
        # Rewrite the session overlay so the remote URL wins over the
        # dynamic local port we provisionally wrote earlier.
        cat > "$SESSION_TOML" <<EOF
# Written by scripts/start.sh — remote sandbox-agent mode.

[sandbox_agent]
base_url = "$RESOLVED_SANDBOX_URL"

[database]
url = "postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier"
EOF
        SANDBOX_TOKEN_VAL="$(uv run --project core python -c 'from aitelier.config import load_config; print(load_config().sandbox_agent.token or "")' 2>/dev/null || echo "")"
        auth_header=()
        if [ -n "$SANDBOX_TOKEN_VAL" ]; then
            auth_header=("-H" "Authorization: Bearer $SANDBOX_TOKEN_VAL")
        fi
        if curl -sf "${auth_header[@]}" "$RESOLVED_SANDBOX_URL/v1/agents" >/dev/null 2>&1; then
            echo "  ✓ remote sandbox-agent reachable"
        else
            echo "  ✗ remote sandbox-agent unreachable at $RESOLVED_SANDBOX_URL"
            echo "    Check [sandbox_agent] token in aitelier.secrets.toml and that the host is up."
        fi
    else
        echo "=== Starting Sandbox Agent (local) ==="

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
    fi  # end: local-vs-remote sandbox-agent branch
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
        mkdir -p "$AITELIER_LOG_DIR"
        # Detach + persist logs. `make logs` tails this file alongside the
        # sandbox-agent log; `make stop` reads the PID file.
        # `--project core` ensures we run from the workspace install (with all
        # deps incl. asyncpg) rather than any stale `uv tool install aitelier`
        # on PATH.
        nohup uv run --project core aitelier serve >> "$AITELIER_LOG" 2>&1 &
        AITELIER_PID=$!
        echo "$AITELIER_PID" > "$AITELIER_PID_FILE"
        echo "  Started (PID $AITELIER_PID) on :7777"
        echo "  Logs: $AITELIER_LOG"

        for i in {1..10}; do
            if curl -sf http://localhost:7777/v1/health >/dev/null 2>&1; then
                echo "  ✓ aitelier service ready"
                break
            fi
            sleep 1
        done

        if ! curl -sf http://localhost:7777/v1/health >/dev/null 2>&1; then
            echo "  ✗ aitelier service not responding after 10s — check $AITELIER_LOG"
        fi
    fi
fi

echo ""
echo "=== Ready ==="
echo "  LiteLLM proxy:    http://localhost:4000"
echo "  Sandbox Agent:    http://127.0.0.1:${SANDBOX_AGENT_PORT}"
echo "  aitelier service: http://localhost:7777"
