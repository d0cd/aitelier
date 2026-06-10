#!/usr/bin/env bash
# Local end-to-end test for aitelier with Sandbox Agent isolated in a brig cell.
#
# Shape:
#   - brig cell hosts ONLY Sandbox Agent (docs/deploy/sandbox-agent.cell.yaml).
#   - aitelier itself runs on the host (just like in docker/host mode).
#   - aitelier reaches SA-in-cell via brig's ingress reverse proxy at
#     http://127.0.0.1:8443/sandbox-agent/v1/... with bearer auth.
#
# Mirrors test-docker-mode.sh but targets brig for SA instead of compose.
# Skips cleanly if brig isn't installed — brig isn't on PyPI / homebrew, so
# this script is local-only by design.
#
# What it does:
#   1. Verifies `brig` is on PATH and the VM is up.
#   2. Builds `sandbox-agent-brig:latest` via `brig image build`.
#   3. Launches the cell from docs/deploy/sandbox-agent.cell.yaml.
#   4. Probes the cell's ingress for /v1/agents readiness.
#   5. Starts aitelier on the host, pointed at the brig SA ingress URL.
#   6. Runs the live test suite against aitelier-on-host.
#   7. Tears down everything on exit.
#
# PREREQS (set up once locally):
#   - `brig` on PATH globally:
#         uv tool install -e ~/projects/brig
#   - VM running:
#         brig system up
#   - Brig secrets registered:
#         brig secrets add claude-credentials < ~/.claude/.credentials.json
#         brig secrets add codex-credentials < ~/.codex/auth.json
#         brig secrets add warden-ca-cert < <warden ca pem; see brig-feedback.md>
#         python3 -c 'import secrets;print(secrets.token_urlsafe(32))' \
#           | brig secrets add sandbox-agent-ingress-token
#   - Pre-baked claude binary on the host (the build path can't fetch it
#     fast enough through brig's mitmproxy; see Dockerfile + the
#     fail-fast message below).
#   - aitelier deps installed on host: `make install`.
#
# Run with:
#   ./scripts/test-brig-mode.sh
# Or:
#   make test-brig-mode-e2e

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

CELL_YAML="docs/deploy/sandbox-agent.cell.yaml"
CELL_NAME="sandbox-agent"
IMAGE_TAG="sandbox-agent-brig:latest"
DOCKERFILE="docker/sandbox-agent.brig.Dockerfile"

# Brig publishes the ingress reverse-proxy on host 127.0.0.1:8443.
# URLs are /{cell-name}/{path_prefix}/... with a Bearer token from the
# `<cell>-ingress-token` secret.
SA_INGRESS_URL="${BRIG_SA_URL:-http://127.0.0.1:8443/${CELL_NAME}}"
SA_INGRESS_TOKEN_FILE="${HOME}/.brig/secrets/${CELL_NAME}-ingress-token"

# Host-side aitelier — we start it as a subprocess with a brig-specific
# config that points [sandbox_agent] at the cell ingress. Standard host
# Postgres + LiteLLM apply (this is the same `make start` infra).
AITELIER_HOST="127.0.0.1"
AITELIER_PORT="${AITELIER_BRIGTEST_PORT:-7787}"
AITELIER_BASE_URL="http://${AITELIER_HOST}:${AITELIER_PORT}"
AITELIER_CONFIG="$(mktemp -t aitelier-brigtest.XXXXXX.toml)"
AITELIER_LOG="$(mktemp -t aitelier-brigtest-log.XXXXXX)"
AITELIER_PID=""

if ! command -v brig >/dev/null 2>&1; then
    echo "✗ brig not on PATH; skipping brig-mode e2e."
    echo "  Install system-wide:  uv tool install -e ~/projects/brig"
    exit 0
fi

if ! brig system doctor --quick >/dev/null 2>&1; then
    echo "✗ \`brig system doctor --quick\` failed."
    echo "  Bring brig up:  brig system up"
    exit 1
fi

cleanup() {
    echo ""
    echo "=== Tearing down ==="
    if [ -n "$AITELIER_PID" ] && kill -0 "$AITELIER_PID" 2>/dev/null; then
        kill "$AITELIER_PID" 2>/dev/null || true
        wait "$AITELIER_PID" 2>/dev/null || true
        echo "  ✓ aitelier subprocess stopped"
    fi
    brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true
    brig cell rm "$CELL_NAME" >/dev/null 2>&1 || true
    echo "  ✓ brig cell stopped + removed"
    rm -f "$AITELIER_CONFIG" "$AITELIER_LOG"
    [ -n "${AITELIER_CWD:-}" ] && rm -rf "$AITELIER_CWD"
}
trap cleanup EXIT

if [ ! -x docker/prebaked-agents/claude/claude ]; then
    echo "✗ docker/prebaked-agents/claude/claude missing."
    echo "  brig's mitmproxy + Lima networking is too slow for SA's 30s"
    echo "  install timeout, so we pre-fetch the claude binary on the host"
    echo "  and bake it into the image. Fetch it once:"
    echo ""
    echo "    VERSION=\$(curl -fsSL https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/latest)"
    echo "    mkdir -p docker/prebaked-agents/claude"
    echo "    curl -fL -o docker/prebaked-agents/claude/claude \\"
    echo "      \"https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/\${VERSION}/linux-arm64/claude\""
    echo "    chmod +x docker/prebaked-agents/claude/claude"
    echo ""
    echo "  See $DOCKERFILE for the rationale; the file is gitignored."
    exit 1
