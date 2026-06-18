#!/usr/bin/env bash
# Shared helpers sourced by start.sh, doctor.sh, status.sh.
# Pure functions only — no I/O at source time.

_port_in_use() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        nc -z 127.0.0.1 "$1" >/dev/null 2>&1
    fi
}

_port_holder() {
    # Print "<cmd> (PID <n>)" for the process listening on $1, or empty.
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fpcn 2>/dev/null | awk '
            /^p/ { pid=substr($0,2) }
            /^c/ { cmd=substr($0,2); printf "%s (PID %s)\n", cmd, pid; exit }
        '
    fi
}

_compose_owns_port() {
    # Returns 0 if one of OUR docker compose services already publishes this port.
    # Identifies "ours" via the com.docker.compose.project=docker label
    # (the dir-name aitelier's compose file lives in).
    docker ps --filter "publish=$1" --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
        | grep -qx docker
}

_other_container_on_port() {
    # Name of the (non-ours) docker container holding the port, or empty.
    docker ps --filter "publish=$1" --format '{{.Names}}' 2>/dev/null | head -1
}

_pick_free_port() {
    # `uv run python`, not bare `python3` — works on a uv-managed machine
    # with no system python3 (matches credential extraction in start.sh).
    uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# --- Brig cell lifecycle (SA-in-a-cell) ------------------------------------
# Shared by start.sh (bring up) and stop.sh (tear down). Constants match
# scripts/test-brig-mode.sh and docs/deploy/sandbox-agent.cell.yaml.
_BRIG_CELL="sandbox-agent"
_BRIG_CELL_YAML="docs/deploy/sandbox-agent.cell.yaml"
_BRIG_IMAGE_TAG="sandbox-agent-brig:latest"
_BRIG_DOCKERFILE="docker/sandbox-agent.brig.Dockerfile"

_brig_ingress_token_file() {
    echo "${HOME}/.brig/secrets/${_BRIG_CELL}-ingress-token"
}

_brig_ingress_url() {
    # brig publishes the ingress reverse-proxy on host 127.0.0.1:8443 as
    # /{cell}/{path_prefix}/... . Overridable for non-default brig setups.
    echo "${BRIG_SA_URL:-http://127.0.0.1:8443/${_BRIG_CELL}}"
}

_brig_cell_up() {
    # Ensure the SA brig cell is running and its ingress answers. Reuses a
    # live cell; otherwise builds the image (cold path) and launches it.
    # Returns non-zero (with a remediation hint) if a prereq is missing —
    # callers should fail rather than silently fall back to a local SA.
    local repo_root="$1"
    local ingress token i
    ingress="$(_brig_ingress_url)"
    local token_file; token_file="$(_brig_ingress_token_file)"

    if ! command -v brig >/dev/null 2>&1; then
        echo "  ✗ brig not on PATH. Install: uv tool install -e ~/projects/brig"
        return 1
    fi
    # Self-heal the VM dependency: `brig system up` is idempotent ("ensure VM
    # + warden running"). Doing it here makes both `make start` and the
    # launchd supervisor recover after a reboot instead of crash-looping
    # because the VM happened to be down.
    if ! brig system doctor --quick >/dev/null 2>&1; then
        echo "  Brig VM not ready — bringing it up (brig system up) ..."
        if ! brig system up >/dev/null 2>&1; then
            echo "  ✗ 'brig system up' failed — run it manually and retry."
            return 1
        fi
        local up_ok="" i
        for i in $(seq 1 30); do
            if brig system doctor --quick >/dev/null 2>&1; then up_ok=1; break; fi
            sleep 1
        done
        if [ -z "$up_ok" ]; then
            echo "  ✗ brig VM still not healthy 30s after 'brig system up'."
            return 1
        fi
        echo "  ✓ brig VM up"
    fi
    if [ ! -f "$token_file" ]; then
        echo "  ✗ ingress token not registered ($token_file)."
        echo "    python3 -c 'import secrets;print(secrets.token_urlsafe(32))' \\"
        echo "      | brig secrets add ${_BRIG_CELL}-ingress-token"
        return 1
    fi
    token="$(cat "$token_file")"

    # Warm path: a cell whose ingress already answers — reuse it.
    if curl -sf -H "Authorization: Bearer $token" "$ingress/v1/agents" >/dev/null 2>&1; then
        echo "  ✓ brig SA cell already running (ingress reachable)"
        return 0
    fi

    # Cold path: build (if image absent) + launch.
    if [ ! -x "$repo_root/docker/prebaked-agents/claude/claude" ]; then
        echo "  ✗ docker/prebaked-agents/claude/claude missing — brig's mitmproxy"
        echo "    is too slow for SA's install timeout, so the claude binary must"
        echo "    be pre-baked. Fetch it once (see $_BRIG_DOCKERFILE)."
        return 1
    fi
    if ! brig image ls 2>/dev/null | grep -q "sandbox-agent-brig"; then
        echo "  Building $_BRIG_IMAGE_TAG (first run) ..."
        if ! ( cd "$repo_root" && brig image build --tag "$_BRIG_IMAGE_TAG" --file "$_BRIG_DOCKERFILE" . ); then
            echo "  ✗ brig image build failed"
            return 1
        fi
    fi
    echo "  Launching cell from $_BRIG_CELL_YAML ..."
    brig cell stop "$_BRIG_CELL" >/dev/null 2>&1 || true
    brig cell rm "$_BRIG_CELL" >/dev/null 2>&1 || true
    if ! ( cd "$repo_root" && brig run --file "$_BRIG_CELL_YAML" -d ); then
        echo "  ✗ brig run failed. Check: brig secrets list; brig policy show $_BRIG_CELL"
        return 1
    fi
    echo "  Waiting for SA ingress on $ingress ..."
    for i in $(seq 1 60); do
        if curl -sf -H "Authorization: Bearer $token" "$ingress/v1/agents" >/dev/null 2>&1; then
            echo "  ✓ brig SA reachable through ingress after ${i}s"
            return 0
        fi
        sleep 1
    done
    echo "  ✗ brig SA not responding through ingress after 60s (brig cell logs $_BRIG_CELL -f)"
    return 1
}

_brig_cell_down() {
    command -v brig >/dev/null 2>&1 || return 0
    brig cell stop "$_BRIG_CELL" >/dev/null 2>&1 || true
    brig cell rm "$_BRIG_CELL" >/dev/null 2>&1 || true
}
