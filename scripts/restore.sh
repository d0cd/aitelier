#!/usr/bin/env bash
# Restore the aitelier Postgres database from a custom-format dump produced by
# scripts/backup.sh.
#
# DESTRUCTIVE: drops and recreates objects in the live database
# (--clean --if-exists). Stop the aitelier service first so it isn't writing
# during the restore.
#
#   ./scripts/restore.sh backups/aitelier-20260601-030000.dump

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="docker compose -f $REPO_ROOT/docker/docker-compose.yml"

file="${1:-}"
if [ -z "$file" ]; then
    echo "usage: scripts/restore.sh <backup.dump>" >&2
    echo "  recent backups:" >&2
    ls -1t "$REPO_ROOT"/backups/aitelier-*.dump 2>/dev/null | head -5 | sed 's/^/    /' >&2 || true
    exit 2
fi
if [ ! -s "$file" ]; then
    echo "✗ no such backup file (or empty): $file" >&2
    exit 1
fi

if ! $COMPOSE exec -T postgres pg_isready -U aitelier -d aitelier >/dev/null 2>&1; then
    echo "✗ Postgres not reachable — start it first (make start infra)" >&2
    exit 1
fi

echo "About to RESTORE into the live aitelier database from:"
echo "    $file"
echo "This DROPS and recreates existing objects (--clean --if-exists)."
echo "Stop the service first so it isn't writing: make service-uninstall"
echo "(or 'make stop service' if you're not running under launchd)."
printf "Type 'yes' to proceed: "
read -r confirm
if [ "$confirm" != "yes" ]; then
    echo "aborted."
    exit 1
fi

if $COMPOSE exec -T postgres pg_restore --clean --if-exists -U aitelier -d aitelier < "$file"; then
    echo "  ✓ restore complete"
else
    echo "  ! pg_restore exited non-zero — review output above" >&2
    echo "    (--clean warnings about absent objects are usually harmless)" >&2
    exit 1
fi
