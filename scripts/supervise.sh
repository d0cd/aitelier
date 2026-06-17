#!/usr/bin/env bash
# launchd entrypoint: ensure infra is up, then run the aitelier service in the
# FOREGROUND so launchd can supervise it (KeepAlive restarts it on crash).
#
# `start.sh infra` is idempotent — it brings up Postgres/LiteLLM/Sandbox Agent
# and writes runs/.session.toml, then returns. We then `exec` the server, which
# replaces this shell, so launchd tracks the serve process directly. If the
# server dies, launchd re-runs this script (re-ensuring infra cheaply).
#
# Not meant to be run by hand — use `make start` for an interactive launch.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

./scripts/start.sh infra

# Record the PID we're about to become — `exec` preserves it, so this matches
# the live serve process and keeps `make status` / `make stop` accurate.
echo $$ > runs/.aitelier.pid

exec uv run --project core aitelier serve
