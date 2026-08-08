#!/bin/sh
# Docker-mode Sandbox Agent credential boundary. Compose mounts the token
# read-only; keep it out of image layers and export it only to SA's process
# tree. An empty secret deliberately falls back to the mounted Claude login.

set -eu

if [ -s /run/secrets/claude-oauth-token ]; then
    CLAUDE_CODE_OAUTH_TOKEN="$(cat /run/secrets/claude-oauth-token)"
    export CLAUDE_CODE_OAUTH_TOKEN
fi

exec "$@"
