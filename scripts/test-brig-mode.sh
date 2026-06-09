#!/usr/bin/env bash
# Local end-to-end test for aitelier deployed as a brig cell.
#
# Mirrors test-docker-mode.sh but targets brig instead of compose. Skips
# cleanly if brig isn't installed — brig isn't on PyPI / homebrew, so
# this script is local-only by design.
#
# What it does:
#   1. Verifies `brig` is on PATH.
#   2. Verifies `localhost/aitelier:latest` image exists (or builds it
#      from docker/Dockerfile, since brig uses container images).
#   3. Launches the cell from docs/deploy/aitelier.cell.yaml.
#   4. Polls the cell's ingress for /v1/health readiness.
#   5. Runs the live test suite against aitelier-in-cell.
#   6. Tears down on exit (success or failure).
#
# What it doesn't do:
#   - Set up Postgres + LiteLLM as separate cells (the cell yaml's
#     host_services lines are commented out; configure them for your
#     environment).
#   - Materialize Claude / Codex credentials in the cell (the secrets
#     block in the yaml lists them but you must add them via
#     `brig secrets add` before running this).
#
# Run with:
#   ./scripts/test-brig-mode.sh
# Or:
#   make test-brig-mode-e2e

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

CELL_YAML="docs/deploy/aitelier.cell.yaml"
CELL_NAME="aitelier"
INGRESS_URL="${BRIG_AITELIER_URL:-http://aitelier.host.brig}"

if ! command -v brig >/dev/null 2>&1; then
    echo "✗ brig not installed; skipping brig-mode e2e."
    echo "  This script targets a brig-cell deployment of aitelier."
    echo "  See docs/deploy/aitelier.cell.yaml for the cell definition."
    exit 0
fi

if ! brig system status >/dev/null 2>&1; then
    echo "✗ brig is installed but \`brig system status\` failed."
    echo "  Make sure brig's daemon is running."
    exit 1
fi

cleanup() {
    echo ""
    echo "=== Tearing down cell ==="
    brig stop "$CELL_NAME" >/dev/null 2>&1 || true
    echo "  ✓ cell stopped"
}
trap cleanup EXIT

echo "=== Checking aitelier image ==="
if ! brig image list 2>/dev/null | grep -q "localhost/aitelier:latest"; then
    echo "  aitelier image not found; building from docker/Dockerfile..."
    if brig image build --tag aitelier:latest --file docker/Dockerfile .; then
        echo "  ✓ built localhost/aitelier:latest"
    else
        echo "  ✗ build failed."
        echo "    Build manually: brig image build --tag aitelier:latest \\"
        echo "                                     --file docker/Dockerfile ."
        exit 1
    fi
else
    echo "  ✓ localhost/aitelier:latest already present"
fi

echo "=== Stopping any prior cell ==="
brig stop "$CELL_NAME" >/dev/null 2>&1 || true

echo "=== Launching cell from $CELL_YAML ==="
if ! brig run --file "$CELL_YAML" -d; then
    echo "  ✗ \`brig run\` failed."
    echo "    Check that secrets referenced in the yaml are registered:"
    echo "      brig secrets list"
    echo "    And that policy.allow covers the hosts your build needs."
    exit 1
fi

echo "=== Waiting for ingress on $INGRESS_URL ==="
for i in {1..60}; do
    if curl -sf "$INGRESS_URL/v1/health" >/dev/null 2>&1; then
        echo "  ✓ aitelier reachable after ${i}s"
        break
    fi
    sleep 1
done

if ! curl -sf "$INGRESS_URL/v1/health" >/dev/null 2>&1; then
    echo "  ✗ aitelier not responding at $INGRESS_URL after 60s"
    echo "    Inspect: brig logs $CELL_NAME -f"
    exit 1
fi

echo "=== Running live test suite against $INGRESS_URL ==="
AITELIER_LIVE_URL="$INGRESS_URL" make test-live
