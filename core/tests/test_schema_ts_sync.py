"""Drift guard binding schemas/v1 to the hand-maintained TypeScript SDK types.

The Python SDK regenerates models from schemas/v1 (scripts/generate-types.sh),
but sdks/typescript/src/types.ts is hand-maintained — the wire is snake_case and
the SDK exposes camelCase, which codegen can't produce. Nothing else catches a
schema field added to the Python models but never mirrored into TS.

This pins a hash of schemas/v1: when a schema changes, the hash diverges and
this test fails until you (a) update sdks/typescript/src/types.ts to match and
(b) update _EXPECTED_SCHEMAS_HASH below. Same spirit as
scripts/check-model-prices.py for pricing drift.
"""

from __future__ import annotations

import hashlib
import pathlib

# sha256 over each schemas/v1/*.json (name + bytes), files sorted by name.
_EXPECTED_SCHEMAS_HASH = "91710cb7a2fd53e2ddaf03ea417c04f360b087919b2941f169c2b310126946d2"

_SCHEMAS_DIR = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "v1"


def _schemas_hash() -> str:
    h = hashlib.sha256()
    for f in sorted(_SCHEMAS_DIR.glob("*.json")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def test_ts_types_in_sync_with_schemas():
    assert _schemas_hash() == _EXPECTED_SCHEMAS_HASH, (
        "schemas/v1 changed. The Python SDK regenerates from these, but the "
        "TypeScript types are hand-maintained — update "
        "sdks/typescript/src/types.ts to mirror the change, then set "
        "_EXPECTED_SCHEMAS_HASH in this test to the new hash."
    )
