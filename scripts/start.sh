#!/usr/bin/env bash
# Start aitelier — materialize provider credentials, boot infra, launch service.
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
# Claude prefers the long-lived setup token already registered with brig, then
# falls back to the legacy Claude Code credentials file. Codex uses auth.json.

set -euo pipefail

# Runtime overlays and credential snapshots must never inherit a permissive
# host umask. Existing files are tightened explicitly at their write sites.
umask 077

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/docker/.env"
CLAUDE_SETUP_TOKEN_FILE="$REPO_ROOT/runs/.claude-oauth-token"
CLAUDE_SETUP_TOKEN_FINGERPRINT_FILE="$REPO_ROOT/runs/.claude-oauth-token.sha256"
SANDBOX_AGENT_TOKEN_FINGERPRINT_FILE="$REPO_ROOT/runs/.sandbox-agent-claude-token.sha256"
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
# "docker" | "brig" | "remote". "docker" flips the compose `sa` profile.
# "brig" launches SA in an isolated brig cell and points aitelier at the
# cell's ingress (skips the host binary install). "remote" assumes SA runs
# elsewhere — honored explicitly here, or auto-detected when base_url points
# off-localhost (preserves prior behavior). host (default) installs + runs
# the SA binary locally.
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

# Sandbox Agent install channel — single source so the install command and
# its failure-hint can't drift. `0.4.x` tracks the latest 0.4 patch; for
# reproducible deploys, pin an exact patch (e.g. "0.4.3") here and keep the
# docker/*.Dockerfile ARGs in sync. Override via env if needed.
SANDBOX_AGENT_CHANNEL="${SANDBOX_AGENT_CHANNEL:-0.4.x}"
# Sandbox Agent's ACP proxy otherwise defaults to 120s, shorter than
# aitelier's 600s default and common explicit long-run timeouts. Keep the
# inner request alive long enough for aitelier to remain the authoritative
# deadline owner; operators can still override this deployment default.
SANDBOX_AGENT_ACP_REQUEST_TIMEOUT_MS="${SANDBOX_AGENT_ACP_REQUEST_TIMEOUT_MS:-3660000}"
export SANDBOX_AGENT_ACP_REQUEST_TIMEOUT_MS

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

if [ "$SA_MODE" = "brig" ] || [ "$SA_MODE" = "remote" ]; then
    # SA is off-host (brig cell / remote URL). There is no local port to
    # resolve, and base_url comes from aitelier.toml — skip the free-port
    # dance so we don't write a bogus local base_url into the overlay.
    SANDBOX_AGENT_PORT=""
elif [ "$SA_MODE" = "docker" ]; then
    # docker compose binds the container's 2468 to host 2468 — it can't take
    # a dynamic host port. Skip the free-port dance entirely; picking a random
    # port here would write a base_url into the session overlay that nothing
    # listens on (the container is still on 2468), failing every dispatch.
    SANDBOX_AGENT_PORT=2468
elif [ -n "$SANDBOX_AGENT_PORT_REQUESTED" ]; then
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
if [ -L "$SESSION_TOML" ]; then
    echo "  ✗ refusing to replace symlinked runtime config: $SESSION_TOML"
    exit 1
fi
if [ -e "$SESSION_TOML" ]; then
    chmod 600 "$SESSION_TOML"
fi
_overlay_header="# Written by scripts/start.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ"). Ephemeral.
# Removed by scripts/stop.sh. Do not edit by hand — your changes will be
# overwritten on next start. Put persistent config in aitelier.toml instead."
if [ "$SA_MODE" = "brig" ]; then
    # SA runs in a brig cell. base_url comes from aitelier.toml (the cell
    # ingress); inject the ingress bearer token from brig's own secret store
    # so it isn't duplicated into aitelier.secrets.toml — single source of
    # truth is the brig secret. Deep-merges over aitelier.toml's
    # [sandbox_agent], so base_url is preserved.
    _sa_ingress_token="$(cat "$(_brig_ingress_token_file)" 2>/dev/null || true)"
    {
        echo "$_overlay_header"
        echo ""
        echo "[sandbox_agent]"
        [ -n "$_sa_ingress_token" ] && echo "token = \"$_sa_ingress_token\""
        echo ""
        echo "[database]"
        echo "url = \"postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier\""
    } > "$SESSION_TOML"
elif [ "$SA_MODE" = "remote" ]; then
    # base_url + token come from aitelier.toml / aitelier.secrets.toml; the
    # overlay only carries the runtime DB DSN.
    {
        echo "$_overlay_header"
        echo ""
        echo "[database]"
        echo "url = \"postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier\""
    } > "$SESSION_TOML"
else
    cat > "$SESSION_TOML" <<EOF
$_overlay_header

[sandbox_agent]
base_url = "http://127.0.0.1:${SANDBOX_AGENT_PORT}"

