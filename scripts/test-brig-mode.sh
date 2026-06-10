#!/usr/bin/env bash
# Local end-to-end test for aitelier deployed as a brig cell.
#
# Mirrors test-docker-mode.sh but targets brig instead of compose. Skips
# cleanly if brig isn't installed — brig isn't on PyPI / homebrew, so
# this script is local-only by design.
#
# Uses brig 0.3.0 CLI surface (verified against the real binary):
#   brig run --file <yaml> -d            (top-level; NOT `brig cell run`)
#   brig cell stop / cell logs / cell list
#   brig image build --tag X --file Y <context>
#   brig system doctor
#
# What it does:
#   1. Verifies `brig` is on PATH and the VM is up.
#   2. Builds `aitelier:latest` via `brig image build`.
#   3. Launches the cell from docs/deploy/aitelier.cell.yaml.
#   4. Polls the cell's ingress for /v1/health readiness.
#   5. Runs the live test suite against aitelier-in-cell.
#   6. Tears down on exit (success or failure).
#
# PREREQS (none of these are automated — set them up once locally):
#   - `brig` on PATH globally. If you've installed brig but not exposed
#     it system-wide:
#         uv tool install -e ~/projects/brig
#     Or (simpler) just run via:
#         (cd ~/projects/brig && uv run brig <args>)
#   - VM running: `brig system up`
#   - aitelier-config + aitelier-secrets-toml + claude-credentials +
#     codex-credentials registered as brig secrets:
#         brig secrets add aitelier-config < aitelier.toml
#         brig secrets add aitelier-secrets-toml < aitelier.secrets.toml
#         brig secrets add claude-credentials < ~/.claude/.credentials.json
#         brig secrets add codex-credentials < ~/.codex/auth.json
#   - Postgres + LiteLLM reachable via `*.host.brig` routing:
#         brig policy set global --allow api.anthropic.com
#         # Add postgres to host_services if not already there
#         # (litellm + aitelier are typically pre-configured).
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
    echo "✗ brig not on PATH; skipping brig-mode e2e."
    echo "  Install system-wide:"
    echo "    uv tool install -e ~/projects/brig"
    echo "  Or run this script via:"
    echo "    PATH=\"\$HOME/projects/brig/.venv/bin:\$PATH\" ./scripts/test-brig-mode.sh"
    exit 0
fi

# brig 0.3.0 has no `system status`. Use `system doctor` to confirm
# the VM + warden are healthy. Fall back to limactl if doctor missing.
if ! brig system doctor --quick >/dev/null 2>&1; then
    echo "✗ \`brig system doctor --quick\` failed."
    echo "  Bring brig up:  brig system up"
    exit 1
fi

cleanup() {
    echo ""
    echo "=== Tearing down cell ==="
    brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true
    brig cell rm "$CELL_NAME" >/dev/null 2>&1 || true
    echo "  ✓ cell stopped + removed"
}
trap cleanup EXIT

echo "=== Building aitelier:latest ==="
# `brig image build` doesn't have a `list` / `ls` subcommand. Just
# build unconditionally — the build cache makes repeats cheap.
if ! brig image build --tag aitelier:latest --file docker/Dockerfile .; then
    echo "  ✗ image build failed."
    exit 1
fi

echo "=== Stopping any prior cell ==="
brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true
brig cell rm "$CELL_NAME" >/dev/null 2>&1 || true

echo "=== Launching cell from $CELL_YAML ==="
if ! brig run --file "$CELL_YAML" -d; then
    echo "  ✗ \`brig run\` failed."
    echo "    Check that secrets referenced in the yaml are registered:"
    echo "      brig secrets list"
    echo "    And that policy.allow covers api.anthropic.com etc.:"
    echo "      brig policy show"
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
