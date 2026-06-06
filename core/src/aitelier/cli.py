"""CLI entry point for aitelier."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="aitelier",
        description="AI task toolkit — fan-out dispatch across providers",
    )
    sub = parser.add_subparsers(dest="command")

    # Task execution (aitelier <task> ...)
    run_parser = sub.add_parser("run", help="Run a named task")
    run_parser.add_argument("task", help="Task name (audit, research, lint, implement, summarize)")
    run_parser.add_argument("args", nargs="*", help="Task arguments (workspace path or content)")
    run_parser.add_argument(
        "--fanout", action="store_true",
        help="Fan out across all preferred providers",
    )
    run_parser.add_argument("--providers", nargs="+", help="Override providers for fan-out")
    run_parser.add_argument(
        "--max-concurrent", type=int, default=4,
        help="Max concurrent fan-out (default: 4)",
    )
    run_parser.add_argument("--timeout", type=int, help="Timeout in seconds")
    run_parser.add_argument("--focus", default=None, help="Focus area (for audit/lint)")
    run_parser.add_argument("--format", default=None, help="Output format (for summarize)")
    run_parser.add_argument("--workspace-mode", choices=["copy", "in_place"], default="copy")

    # Serve
    serve_parser = sub.add_parser("serve", help="Start the HTTP service")
    serve_parser.add_argument("--port", type=int, default=7777, help="Port (default: 7777)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")

    # List tasks
    sub.add_parser("list", help="List available tasks")

    # Show runs
    runs_parser = sub.add_parser("runs", help="Show recent run records")
    runs_parser.add_argument("--last", type=int, default=10, help="Number of runs to show")

    # Compare
    compare_parser = sub.add_parser("compare", help="Show fan-out comparison")
    compare_parser.add_argument("run_id", help="Run ID to compare")

    # Status
    sub.add_parser("status", help="Check service and infrastructure status")

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

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "serve":
        _cmd_serve(args)
    elif args.command == "list":
        _cmd_list()
    elif args.command == "runs":
        _cmd_runs(args)
    elif args.command == "compare":
        _cmd_compare(args)
    elif args.command == "status":
        _cmd_status()
    elif args.command == "traces":
        _cmd_traces(args)


async def _cmd_run(args: argparse.Namespace) -> None:
    from aitelier.observability import setup_langfuse

    setup_langfuse()

    # Build task spec from name + args
    task = _build_task_spec(args)

    if args.fanout:
        from aitelier.fanout import fanout

        providers = args.providers or task.get("preferred_providers")
        results = await fanout(task, providers=providers, max_concurrent=args.max_concurrent)
        for r in results:
            _print_result(r)
    else:
        from aitelier.runner import execute

        result = await execute(task)
        _print_result(result)


def _build_task_spec(args: argparse.Namespace) -> dict:
    # Import task definitions
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from tasks import get_task

    task_args = args.args or []
    kwargs = {}

    # Route positional args based on task type
    if args.task in ("audit", "lint", "implement"):
        if task_args:
            kwargs["workspace"] = task_args[0]
        else:
            kwargs["workspace"] = "."
        if args.task == "implement" and len(task_args) > 1:
            kwargs["description"] = " ".join(task_args[1:])
        elif args.task == "implement":
            print("Error: implement requires a description", file=sys.stderr)
            sys.exit(1)
    elif args.task == "research":
        kwargs["topic"] = " ".join(task_args) if task_args else "general"
    elif args.task == "summarize":
        kwargs["content"] = " ".join(task_args) if task_args else sys.stdin.read()

    if args.focus and args.task in ("audit", "lint"):
        kwargs["focus"] = args.focus
    if args.format and args.task == "summarize":
        kwargs["format"] = args.format
    if hasattr(args, "workspace_mode") and args.task in ("audit", "lint", "implement"):
        kwargs["workspace_mode"] = args.workspace_mode

    task = get_task(args.task, **kwargs)

    if args.timeout:
        task["timeout"] = args.timeout

    return task


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from aitelier.config import get_config
    from aitelier.server import app

    cfg = get_config().service
    host = args.host if args.host != "127.0.0.1" else cfg.host
    port = args.port if args.port != 7777 else cfg.port

    uvicorn.run(app, host=host, port=port)


def _cmd_list() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from tasks import list_tasks

    tasks = list_tasks()
    if not tasks:
        print("No tasks found.")
        return
    for name in tasks:
        print(f"  {name}")


def _cmd_runs(args: argparse.Namespace) -> None:
    runs_dir = Path("runs")
    if not runs_dir.exists():
        print("No runs yet.")
        return

    run_dirs = sorted(runs_dir.iterdir(), reverse=True)
    for rd in run_dirs[: args.last]:
        manifest_path = rd / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            results_summary = ", ".join(
                f"{r['provider']}={r['status']}" for r in manifest.get("results", [])
            )
            print(f"  {rd.name}  [{results_summary}]")
        else:
            print(f"  {rd.name}  [no manifest]")


def _cmd_compare(args: argparse.Namespace) -> None:
    compare_path = Path("runs") / args.run_id / "compare.md"
    if not compare_path.exists():
        print(f"No comparison found for run {args.run_id}", file=sys.stderr)
        sys.exit(1)
    print(compare_path.read_text())


def _cmd_traces(args: argparse.Namespace) -> None:
    """List traces (with filters), or show one in detail when trace_id is given."""
    from aitelier.traces import get_trace, recent_traces

    if args.trace_id:
        trace = get_trace(args.trace_id)
        if not trace:
            print(f"Trace not found: {args.trace_id}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(trace, indent=2, default=str))
            return
        _print_trace_detail(trace)
        return

    traces = recent_traces(
        since=args.since,
        trace_tag=args.tag,
        status=args.status,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(traces, indent=2, default=str))
        return

    if not traces:
        print("No traces match.")
        return

    # Compact table
    print(f"{'trace_id':<40} {'kind':<10} {'model':<18} {'status':<6} {'tokens':>8}  tag")
    print("-" * 100)
    for t in traces:
        total = t.get("total_tokens") or 0
        tag = t.get("trace_tag") or ""
        print(
            f"{(t['trace_id'] or '')[:40]:<40} "
            f"{(t.get('kind') or '')[:10]:<10} "
            f"{(t.get('model') or '')[:18]:<18} "
            f"{(t.get('status') or '')[:6]:<6} "
            f"{total:>8}  "
            f"{tag}"
        )


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


def _cmd_status() -> None:
    import shutil

    import httpx

    from aitelier.config import get_config
    from aitelier.traces import recent_traces

    cfg = get_config()

    print("aitelier status\n")

    # --- Services ---
    print("Services:")
    # LiteLLM proxy
    try:
        resp = httpx.get(
            f"{cfg.litellm.base_url}/health",
            headers={"Authorization": f"Bearer {cfg.litellm.api_key}"},
            timeout=3,
        )
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
    print("\nModels (via LiteLLM proxy):")
    try:
        resp = httpx.get(
            f"{cfg.litellm.base_url}/models",
            headers={"Authorization": f"Bearer {cfg.litellm.api_key}"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("id", "?") for m in data.get("data", [])]
            for m in sorted(models):
                print(f"  {m}")
            if not models:
                print("  (none)")
        else:
            print(f"  (couldn't list — HTTP {resp.status_code})")
    except Exception:
        print("  (proxy unreachable)")

    # --- Traces ---
    print("\nTraces:")
    try:
        traces = recent_traces(limit=5)
        print(f"  {len(traces)} recent (showing last 5)")
        for t in traces:
            tag = f" [{t['trace_tag']}]" if t.get("trace_tag") else ""
            print(f"    {t['trace_id']}  {t['status']}{tag}")
        if not traces:
            print("  (none)")
    except Exception:
        print("  (no trace store)")

    # --- Config ---
    print(f"\nConfig: {cfg.runs_dir}/")


def _print_result(result: dict) -> None:
    status = result["status"]
    provider = result["provider"]
    duration = result["duration_s"]
    cost = result.get("cost_usd")

    header = f"[{provider}] {status} ({duration}s"
    if cost is not None:
        header += f", ${cost:.4f}"
    header += ")"
    print(header)

    if status == "error":
        print(f"  Error: {result.get('error_type')}: {result.get('error_msg')}")
    else:
        print(result.get("text", ""))
    print()


if __name__ == "__main__":
    main()
