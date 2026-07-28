# Running aitelier under process-compose

aitelier is a long-running service (`aitelier serve` on `:7777`) plus its infra
(Postgres, LiteLLM, and the Sandbox Agent). To keep it running across crashes
and reboots, run it under a process supervisor. This doc covers
[process-compose](https://github.com/F1bonacc1/process-compose); for a bare
launchd/systemd machine with no supervisor, `scripts/supervise.sh` is the
foreground entrypoint you point one at.

> **Pick one supervisor.** Run aitelier under **either** process-compose **or**
> your OS init (launchd/systemd) — not both. Two supervisors race for `:7777`
> and fight over `runs/.aitelier.pid`. If you use process-compose, don't also
> stand up an OS-level agent for the same service.

## The recipe

Add this process to your `process-compose.yaml`. It waits for the container
runtime, then runs `supervise.sh` (which ensures infra is up, then execs the
server in the foreground so the supervisor tracks the serve process directly):

```yaml
processes:
  aitelier:
    description: "LLM/agent gateway on :7777"
    working_dir: /path/to/aitelier
    # Wait for the container runtime (Postgres/LiteLLM run in it), then serve.
    command: "for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done; exec scripts/supervise.sh"
    availability:
      restart: on_failure          # supervise, don't run-once
      max_restarts: 100
    readiness_probe:
      http_get:
        host: 127.0.0.1
        port: 7777
        path: /v1/health
      initial_delay_seconds: 5
      period_seconds: 5
      failure_threshold: 30        # ~150s to go healthy on a cold start
    # Optional: if the Sandbox Agent runs in a brig cell that is ALSO a
    # process-compose entry, order aitelier after it (see below).
    # depends_on:
    #   brig:
    #     condition: process_started
```

Two things that are easy to miss:

- **`availability.restart` is required to actually be supervised.** Without an
  `availability` block, process-compose runs the process *once* — a serve crash
  would not be restarted. Set `restart: on_failure`.
- **`readiness_probe` on `/v1/health`** lets the supervisor (and anything with a
  `depends_on: aitelier`) know when it's actually serving, not just spawned.

## The Sandbox Agent dependency

aitelier's agent path (`agent:<backend>`) runs inside the **Sandbox Agent**,
whose lifecycle is owned by **brig** — `brig system watchdog` keeps its cell
(and the Lima VM) alive across host sleep/reboot. aitelier only *consumes* it:
`start.sh` probes the SA and waits briefly for it, but does not manage its
lifecycle.

So make sure brig is running (`brig system up`, or a `brig system watchdog`
supervised the same way). If brig is itself a process-compose entry, express the
order with `depends_on: brig` (uncomment above) so aitelier starts after it.

## Operating it

- Start on demand: `pc start-svc aitelier` (or drop `disabled: true` to make it
  always-on).
- After a code edit: `pc restart-svc aitelier`.
- Logs: `pc logs aitelier`.

`aitelier serve` shuts down cleanly on `SIGTERM` (its lifespan cancels in-flight
runs and marks orphans), so the supervisor can stop/restart it safely.
