#!/usr/bin/env bash
# Regenerate Python models from JSON schemas.
#
# datamodel-codegen 0.57 refuses a directory of schemas with a single-file
# output ("Modular references require an output directory"). Our schemas have
# no cross-file refs, so we merge them into one combined schema (each file's
# contents under $defs keyed by its title, with intra-file `#/$defs/...` refs
# rewritten to stay valid) and generate a single models.py from that — keeping
# the SDK's `from ._generated.models import ...` surface stable.
#
# TypeScript types live at `sdks/typescript/src/types.ts` and are
# hand-maintained — the wire is snake_case but the SDK exposes camelCase
# (idiomatic JS), which codegen can't produce directly. Update types.ts by hand.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_DIR="$REPO_ROOT/schemas/v1"
OUT="$REPO_ROOT/sdks/python/src/aitelier_client/_generated/models.py"

echo "=== Generating Python models ==="
if [ -x "$REPO_ROOT/.venv/bin/datamodel-codegen" ]; then
    DMC="$REPO_ROOT/.venv/bin/datamodel-codegen"
elif command -v uv &>/dev/null; then
    DMC="uv run --no-sync datamodel-codegen"
elif command -v datamodel-codegen &>/dev/null; then
    DMC="datamodel-codegen"
else
    echo "Warning: datamodel-codegen not found. Install with: uv sync (dev deps)"
    exit 0
fi

# Fixed basename inside a random dir: datamodel-codegen stamps the input
# filename into the generated header, so a stable name keeps regen output
# deterministic (no spurious diff when schemas are unchanged).
_GENDIR="$(mktemp -d)"
COMBINED="$_GENDIR/AitelierControlPlane.json"
trap 'rm -rf "$_GENDIR"' EXIT

python3 - "$SCHEMA_DIR" "$COMBINED" <<'PY'
import glob, json, os, sys

schema_dir, out = sys.argv[1], sys.argv[2]

def rewrite_refs(node, title):
    """Rewrite intra-file '#/$defs/X' refs to '#/$defs/<title>/$defs/X' so they
    resolve once the schema is nested under the combined root's $defs."""
    if isinstance(node, dict):
        return {
            k: (v.replace("#/$defs/", f"#/$defs/{title}/$defs/", 1)
                if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/")
                else rewrite_refs(v, title))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [rewrite_refs(x, title) for x in node]
    return node

defs = {}
for f in sorted(glob.glob(os.path.join(schema_dir, "*.json"))):
    s = json.load(open(f))
    title = s.get("title") or os.path.basename(f).split(".")[0]
    s = {k: v for k, v in s.items() if k not in ("$schema", "$id")}
    defs[title] = rewrite_refs(s, title)

json.dump({"$schema": "https://json-schema.org/draft/2020-12/schema",
           "title": "AitelierControlPlane", "$defs": defs},
          open(out, "w"), indent=2)
print(f"  merged {len(defs)} schemas")
PY

$DMC \
    --input "$COMBINED" \
    --input-file-type jsonschema \
    --output "$OUT" \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.11 \
    --use-standard-collections \
    --use-annotated \
    --field-constraints \
    --disable-timestamp

echo "Python models generated → $OUT"
echo ""
echo "Done."
