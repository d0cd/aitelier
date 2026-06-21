#!/usr/bin/env python
"""Backfill OTLP spans from aitelier's durable run record.

aitelier records every run + its event timeline durably in Postgres; the
OTLP export is an optional, disposable *view*. This script replays runs
from the store into the configured `[otel]` endpoint, so you can populate
a fresh tracing backend (Jaeger, Tempo, Phoenix, …) from the system of
record at any time — tear the backend down, spin a new one up, backfill.

It reuses the exact span-emission path used at finalize time
(`otel.record_inference_span`), so a backfilled span tree is identical to
one emitted live: a root `gen_ai.*` span per run plus an `execute_tool`
child span per tool call, with `trace_id == run_id` for hex-id runs.

Requires `[otel] enabled = true` + an `endpoint` in aitelier.toml, and the
`aitelier[otel]` extra installed. Filters mirror `GET /v1/runs`.

Notes:
  - Trace ids are stable (== run_id) only for the 32-hex run ids minted
    since the W3C-trace-id change; legacy timestamp-style ids aren't valid
    trace ids, so each replay assigns them a fresh random trace id.
  - Span ids are freshly generated per run, so re-backfilling into a
    NON-empty backend appends duplicate spans. Backfill into a fresh
    backend (the intended "rebuild from the record" workflow).

Usage (from repo root):
  uv run --project core python scripts/otel-backfill.py
  uv run --project core python scripts/otel-backfill.py --since 2026-06-01 --kind agent
  uv run --project core python scripts/otel-backfill.py --trace-tag audit --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def _run(args: argparse.Namespace) -> int:
    from aitelier import otel
    from aitelier.config import get_config
    from aitelier.storage import RunFilter, get_store

    cfg = get_config().otel
    if not cfg.enabled or not cfg.endpoint:
        raise SystemExit(
            "[otel] must be enabled with an endpoint set in aitelier.toml "
            "(this script exports to that endpoint)."
        )

    otel.init_tracer_provider()
    if otel._tracer is None:
        raise SystemExit(
            "OTel tracer failed to initialize — install the extra: "
            "uv pip install 'aitelier[otel]'."
        )

    store = await get_store()
    runs = await store.list_runs(RunFilter(
        since=_iso(args.since), until=_iso(args.until),
        trace_tag=args.trace_tag, kind=args.kind, state=args.state,
        limit=args.limit,
    ))

    print(f"backfilling {len(runs)} run(s) → {cfg.endpoint} …")
    emitted = 0
    for run in runs:
        events = await store.list_events(run.run_id, limit=5000)
        otel.record_inference_span(
            operation="embeddings" if run.kind == "embed" else "chat",
            request_body=run.request_body,
            # Mirror the live path: pass the result only on success so an
            # error run records its error_type instead of empty usage.
            result=run.result if run.status != "error" else None,
            run_id=run.run_id, events=events,
            started_at=run.started_at, ended_at=run.ended_at,
            error_type=run.error_type, error_msg=run.error_msg,
        )
        emitted += 1
        if emitted % 200 == 0:
            print(f"  …{emitted}/{len(runs)}")

    # Flush the BatchSpanProcessor before exit, or the last batch is lost.
    otel.shutdown_tracer_provider()
    print(f"done — emitted {emitted} run trace(s).")
    return emitted


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--since", help="ISO-8601 lower bound on started_at")
    p.add_argument("--until", help="ISO-8601 upper bound on started_at")
    p.add_argument("--trace-tag", dest="trace_tag", help="filter by trace_tag")
    p.add_argument("--kind", choices=["complete", "embed", "agent"],
                   help="filter by run kind")
    p.add_argument("--state", help="filter by lifecycle state "
                                    "(completed/failed/cancelled/…)")
    p.add_argument("--limit", type=int, default=10000,
                   help="max runs to backfill (default 10000)")
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
