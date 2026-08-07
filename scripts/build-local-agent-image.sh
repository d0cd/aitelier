#!/usr/bin/env bash
# Build and optionally activate the Brig Sandbox Agent image from local dirty
# Sandbox Agent and codex-acp worktrees. No package publication is involved.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_HOME_DIR="$(cd ~ && pwd -P)"
SANDBOX_AGENT_SOURCE="${LOCAL_SANDBOX_AGENT_SOURCE:-$USER_HOME_DIR/tools/sandbox-agent}"
CODEX_ACP_SOURCE="${LOCAL_CODEX_ACP_SOURCE:-$USER_HOME_DIR/tools/codex-acp}"
IMAGE_TAG="${LOCAL_BRIG_IMAGE_TAG:-sandbox-agent-brig:latest}"
CELL_NAME="sandbox-agent"
CELL_YAML="$REPO_ROOT/docs/deploy/sandbox-agent.cell.yaml"
DOCKERFILE_REL="aitelier/docker/sandbox-agent.brig.local.Dockerfile"
BUILD_ONLY=0

case "${1:-}" in
    --build-only)
        BUILD_ONLY=1
        shift
        ;;
    -h|--help)
        echo "usage: $0 [--build-only]"
        exit 0
        ;;
esac
if [ "$#" -ne 0 ]; then
    echo "usage: $0 [--build-only]" >&2
    exit 2
fi

for required in \
    "$SANDBOX_AGENT_SOURCE/Cargo.toml" \
    "$CODEX_ACP_SOURCE/Cargo.toml" \
    "$REPO_ROOT/docker/sandbox-agent.brig.local.Dockerfile" \
    "$REPO_ROOT/docker/prebaked-agents/claude/claude"; do
    if [ ! -e "$required" ]; then
        echo "missing required local build input: $required" >&2
        exit 1
    fi
done

for command_name in brig rsync curl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "required command is not installed: $command_name" >&2
        exit 1
    fi
done

TASK_TMP_ROOT="${TMPDIR:-/tmp}"
BUILD_CONTEXT="$(mktemp -d "$TASK_TMP_ROOT/aitelier-local-agents.XXXXXX")"

# shellcheck disable=SC2329  # Invoked indirectly by the EXIT trap below.
cleanup() {
    local resolved_context resolved_tmp
    [ -n "${BUILD_CONTEXT:-}" ] || return 0
    [ ! -L "$BUILD_CONTEXT" ] || {
        echo "refusing to remove symlinked build context: $BUILD_CONTEXT" >&2
        return 1
    }
    resolved_context="$(cd "$BUILD_CONTEXT" 2>/dev/null && pwd -P)" || return 1
    resolved_tmp="$(cd "$TASK_TMP_ROOT" && pwd -P)" || return 1
    case "$resolved_context" in
        "$resolved_tmp"/aitelier-local-agents.*)
            rm -rf -- "$resolved_context"
            ;;
        *)
            echo "refusing to remove unexpected build context: $resolved_context" >&2
            return 1
            ;;
    esac
}
trap cleanup EXIT

mkdir -p \
    "$BUILD_CONTEXT/aitelier/docker/prebaked-agents/claude" \
    "$BUILD_CONTEXT/aitelier/scripts" \
    "$BUILD_CONTEXT/sandbox-agent" \
    "$BUILD_CONTEXT/codex-acp"

echo "=== Staging local worktrees ==="
echo "  Sandbox Agent: $SANDBOX_AGENT_SOURCE"
echo "  codex-acp:     $CODEX_ACP_SOURCE"
echo "  Build context: $BUILD_CONTEXT"

rsync -a \
    --exclude .git --exclude target --exclude node_modules \
    "$SANDBOX_AGENT_SOURCE/" "$BUILD_CONTEXT/sandbox-agent/"
rsync -a \
    --exclude .git --exclude target --exclude node_modules \
    "$CODEX_ACP_SOURCE/" "$BUILD_CONTEXT/codex-acp/"
rsync -a "$REPO_ROOT/docker/sandbox-agent.brig.local.Dockerfile" \
    "$BUILD_CONTEXT/aitelier/docker/"
rsync -a "$REPO_ROOT/docker/prebaked-agents/claude/claude" \
    "$BUILD_CONTEXT/aitelier/docker/prebaked-agents/claude/"
rsync -a "$REPO_ROOT/scripts/cell-entrypoint.sh" \
    "$BUILD_CONTEXT/aitelier/scripts/"

if ! brig system doctor --quick >/dev/null 2>&1; then
    echo "=== Starting Brig VM ==="
    brig system up
fi

echo "=== Building local-source image: $IMAGE_TAG ==="
brig image build \
    --tag "$IMAGE_TAG" \
    --file "$DOCKERFILE_REL" \
    "$BUILD_CONTEXT"

if [ "$BUILD_ONLY" -eq 1 ]; then
    echo "=== Local image built; cell left unchanged ==="
    exit 0
fi

if [ "$CELL_NAME" != "sandbox-agent" ]; then
    echo "refusing to replace unexpected Brig cell: $CELL_NAME" >&2
    exit 1
fi

TOKEN_FILE="$USER_HOME_DIR/.brig/secrets/$CELL_NAME-ingress-token"
if [ ! -f "$TOKEN_FILE" ] || [ -L "$TOKEN_FILE" ]; then
    echo "missing or unsafe Brig ingress token: $TOKEN_FILE" >&2
    exit 1
fi
IFS= read -r INGRESS_TOKEN < "$TOKEN_FILE"
if [ -z "$INGRESS_TOKEN" ]; then
    echo "Brig ingress token is empty: $TOKEN_FILE" >&2
    exit 1
fi

echo "=== Replacing Brig cell: $CELL_NAME ==="
brig cell stop "$CELL_NAME" >/dev/null 2>&1 || true
brig cell rm "$CELL_NAME" >/dev/null 2>&1 || true
brig run --file "$CELL_YAML" -d

INGRESS_URL="${BRIG_SA_URL:-http://127.0.0.1:8443/$CELL_NAME}"

echo "=== Waiting for local Sandbox Agent ==="
for _ in {1..60}; do
    if curl -sf --max-time 2 \
        -H "Authorization: Bearer $INGRESS_TOKEN" \
        "$INGRESS_URL/v1/agents?config=true" >/dev/null 2>&1; then
        echo "  Local Sandbox Agent is ready at $INGRESS_URL"
        exit 0
    fi
    sleep 1
done

echo "local Sandbox Agent did not become ready after 60 seconds" >&2
echo "inspect with: brig cell logs $CELL_NAME -f" >&2
exit 1
