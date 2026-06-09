#!/usr/bin/env bash
# Regenerate Python models from JSON schemas.
#
# TypeScript types live at `sdks/typescript/src/types.ts` and are
# hand-maintained — the wire is snake_case but the SDK exposes camelCase
# (idiomatic JS), which `json-schema-to-typescript` can't produce
# directly. When a schema gains a field, update `types.ts` by hand.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_DIR="$REPO_ROOT/schemas/v1"

echo "=== Generating Python models ==="
# Prefer the workspace venv (installed via `uv sync`) over a system binary.
if [ -x "$REPO_ROOT/.venv/bin/datamodel-codegen" ]; then
    DMC="$REPO_ROOT/.venv/bin/datamodel-codegen"
elif command -v uv &>/dev/null; then
    DMC="uv run --no-sync datamodel-codegen"
elif command -v datamodel-codegen &>/dev/null; then
    DMC="datamodel-codegen"
else
    DMC=""
fi

if [ -n "$DMC" ]; then
    $DMC \
        --input "$SCHEMA_DIR" \
        --input-file-type jsonschema \
        --output "$REPO_ROOT/sdks/python/src/aitelier_client/_generated/models.py" \
        --output-model-type pydantic_v2.BaseModel \
        --target-python-version 3.11
    echo "Python models generated."
else
    echo "Warning: datamodel-codegen not found. Install with: uv sync (dev deps)"
fi

echo ""
echo "Done."
