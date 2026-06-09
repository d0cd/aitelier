#!/usr/bin/env bash
# Run contract tests for both SDKs against the shared test corpus.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Python SDK contract tests ==="
cd "$REPO_ROOT/sdks/python"
if ! command -v pytest &>/dev/null; then
    echo "Error: pytest not found. Run 'make install' (or 'uv sync' in sdks/python)." >&2
    exit 1
fi
pytest tests/ -v --tb=short

echo ""
echo "=== TypeScript SDK contract tests ==="
cd "$REPO_ROOT/sdks/typescript"
if [ ! -f node_modules/.bin/vitest ]; then
    echo "Error: vitest not installed in sdks/typescript/node_modules. Run 'make install' (or 'pnpm install' there)." >&2
    exit 1
fi
npx vitest run

echo ""
echo "Done."
