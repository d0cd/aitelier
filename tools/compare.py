"""Ad-hoc comparison utility for fan-out results."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def compare_run(run_id: str, runs_dir: str = "runs") -> None:
    """Print a comparison of fan-out results for a given run."""
    run_dir = Path(runs_dir) / run_id
    if not run_dir.exists():
        print(f"Run not found: {run_id}", file=sys.stderr)
        sys.exit(1)

    compare_path = run_dir / "compare.md"
    if compare_path.exists():
        print(compare_path.read_text())
        return

    # Build comparison from individual results
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest for run: {run_id}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())
    for entry in manifest.get("results", []):
        provider = entry["provider"]
        result_path = run_dir / provider / "result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            status = result["status"]
            duration = result["duration_s"]
            print(f"## {provider} [{status}] ({duration}s)")
            print(result.get("text", "")[:3000])
            print("\n---\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/compare.py <run-id>", file=sys.stderr)
        sys.exit(1)
    compare_run(sys.argv[1])
