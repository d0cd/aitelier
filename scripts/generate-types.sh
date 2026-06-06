#!/usr/bin/env bash
# Regenerate types from JSON schemas for both SDKs.
# Python: datamodel-code-generator (JSON Schema → Pydantic)
# TypeScript: json-schema-to-typescript

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
echo "=== Generating TypeScript types ==="
if command -v json2ts &>/dev/null; then
    output_file="$REPO_ROOT/sdks/typescript/src/_generated/types.ts"
    echo '/* Generated from schemas/v1/ — do not hand-edit. */' > "$output_file"
    echo '' >> "$output_file"
    for schema in "$SCHEMA_DIR"/*.schema.json; do
        json2ts "$schema" >> "$output_file"
        echo '' >> "$output_file"
    done
    echo "TypeScript types generated."
else
    echo "Warning: json2ts not found. Install with: pnpm add -g json-schema-to-typescript"
fi

echo ""
echo "Done."