fi

echo "=== Building $IMAGE_TAG ==="
if ! brig image build --tag "$IMAGE_TAG" --file "$DOCKERFILE" .; then
    echo "  ✗ image build failed."
    exit 1
fi

echo "=== Stopping any prior cell ==="
brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true
brig cell rm "$CELL_NAME" >/dev/null 2>&1 || true

echo "=== Launching cell from $CELL_YAML ==="
if ! brig run --file "$CELL_YAML" -d; then
    echo "  ✗ \`brig run\` failed. Check:"
    echo "      brig secrets list   # claude-credentials, warden-ca-cert, etc."
    echo "      brig policy show $CELL_NAME"
    exit 1
fi

if [ ! -f "$SA_INGRESS_TOKEN_FILE" ]; then
    echo "  ✗ ingress token not registered. Generate + register:"
    echo "      python3 -c 'import secrets;print(secrets.token_urlsafe(32))' \\"
    echo "        | brig secrets add ${CELL_NAME}-ingress-token"
    exit 1
fi
SA_INGRESS_TOKEN="$(cat "$SA_INGRESS_TOKEN_FILE")"

echo "=== Waiting for SA ingress on $SA_INGRESS_URL ==="
for i in {1..60}; do
    if curl -sf -H "Authorization: Bearer $SA_INGRESS_TOKEN" \
            "$SA_INGRESS_URL/v1/agents" >/dev/null 2>&1; then
        echo "  ✓ SA reachable through ingress after ${i}s"
        break
    fi
    sleep 1
done

if ! curl -sf -H "Authorization: Bearer $SA_INGRESS_TOKEN" \
        "$SA_INGRESS_URL/v1/agents" >/dev/null 2>&1; then
    raw_code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $SA_INGRESS_TOKEN" \
        "$SA_INGRESS_URL/v1/agents" 2>/dev/null || echo "000")
    echo "  ✗ SA not responding through brig ingress after 60s (HTTP $raw_code)"
    echo "    Inspect: brig cell logs $CELL_NAME -f"
    echo "             limactl shell brig sudo podman logs warden | tail"
    exit 1
fi

echo "=== Starting aitelier on host (pointed at brig SA) ==="
# Minimal aitelier.toml: host Postgres + LiteLLM are reachable directly
# (we're on the host now, no warden in the path). Only [sandbox_agent]
# changes — point at the brig ingress.
cat > "$AITELIER_CONFIG" <<EOF
[database]
url = "postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier"

[litellm]
base_url = "http://127.0.0.1:4000"

[sandbox_agent]
base_url = "$SA_INGRESS_URL"
token = "$SA_INGRESS_TOKEN"

[service]
host = "$AITELIER_HOST"
port = $AITELIER_PORT
allow_loopback_webhooks = false
EOF

# Run from a clean cwd so we don't pick up the repo's `runs/.session.toml`
# overlay — that file gets written by `scripts/start.sh` for the host's
# normal aitelier and would override [sandbox_agent].base_url with the
# host's local SA port.
AITELIER_CWD="$(mktemp -d -t aitelier-brigtest.XXXXXX)"
(
    cd "$AITELIER_CWD" || exit 1
    uv run --project "$REPO_ROOT/core" aitelier --config "$AITELIER_CONFIG" serve \
        --host "$AITELIER_HOST" --port "$AITELIER_PORT"
) > "$AITELIER_LOG" 2>&1 &
AITELIER_PID=$!

for i in {1..30}; do
    if curl -sf "$AITELIER_BASE_URL/v1/health" >/dev/null 2>&1; then
        echo "  ✓ aitelier on host ready at $AITELIER_BASE_URL (pid $AITELIER_PID)"
        break
    fi
    if ! kill -0 "$AITELIER_PID" 2>/dev/null; then
        echo "  ✗ aitelier subprocess died. Last log lines:"
        tail -30 "$AITELIER_LOG"
        exit 1
    fi
    sleep 1
done

if ! curl -sf "$AITELIER_BASE_URL/v1/health" >/dev/null 2>&1; then
    echo "  ✗ aitelier on host not responding after 30s. Last log lines:"
    tail -30 "$AITELIER_LOG"
    exit 1
fi

echo "=== Running live test suite against $AITELIER_BASE_URL ==="
# AITELIER_LIVE_TMPDIR=/work because:
#   - The file is written by SA inside the cell, where `/work` is brig's
#     auto-mounted writable workspace.
#   - aitelier-on-host validates the path with `_has_symlinked_component`
#     against its OWN filesystem; `/work` doesn't exist on macOS, so the
#     check passes through (the validator allows non-existent paths).
#   - `/tmp` is a symlink on macOS host and would be rejected by
#     aitelier's symlink-component guard before it ever reaches SA.
AITELIER_LIVE_URL="$AITELIER_BASE_URL" \
AITELIER_LIVE_TMPDIR="/work" \
    make test-live
