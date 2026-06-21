# Feedback from running Sandbox Agent in a brig cell

**Consumer:** aitelier — runs on the host (with Postgres + LiteLLM) and
reaches Sandbox Agent inside a brig cell via brig's ingress reverse proxy
at `http://127.0.0.1:8443/sandbox-agent/v1/...` with a bearer token. Only
SA runs in the cell; the agent-execution sandbox stays cleanly separated
from aitelier's own runtime.

**Artifacts:** `docs/deploy/sandbox-agent.cell.yaml`,
`docker/sandbox-agent.brig.Dockerfile`, `scripts/test-brig-mode.sh`.

**Status (re-verified 2026-06-09 against brig source @ `d6e0553`):**
aitelier-on-host + SA-in-brig is a fully working, supported deployment.
Essentially all prior feedback has shipped — subnet reclaim, `restart:
always`, ingress 404 + per-cell attribution, raw TCP `host_services`,
`brig image build --use-warden`, connect-time DNS-rebinding guard,
`brig system doctor` CA-staleness check, ingress hits in `brig cell
network`, `brig cell start` ingress re-registration, `<cell>-ingress-token`
docs, CLI aliases (`brig ps` / `cell ls` / `cell status`), SSE
pass-through, and `tls_passthrough`. This doc is pruned to **only the
items still open**; both are minor.

---

## Outstanding

### 1. No VM autostart at login (minor — we self-heal)

`restart: always` re-launches a cell (and re-registers its ingress) on
`brig system up`, but the brig **VM + warden don't autostart at login** —
after a host reboot, something must run `brig system up` before any cell
is reachable. aitelier works around this: `scripts/lib.sh` runs
`brig system up` (idempotent) before bringing the SA cell up, so `make
start` and the launchd supervisor self-heal. Flagging for the next
consumer that wants a truly always-on brig-hosted service without a
wrapper script.

**Suggestion:** an optional brig launchd/login agent that brings the VM
+ warden up at login (off by default), so `restart: always` cells come
back after a reboot with no external nudge.

### 2. `HOME` under `/tmp` makes codex refuse to create helper binaries (minor — non-fatal)

The cell rootfs is read-only except `/work`, `/tmp`, `/run`, so the
entrypoint relocates `HOME=/tmp/home` (where the credential secrets are
symlinked). codex CLI then warns on every spawn:

```
WARNING: proceeding, even though we could not update PATH: Refusing to
create helper binaries under temporary dir "/tmp"
(codex_home: AbsolutePathBuf("/tmp/home/.codex"))
```

It's non-fatal — verified a tool-using `agent:codex/gpt-5.5` run executes
its shell tool and returns output — but codex treats a `/tmp`-rooted
`codex_home` as untrusted and skips helper-binary creation, which some
codex workflows may need. The next CLI that's stricter about a `/tmp`
HOME could fail outright.

**Suggestion:** a brig-provided **writable non-`/tmp` HOME mount** (e.g. a
small per-cell `/home/<cell>` tmpfs outside `/tmp`) so HOME-sensitive CLIs
behave normally without the cell author hand-rolling a mount.
