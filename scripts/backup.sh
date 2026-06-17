#!/usr/bin/env bash
# Back up the aitelier Postgres database to a timestamped custom-format dump.
#
# The dump runs *inside* the postgres container (pg_dump 16), so there's no
# host client/server version mismatch to worry about. Keeps the most recent
# $AITELIER_BACKUP_RETAIN dumps (default 14) under backups/.
#
#   ./scripts/backup.sh            # one dump now, prune old ones
#   AITELIER_BACKUP_RETAIN=30 ...  # keep more history

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="docker compose -f $REPO_ROOT/docker/docker-compose.yml"
BACKUP_DIR="$REPO_ROOT/backups"
RETAIN="${AITELIER_BACKUP_RETAIN:-14}"

mkdir -p "$BACKUP_DIR"

if ! $COMPOSE exec -T postgres pg_isready -U aitelier -d aitelier >/dev/null 2>&1; then
    echo "✗ Postgres not reachable — start it first (make start infra)" >&2
    exit 1
fi

ts="$(date -u +"%Y%m%d-%H%M%S")"
out="$BACKUP_DIR/aitelier-$ts.dump"

echo "Backing up aitelier database → $out"
$COMPOSE exec -T postgres pg_dump -U aitelier -d aitelier -Fc > "$out"

# Guard against a truncated/empty dump masquerading as success.
if [ ! -s "$out" ]; then
    echo "✗ Dump is empty — removing $out" >&2
    rm -f "$out"
    exit 1
fi

echo "  ✓ wrote $(du -h "$out" | awk '{print $1}')"

# Retention: keep the newest $RETAIN dumps; prune the rest. Only ever touches
# files we created (the aitelier-*.dump glob) — never a blind recursive delete.
mapfile -t old < <(ls -1t "$BACKUP_DIR"/aitelier-*.dump 2>/dev/null | tail -n +"$((RETAIN + 1))")
if [ "${#old[@]}" -gt 0 ]; then
    echo "  pruning ${#old[@]} dump(s) beyond retention=$RETAIN:"
    for f in "${old[@]}"; do
        echo "    - $(basename "$f")"
        rm -f "$f"
    done
fi
