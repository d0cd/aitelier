# Always-on aitelier (macOS launchd)

`make start` is a manual, foreground-ish launch. To keep aitelier *always up* —
starting at login and restarting if it crashes — install the launchd agents.

```bash
make service-install      # load the supervisor + daily-backup agents
make service-uninstall    # stop and remove both
```

## What gets installed

Two user LaunchAgents in `~/Library/LaunchAgents/` (rendered from the templates
in this directory by `scripts/install-launchd.sh`, which substitutes the repo
path and a usable `PATH`):

- **`com.aitelier.agent`** — runs `scripts/supervise.sh`, which ensures infra is
  up (`start.sh infra`, idempotent) then `exec`s `aitelier serve` in the
  foreground. `RunAtLoad` starts it at login; `KeepAlive` restarts it on exit
  (crash *or* manual kill); `ThrottleInterval=10` prevents crash-loop hammering.
  Logs to `runs/logs/aitelier.log`.
- **`com.aitelier.backup`** — runs `scripts/backup.sh` daily at 03:00. Logs to
  `runs/logs/backup.log`. See [`backup-restore.md`](backup-restore.md).

Because `exec` preserves the PID, `supervise.sh` writes `runs/.aitelier.pid`
so `make status` / `make stop` keep reporting the right process.

## Reboot recovery needs Docker too

The launchd agents bring back the **host** service, but Postgres / LiteLLM /
Sandbox Agent run in Docker. For a clean recovery after reboot, set **Docker
Desktop → Settings → General → "Start Docker Desktop when you sign in"**. The
supervisor's `start.sh infra` step waits for Docker and self-heals (via
KeepAlive) once it's available.

## Operating it

```bash
launchctl print "gui/$(id -u)/com.aitelier.agent"      # state, last exit, PID
launchctl kickstart -k "gui/$(id -u)/com.aitelier.agent"  # force a restart
curl -f http://127.0.0.1:7777/v1/health                # is it up?
```

**Stopping:** while the agent is loaded, `make stop service` won't keep it down
— KeepAlive respawns it (the script warns you about this). Use
`make service-uninstall` to actually stop it.

## Not macOS?

These are launchd-specific. On Linux, the equivalent is a systemd user service
running `scripts/supervise.sh` with `Restart=always` plus a `.timer` unit for
the backup — not provided here yet.
