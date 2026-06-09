#!/usr/bin/env bash
# Run the live test suite against Sandbox Agent in Docker mode.
#
# DESTRUCTIVE:
#   - Stops your currently-running aitelier service and host-mode SA.
#   - Swaps aitelier.toml to mode = "docker".
#   - Runs `make start` (builds the SA image, starts the compose `sa`
#     profile, brings aitelier back up).
#   - Executes `make test-live` against the Docker-hosted SA.
#   - Restores the previous aitelier.toml and restarts in the original
#     mode on exit (success or failure).
#
# REQUIREMENTS:
#   - Docker installed and running.
#   - Real LLM provider credentials extractable by the cred-materialize
#     step (~/.claude/.credentials.json or equivalent) — the live agent
#     tests need a working backend.
#
# Run with: ./scripts/test-docker-mode.sh
# Or via:   make test-docker-mode-e2e

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG=aitelier.toml
BACKUP=
CREATED_CONFIG=0

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ docker not installed; aborting."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "✗ docker daemon not reachable; aborting."
    exit 1
fi

cleanup() {
    echo ""
    echo "=== Restoring previous config ==="
    if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
        mv "$BACKUP" "$CONFIG"
        echo "  ✓ restored $CONFIG from $BACKUP"
    elif [ "$CREATED_CONFIG" = "1" ] && [ -f "$CONFIG" ]; then
        rm "$CONFIG"
        echo "  ✓ removed $CONFIG (no original to restore)"
    fi
    echo "=== Restarting in original mode ==="
    ./scripts/stop.sh >/dev/null 2>&1 || true
    ./scripts/start.sh >/dev/null 2>&1 || echo "  (you may need to restart manually)"
}
trap cleanup EXIT

echo "=== Saving current config ==="
if [ -f "$CONFIG" ]; then
    # Full template path is portable across macOS BSD-mktemp and Linux
    # GNU-mktemp (`-t prefix` differs between them).
    BACKUP="$(mktemp "${TMPDIR:-/tmp}/aitelier.toml.XXXXXX")"
    cp "$CONFIG" "$BACKUP"
    echo "  backed up $CONFIG → $BACKUP"
fi

echo "=== Writing mode = 'docker' config ==="
if [ ! -f "$CONFIG" ]; then
    CREATED_CONFIG=1
fi
cat > "$CONFIG.test-docker-mode" <<EOF
[sandbox_agent]
mode = "docker"
base_url = "http://localhost:2468"
EOF
mv "$CONFIG.test-docker-mode" "$CONFIG"

echo "=== Stopping current aitelier + host SA ==="
./scripts/stop.sh || true

echo "=== Starting in docker mode ==="
./scripts/start.sh

echo "=== Waiting for aitelier ready ==="
for _ in {1..30}; do
    if curl -sf http://localhost:7777/v1/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "=== Running live test suite ==="
AITELIER_LIVE_URL=http://localhost:7777 make test-live
