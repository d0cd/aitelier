#!/usr/bin/env bash
# Shared helpers sourced by start.sh, doctor.sh, status.sh.
# Pure functions only — no I/O at source time.

_port_in_use() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        nc -z 127.0.0.1 "$1" >/dev/null 2>&1
    fi
}

_port_holder() {
    # Print "<cmd> (PID <n>)" for the process listening on $1, or empty.
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fpcn 2>/dev/null | awk '
            /^p/ { pid=substr($0,2) }
            /^c/ { cmd=substr($0,2); printf "%s (PID %s)\n", cmd, pid; exit }
        '
    fi
}

_compose_owns_port() {
    # Returns 0 if one of OUR docker compose services already publishes this port.
    # Identifies "ours" via the com.docker.compose.project=docker label
    # (the dir-name aitelier's compose file lives in).
    docker ps --filter "publish=$1" --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
        | grep -qx docker
}

_other_container_on_port() {
    # Name of the (non-ours) docker container holding the port, or empty.
    docker ps --filter "publish=$1" --format '{{.Names}}' 2>/dev/null | head -1
}

_pick_free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}
