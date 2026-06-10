#!/bin/sh
# Entrypoint for the aitelier brig cell.
#
# scripts/start.sh provisions HOST services (docker compose, host SA
# install) which don't apply inside a cell. This entrypoint replaces it
# with the minimum in-cell setup: start SA in the background, then
# exec aitelier in the foreground so cell lifecycle = aitelier
# lifecycle.
#
# Postgres + LiteLLM are reached via *.host.brig routing — configure
# host_services for them in brig's network policy.

set -e

# SA is baked into the image at /usr/local/bin/sandbox-agent. Start it
# bound to localhost so only this cell's aitelier can reach it.
sandbox-agent server --host 127.0.0.1 --port 2468 --no-token \
    > /tmp/sandbox-agent.log 2>&1 &
SA_PID=$!
echo "sandbox-agent started (PID $SA_PID)"

# Wait briefly for SA to come up. If it's still not up after 10s, dump
# the log and continue — aitelier itself will report ProviderError on
# agent dispatches and the operator can investigate.
i=0
while [ $i -lt 20 ]; do
    if curl -sf http://127.0.0.1:2468/v1/agents >/dev/null 2>&1; then
        echo "sandbox-agent ready"
        break
    fi
    i=$((i + 1))
    sleep 0.5
done

# Run aitelier in foreground. Process exit = cell exit, which is what
# brig expects for non-detach lifecycle.
exec aitelier serve --host 0.0.0.0 --port 7777
