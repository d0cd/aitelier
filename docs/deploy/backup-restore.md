# Backup & restore

Postgres holds **all** durable aitelier state — `runs`, `run_events`,
`run_scores`, `schedules`, `webhook_deliveries`, `idempotency_keys`, and
`schema_version`. The `runs/` directory on disk is just scratch (prompts,
manifests); it is not authoritative and is not backed up. So a single
`pg_dump` is a complete backup.

## One-off backup

```bash
make backup          # → backups/aitelier-<UTC-timestamp>.dump
```

The dump runs **inside** the `postgres` container (`pg_dump 16`), so there is
no host client/server version mismatch. Output is custom format (`-Fc`), which
`pg_restore` can load selectively and in parallel. Backups land in `backups/`
(gitignored).

Retention defaults to the newest 14 dumps; override per run:

```bash
AITELIER_BACKUP_RETAIN=30 make backup
```

Pruning only ever deletes files matching `backups/aitelier-*.dump`.

## Scheduled backup

`make backup` is manual. aitelier no longer bundles a scheduler — schedule it
with whatever runs your other periodic jobs. Two common options:

```bash
# cron (daily 03:00):
0 3 * * *  cd /path/to/aitelier && make backup >> runs/logs/backup.log 2>&1

# or a process-compose entry that loops (mirrors the abacus-prices pattern):
#   aitelier-backup:
#     working_dir: /path/to/aitelier
#     command: "while :; do make backup >> runs/logs/backup.log 2>&1; sleep 86400; done"
#     availability: { restart: on_failure, max_restarts: 100 }
```

## Restore

Destructive — drops and recreates objects in the live database. Stop the
service first so it isn't writing mid-restore:

```bash
pc stop aitelier                # or: ./scripts/stop.sh service (leaves Postgres up)

make restore backups/aitelier-20260601-030000.dump
```

The script prompts for a typed `yes` and uses `pg_restore --clean --if-exists`.
`--clean` warnings about objects that don't exist yet are harmless on a fresh
database.

## Cross-version migration

aitelier applies SQL migrations from `core/src/aitelier/storage/migrations/` on
startup and records the applied version in `schema_version`. To move data
between aitelier versions:

1. `make backup` on the **old** version.
2. Upgrade the code (`git pull` / new release).
3. `make start` on the **new** version against the **same** Postgres volume —
   pending migrations apply forward automatically.

A dump from an older schema restored into a fresh database will be brought up
to the current schema the next time the new aitelier process starts. Restoring
a **newer** dump into an **older** aitelier is not supported (no down
migrations) — keep a pre-upgrade dump if you may need to roll back.

## Manual equivalents

If you'd rather not use the scripts:

```bash
# Backup
docker compose -f docker/docker-compose.yml exec -T postgres \
    pg_dump -U aitelier -d aitelier -Fc > backups/manual.dump

# Restore
docker compose -f docker/docker-compose.yml exec -T postgres \
    pg_restore --clean --if-exists -U aitelier -d aitelier < backups/manual.dump
```