[database]
url = "postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier"
EOF
fi
chmod 600 "$SESSION_TOML"

# ---------------------------------------------------------------------------
# 1. Materialize provider credentials
# ---------------------------------------------------------------------------

echo "=== Materializing credentials ==="

# Safety: ensure docker/.env is gitignored
if ! grep -q "docker/.env" "$REPO_ROOT/.gitignore" 2>/dev/null; then
    echo "docker/.env" >> "$REPO_ROOT/.gitignore"
    echo "  Added docker/.env to .gitignore"
fi

# Resolve credentials via Python (handles JSON + expiry check). Credentials are
# advisory, not required: the control plane and the local/ollama LLM paths need
# no API key, so a missing or expired Claude/Codex login degrades capability but
# never blocks startup. Absent keys are written empty so LiteLLM's env refs still
# resolve and the proxy boots; the affected models just error at call time.
# Use the uv-managed interpreter so startup works on machines without a
# system Python. The helper atomically writes private files and is unit tested
# independently from the service lifecycle.
uv run python "$REPO_ROOT/scripts/materialize_credentials.py" \
    "$ENV_FILE" \
    "$CLAUDE_SETUP_TOKEN_FILE" \
    "$CLAUDE_SETUP_TOKEN_FINGERPRINT_FILE"

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
    # Mode transitions are handled before Compose claims :2468. Doing this
    # after `docker compose up` is too late: the old host process would make
    # container creation fail before the cleanup branch could run.
    if [ "${AITELIER_SA_PROFILE:-}" = "1" ] \
       && [ -f "$SANDBOX_AGENT_PID_FILE" ]; then
        HOST_SA_PID="$(cat "$SANDBOX_AGENT_PID_FILE" 2>/dev/null || true)"
        if _process_matches "$HOST_SA_PID" "sandbox-agent server"; then
            echo "  Stopping host-mode SA before switching to Docker (PID $HOST_SA_PID)"
            kill "$HOST_SA_PID"
            for _ in {1..50}; do
                kill -0 "$HOST_SA_PID" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "$HOST_SA_PID" 2>/dev/null; then
                echo "  ✗ host Sandbox Agent did not stop; refusing to start Docker SA"
                exit 1
            fi
            rm -f "$SANDBOX_AGENT_PID_FILE"
        elif kill -0 "$HOST_SA_PID" 2>/dev/null; then
            echo "  ✗ refusing to stop PID $HOST_SA_PID: it is not Sandbox Agent"
            echo "    Remove the stale $SANDBOX_AGENT_PID_FILE after checking that process."
            exit 1
        else
            rm -f "$SANDBOX_AGENT_PID_FILE"
        fi
    fi
    # _check_port_or_die treats our own running container as fine, so we can
    # call it unconditionally — `docker compose up -d` is idempotent.
    _check_port_or_die 5433 "Postgres" \
        "stop the conflicting process or change docker/docker-compose.yml host port" \
        || preflight_ok=0
    _check_port_or_die 4000 "LiteLLM proxy" \
        "stop the conflicting process or override LITELLM_BASE_URL" \
        || preflight_ok=0
    if [ "${AITELIER_SA_PROFILE:-}" = "1" ]; then
        _check_port_or_die 2468 "Docker Sandbox Agent" \
            "stop the conflicting process or switch [sandbox_agent] mode" \
            || preflight_ok=0
    fi
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
        echo "  Docker: SA runs in the Compose profile named sa"
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
    elif [ "$SA_MODE" = "brig" ]; then
        echo "  Brig: Sandbox Agent runs in an isolated brig cell"
        if ! _brig_cell_up "$REPO_ROOT"; then
            echo "  ✗ could not bring up the brig SA cell (see above)."
            echo "    [sandbox_agent] mode = \"brig\" requires the cell — not"
            echo "    falling back to a local SA. Fix the prereq above and retry."
            exit 1
        fi
        # base_url = aitelier.toml's cell ingress; the ingress token was
        # injected into the session overlay above. Verify reachability.
        SANDBOX_TOKEN_VAL="$(cat "$(_brig_ingress_token_file)" 2>/dev/null || true)"
        auth_header=()
        if [ -n "$SANDBOX_TOKEN_VAL" ]; then
            auth_header=("-H" "Authorization: Bearer $SANDBOX_TOKEN_VAL")
        fi
        # brig owns the SA cell's lifecycle (`brig system watchdog` recovers it
        # after host sleep/reboot). We only probe it — a real /v1/agents check
        # with the token, so a wedged cell (ingress up, agent dead → 502) is
        # caught. Retry so a cell mid-recovery isn't treated as a hard failure.
        _sa_ok=0
        for _ in $(seq 1 15); do
            if curl -sf "${auth_header[@]}" "$RESOLVED_SANDBOX_URL/v1/agents" >/dev/null 2>&1; then
                _sa_ok=1; break
            fi
            sleep 2
        done
        if [ "$_sa_ok" = 1 ]; then
            echo "  ✓ brig SA reachable via ingress $RESOLVED_SANDBOX_URL"
        else
            echo "  ✗ brig SA unreachable at $RESOLVED_SANDBOX_URL after 30s"
            echo "    brig manages the SA cell — check: brig system doctor; brig cell list"
            exit 1
        fi
    elif [ "$SA_MODE" = "remote" ] \
       || { [[ "$RESOLVED_SANDBOX_URL" != *"localhost"* ]] \
            && [[ "$RESOLVED_SANDBOX_URL" != *"127.0.0.1"* ]]; }; then
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
        echo "  Installing sandbox-agent (Rust binary, channel $SANDBOX_AGENT_CHANNEL)..."
        curl -fsSL "https://releases.rivet.dev/sandbox-agent/${SANDBOX_AGENT_CHANNEL}/install.sh" | sh
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
            echo "    Install manually: curl -fsSL https://releases.rivet.dev/sandbox-agent/${SANDBOX_AGENT_CHANNEL}/install.sh | sh"
            exit 1
        fi
        echo "  ✓ Installed: $(command -v sandbox-agent)"
    fi

    _expected_claude_fingerprint="$(cat "$CLAUDE_SETUP_TOKEN_FINGERPRINT_FILE")"
    _applied_claude_fingerprint="$(cat "$SANDBOX_AGENT_TOKEN_FINGERPRINT_FILE" 2>/dev/null || echo unknown)"
    _host_sa_reachable=0
    if curl -sf "http://localhost:${SANDBOX_AGENT_PORT}/v1/agents" >/dev/null 2>&1; then
        _host_sa_reachable=1
    fi

    # A setup-token rotation must reach an already-running host SA. Restart
    # only a process owned by this start script; never kill an unrelated
    # listener merely because its credential state is unknowable.
    if [ "$_host_sa_reachable" = 1 ] \
       && [ "$_expected_claude_fingerprint" != "$_applied_claude_fingerprint" ]; then
        _host_sa_pid="$(cat "$SANDBOX_AGENT_PID_FILE" 2>/dev/null || true)"
        if _process_matches \
            "$_host_sa_pid" "sandbox-agent server" "--port ${SANDBOX_AGENT_PORT}"; then
            echo "  Claude credential changed; restarting host Sandbox Agent"
            kill "$_host_sa_pid"
            for _ in $(seq 1 50); do
                kill -0 "$_host_sa_pid" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "$_host_sa_pid" 2>/dev/null; then
                echo "  ✗ host Sandbox Agent did not stop after credential change"
                exit 1
            fi
            rm -f "$SANDBOX_AGENT_PID_FILE"
            _host_sa_reachable=0
        else
            echo "  ! reachable Sandbox Agent was not started by aitelier; its Claude credential cannot be verified"
            echo "    Stop it and rerun make start to apply the configured setup token."
        fi
    fi

    if [ "$_host_sa_reachable" = 1 ]; then
        echo "  ✓ sandbox-agent already running on :${SANDBOX_AGENT_PORT}"
    else
        # Spawn detached, log to file, store PID for stop.sh
        if [ -s "$CLAUDE_SETUP_TOKEN_FILE" ]; then
            CLAUDE_CODE_OAUTH_TOKEN="$(cat "$CLAUDE_SETUP_TOKEN_FILE")" \
                nohup sandbox-agent server \
                --host 127.0.0.1 \
                --port "${SANDBOX_AGENT_PORT}" \
                --no-token \
                > "$SANDBOX_AGENT_LOG" 2>&1 &
        else
            nohup sandbox-agent server \
                --host 127.0.0.1 \
                --port "${SANDBOX_AGENT_PORT}" \
                --no-token \
                > "$SANDBOX_AGENT_LOG" 2>&1 &
        fi
        echo $! > "$SANDBOX_AGENT_PID_FILE"
        cp "$CLAUDE_SETUP_TOKEN_FINGERPRINT_FILE" \
           "$SANDBOX_AGENT_TOKEN_FINGERPRINT_FILE"
        chmod 600 "$SANDBOX_AGENT_TOKEN_FINGERPRINT_FILE"
        echo "  Started (PID $(cat "$SANDBOX_AGENT_PID_FILE")) on :${SANDBOX_AGENT_PORT}"
        echo "  Logs: $SANDBOX_AGENT_LOG"

        for _ in {1..20}; do
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

        for _ in {1..10}; do
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
if [ "$SA_MODE" = "brig" ] || [ "$SA_MODE" = "remote" ]; then
    echo "  Sandbox Agent:    ${RESOLVED_SANDBOX_URL:-(see [sandbox_agent] in aitelier.toml)} ($SA_MODE)"
else
    echo "  Sandbox Agent:    http://127.0.0.1:${SANDBOX_AGENT_PORT}"
fi
echo "  aitelier service: http://localhost:7777"
