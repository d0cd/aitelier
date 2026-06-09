#!/usr/bin/env bash
# Local end-to-end test for aitelier deployed as a brig cell.
#
# Mirrors test-docker-mode.sh but targets brig instead of compose. Skips
# cleanly if brig isn't installed — brig isn't on PyPI / homebrew, so
# this script is local-only by design.
#
# Uses post-restructure `brig <noun> <verb>` CLI form
# (per the brig CLI restructure shipped earlier).
#
# What it does:
#   1. Verifies `brig` is on PATH and the daemon responds.
#   2. Ensures `localhost/aitelier:latest` image exists (or builds it).
#   3. Launches the cell from docs/deploy/aitelier.cell.yaml.
#   4. Polls the cell's ingress for /v1/health readiness.
#   5. Runs the live test suite against aitelier-in-cell.
#   6. Tears down on exit (success or failure).
#
# What it doesn't do:
#   - Set up Postgres + LiteLLM as separate cells (the cell yaml lists
#     `*.host.brig` URLs but assumes you've already configured brig's
#     virtual-domain routing to reach them).
#   - Materialize Claude / Codex credentials or aitelier.toml as brig
#     secrets — the secrets block lists them but you must add them via
#     `brig secrets add` before running this.
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

# brig CLI is `brig <noun> <verb>` post-restructure. Probe for
# `brig system status` first; if that fails, the daemon's down.
if ! brig system status >/dev/null 2>&1; then
    echo "✗ \`brig system status\` failed."
    echo "  Make sure brig's daemon is running."
    exit 1
fi

cleanup() {
    echo ""
    echo "=== Tearing down cell ==="
    brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true
    echo "  ✓ cell stopped"
}
trap cleanup EXIT

echo "=== Checking aitelier image ==="
# `brig image ls` lists local images; format unspecified across brig
# versions so we grep the image name.
if ! brig image ls 2>/dev/null | grep -q "localhost/aitelier:latest\|aitelier:latest"; then
    echo "  aitelier image not found; building from docker/Dockerfile..."
    if brig image build --tag aitelier:latest --file docker/Dockerfile .; then
        echo "  ✓ built aitelier:latest"
    else
        echo "  ✗ \`brig image build\` failed."
        echo "    Build manually with brig (consult \`brig image build --help\`)"
        echo "    or use docker: docker build -f docker/Dockerfile -t aitelier:latest ."
        exit 1
    fi
else
    echo "  ✓ aitelier:latest already present"
fi

echo "=== Stopping any prior cell ==="
brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true

echo "=== Launching cell from $CELL_YAML ==="
if ! brig cell run --file "$CELL_YAML" -d; then
    echo "  ✗ \`brig cell run\` failed."
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
    echo "    Inspect: brig cell logs $CELL_NAME -f"
    exit 1
fi

echo "=== Running live test suite against $INGRESS_URL ==="
AITELIER_LIVE_URL="$INGRESS_URL" make test-live
