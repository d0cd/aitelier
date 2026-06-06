#!/usr/bin/env bash
# Run contract tests for both SDKs against the shared test corpus.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Python SDK contract tests ==="
cd "$REPO_ROOT/sdks/python"
if command -v pytest &>/dev/null; then
    pytest tests/ -v --tb=short
else
    echo "Warning: pytest not found"
fi

echo ""
echo "=== TypeScript SDK contract tests ==="
cd "$REPO_ROOT/sdks/typescript"
if [ -f node_modules/.bin/vitest ]; then
    npx vitest run
elif command -v npx &>/dev/null; then
    echo "Warning: vitest not installed. Run: pnpm install"
else
    echo "Warning: npx not found"
fi

echo ""
echo "Done."
