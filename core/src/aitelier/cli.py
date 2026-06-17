"""CLI entry point for aitelier."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="aitelier",
        description="aitelier — HTTP service for AI inference + agent delegation",
    )
    parser.add_argument(
        "--config", type=Path, default=None, metavar="PATH",
        help="Path to aitelier.toml (default: ./aitelier.toml then ~/.config/aitelier/config.toml)",
    )
    sub = parser.add_subparsers(dest="command")

    # Serve
    serve_parser = sub.add_parser("serve", help="Start the HTTP service")
    serve_parser.add_argument("--port", type=int, default=7777, help="Port (default: 7777)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")

    # Show runs
    runs_parser = sub.add_parser("runs", help="Show recent run records")
    runs_parser.add_argument("--last", type=int, default=10, help="Number of runs to show")

    # Doctor (delegates to scripts/doctor.sh in the repo)
    sub.add_parser("doctor", help="Run preflight diagnostics")

    # Status
    status_parser = sub.add_parser("status", help="Check service and infrastructure status")
    status_parser.add_argument(
        "--all-models", action="store_true",
        help="List every model the LiteLLM proxy reports (otherwise show curated aliases)",
    )

    # Traces (list / show single)
    traces_parser = sub.add_parser("traces", help="Query the trace store")
    traces_parser.add_argument("trace_id", nargs="?", default=None,
                                help="If given, show full detail for this trace")
    traces_parser.add_argument("--tag", default=None, help="Filter by trace_tag")
    traces_parser.add_argument("--status", default=None, choices=["ok", "error"],
                                help="Filter by status")
    traces_parser.add_argument("--since", default=None,
                                help="ISO timestamp (e.g. 2026-05-01T00:00:00Z)")
    traces_parser.add_argument("--limit", type=int, default=20,
                                help="Max rows to show (default: 20)")
    traces_parser.add_argument("--json", action="store_true",
                                help="Emit JSON instead of a human table")

    args = parser.parse_args(argv)

    # Load config (with optional --config override) into the singleton before
    # any subcommand runs. This is the only entry point that should ever set
    # the config — every other call site reads via get_config().
    if args.config is not None:
        from aitelier.config import load_config, set_config
        set_config(load_config(args.config))

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "runs":
        _cmd_runs(args)
    elif args.command == "doctor":
        _cmd_doctor()
    elif args.command == "status":
        _cmd_status(all_models=args.all_models)
    elif args.command == "traces":
        _cmd_traces(args)


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from aitelier.config import get_config
    from aitelier.server import app

    cfg = get_config().service
    host = args.host if args.host != "127.0.0.1" else cfg.host
    port = args.port if args.port != 7777 else cfg.port

    uvicorn.run(app, host=host, port=port)


def _cmd_runs(args: argparse.Namespace) -> None:
    runs_dir = Path("runs")
    if not runs_dir.exists():
        print("No runs yet.")
        return

    run_dirs = sorted(runs_dir.iterdir(), reverse=True)
    for rd in run_dirs[: args.last]:
        manifest_path = rd / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except json.JSONDecodeError:
                # One corrupt manifest shouldn't abort the whole listing —
                # matches the JSONDecodeError guard in endpoints/runs.py.
                print(f"  {rd.name}  [corrupt manifest]")
                continue
            results_summary = ", ".join(
                f"{r['provider']}={r['status']}" for r in manifest.get("results", [])
            )
            print(f"  {rd.name}  [{results_summary}]")
        else:
            print(f"  {rd.name}  [no manifest]")


def _cmd_traces(args: argparse.Namespace) -> None:
    """List traces (with filters), or show one in detail when trace_id is given."""
    import asyncio
    from datetime import datetime as _dt

    from aitelier.storage import RunFilter, get_store

    async def _go():
        store = await get_store()
        if args.trace_id:
            run = await store.get_run(args.trace_id)
            return ("single", run)
        since_dt = _dt.fromisoformat(args.since) if args.since else None
        runs = await store.list_runs(RunFilter(
            since=since_dt, trace_tag=args.tag, limit=args.limit,
        ))
        if args.status:
            runs = [r for r in runs if r.status == args.status]
        return ("list", runs)

    mode, payload = asyncio.run(_go())

    if mode == "single":
        run = payload
        if not run:
            print(f"Trace not found: {args.trace_id}", file=sys.stderr)
            sys.exit(1)
        trace = _run_to_dict(run)
        if args.json:
            print(json.dumps(trace, indent=2, default=str))
            return
        _print_trace_detail(trace)
        return

    runs = payload
    if args.json:
        print(json.dumps([_run_to_dict(r) for r in runs], indent=2, default=str))
        return

    if not runs:
        print("No traces match.")
        return

    print(f"{'trace_id':<40} {'kind':<10} {'model':<18} {'status':<6} {'tokens':>8}  tag")
    print("-" * 100)
    for r in runs:
        tag = r.trace_tag or ""
        print(
            f"{(r.run_id or '')[:40]:<40} "
            f"{(r.kind or '')[:10]:<10} "
            f"{(r.model or '')[:18]:<18} "
            f"{(r.status or '')[:6]:<6} "
            f"{(r.total_tokens or 0):>8}  "
            f"{tag}"
        )


def _run_to_dict(run) -> dict:
    """Run dataclass → TraceRecord-shaped dict for CLI JSON output."""
    return {
        "trace_id": run.run_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "model": run.model,
        "kind": run.kind,
        "finish_reason": run.finish_reason,
        "tool_call_count": run.tool_call_count,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "cost_usd": run.cost_usd,
        "system_prompt_hash": run.system_prompt_hash,
        "trace_tag": run.trace_tag,
        "status": run.status,
        "error_type": run.error_type,
        "error_msg": run.error_msg,
        "metadata": run.metadata,
    }


def _print_trace_detail(t: dict) -> None:
    print(f"trace_id:           {t.get('trace_id')}")
    print(f"started_at:         {t.get('started_at')}")
    print(f"ended_at:           {t.get('ended_at')}")
    print(f"kind:               {t.get('kind')}")
    print(f"model:              {t.get('model')}")
    print(f"status:             {t.get('status')}")
    print(f"finish_reason:      {t.get('finish_reason')}")
    print(f"tool_call_count:    {t.get('tool_call_count')}")
    print(f"input_tokens:       {t.get('input_tokens')}")
    print(f"output_tokens:      {t.get('output_tokens')}")
    print(f"total_tokens:       {t.get('total_tokens')}")
    print(f"cost_usd:           {t.get('cost_usd')}")
    print(f"trace_tag:          {t.get('trace_tag')}")
    if t.get("error_type"):
        print(f"error_type:         {t.get('error_type')}")
        print(f"error_msg:          {t.get('error_msg')}")
    md = t.get("metadata")
    if md:
        # metadata is stored as JSON text in the DB; show it pretty
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except json.JSONDecodeError:
                pass
        md_str = md if isinstance(md, str) else json.dumps(md, default=str)
        print(f"metadata:           {md_str}")


def _cmd_doctor() -> None:
    """Run preflight diagnostics by execing scripts/doctor.sh.

    Locates the script relative to the package source, which works for
    editable installs (`uv tool install --editable ./core`). For non-editable
    PyPI installs we'd need to ship doctor.sh as package data — TODO once
    we publish.
    """
    import os

    # core/src/aitelier/cli.py → repo root is parents[3]
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "doctor.sh"
    if not script.exists():
        print(
            f"doctor script not found at {script}.\n"
            "This subcommand currently requires an editable install from a "
            "checkout of the aitelier repo. PyPI install support is pending.",
            file=sys.stderr,
        )
        sys.exit(2)
    os.execvp("bash", ["bash", str(script)])


def _cmd_status(all_models: bool = False) -> None:
    import asyncio
    import shutil

    import httpx

    from aitelier.config import get_config
    from aitelier.storage import RunFilter, get_store

    cfg = get_config()

    print("aitelier status\n")

    # --- Services ---
    print("Services:")
    # LiteLLM proxy — use /health/liveness (no auth, no upstream-provider probe).
    # /health is a deep check that hits every configured backend; an OpenAI 429
    # would flip this to ✗ even though the proxy itself is fine.
    try:
        resp = httpx.get(f"{cfg.litellm.base_url}/health/liveness", timeout=3)
        if resp.status_code == 200:
            print(f"  ✓ LiteLLM proxy    {cfg.litellm.base_url}")
        else:
            print(f"  ✗ LiteLLM proxy    {cfg.litellm.base_url}  (HTTP {resp.status_code})")
    except Exception:
        print(f"  ✗ LiteLLM proxy    {cfg.litellm.base_url}  (unreachable)")

    # aitelier service
    try:
        url = f"http://{cfg.service.host}:{cfg.service.port}"
        resp = httpx.get(f"{url}/v1/health", timeout=3)
        if resp.status_code == 200:
            print(f"  ✓ aitelier service {url}")
        else:
            print(f"  ✗ aitelier service {url}  (HTTP {resp.status_code})")
    except Exception:
        print(f"  ✗ aitelier service http://{cfg.service.host}:{cfg.service.port}  (not running)")

    # --- Agents ---
    print("\nAgents:")
    for name, binary in [("claude-code", "claude"), ("codex", "codex")]:
        path = shutil.which(binary)
        if path:
            print(f"  ✓ {name:<14} {path}")
        else:
            print(f"  ✗ {name:<14} not found")

    # --- Credentials ---
    print("\nCredentials:")
    from pathlib import Path as P
    claude_creds = P.home() / ".claude" / ".credentials.json"
    codex_creds = P.home() / ".codex" / "auth.json"

    if claude_creds.exists():
        try:
            import time
            data = json.loads(claude_creds.read_text())
            expires = data.get("claudeAiOauth", {}).get("expiresAt", 0)
            if expires and expires > time.time() * 1000:
                remaining_h = (expires - time.time() * 1000) / 3_600_000
                print(f"  ✓ Claude OAuth     valid ({remaining_h:.0f}h remaining)")
            elif expires:
                print("  ✗ Claude OAuth     expired — run 'claude login'")
            else:
                print("  ? Claude OAuth     no expiry info")
        except Exception:
            print("  ? Claude OAuth     failed to read")
    else:
        print("  ✗ Claude OAuth     not logged in — run 'claude login'")

    if codex_creds.exists():
        print(f"  ✓ Codex OAuth      {codex_creds}")
    else:
        print("  - Codex OAuth      not logged in (optional)")

    # --- Models ---
    # LiteLLM's /models reports every backend discovery returns — 200+ entries
    # of OpenAI / Anthropic SKU noise. By default show the curated aliases
    # that this repo's tasks/SDKs actually reference, plus pass-through
    # markers. Use --all-models for the full dump.
    print("\nModels (via LiteLLM proxy):")
    try:
        resp = httpx.get(
            f"{cfg.litellm.base_url}/models",
            headers={"Authorization": f"Bearer {cfg.litellm.api_key}"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = sorted(m.get("id", "?") for m in data.get("data", []))
            if all_models:
                shown = models
            else:
                # Curated aliases (the ones in CLAUDE.md "Available models").
                aliases = {"local", "claude-sonnet", "claude-haiku", "nomic-embed-text"}
                shown = [m for m in models if m in aliases]
                # Plus pass-through wildcard markers, if registered.
                shown += [m for m in models if m in {"anthropic/*", "openai/*", "ollama/*"}]
            for m in shown:
                print(f"  {m}")
            if not shown:
                print("  (none)")
            hidden = len(models) - len(shown)
            if not all_models and hidden > 0:
                print(f"  … {hidden} more (run `aitelier status --all-models` to list)")
        else:
            print(f"  (couldn't list — HTTP {resp.status_code})")
    except Exception:
        print("  (proxy unreachable)")

    # --- Traces ---
    print("\nTraces:")

    async def _recent():
        store = await get_store()
        return await store.list_runs(RunFilter(limit=5))

    try:
        runs = asyncio.run(_recent())
        print(f"  {len(runs)} recent (showing last 5)")
        for r in runs:
            tag = f" [{r.trace_tag}]" if r.trace_tag else ""
            print(f"    {r.run_id}  {r.status or '-'}{tag}")
        if not runs:
            print("  (none)")
    except Exception:
        print("  (no trace store)")

    # --- Config ---
    print("\nConfig:")
    print(f"  runs_dir            {cfg.runs_dir}")
    print(f"  database.url        {cfg.database.url or '(unset — InMemoryStore)'}")
    print(f"  sandbox_agent       {cfg.sandbox_agent.base_url}")


if __name__ == "__main__":
    main()
