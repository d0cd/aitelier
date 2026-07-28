#!/usr/bin/env bash
# Supervised entrypoint: ensure infra is up, then run the aitelier service in
# the FOREGROUND so a process supervisor (process-compose — see
# docs/deploy/process-compose.md) tracks the serve process directly and
# restarts it on crash.
#
# `start.sh infra` is idempotent — it brings up Postgres/LiteLLM/Sandbox Agent
# and writes runs/.session.toml, then returns. We then `exec` the server, which
# replaces this shell, so the supervisor tracks the serve process. If the
# server dies, the supervisor re-runs this script (re-ensuring infra cheaply).
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
