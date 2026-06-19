# Feedback from running Sandbox Agent in a brig cell

**First report:** 2026-05-18 (brig 0.3.0 base, aitelier+SA co-located in cell)
**Latest update:** 2026-06-04 (orphan-subnet block + 403-vs-502 ingress after a VM restart)
**Consumer:** aitelier (runs on host; talks to SA-in-cell via brig ingress)
**Test artifact:** `docs/deploy/sandbox-agent.cell.yaml`,
`docker/sandbox-agent.brig.Dockerfile`, `scripts/test-brig-mode.sh`

**Architecture:** Only Sandbox Agent runs in the brig cell. Aitelier
itself runs on the host (where Postgres + LiteLLM live) and reaches
SA-in-cell via brig's ingress reverse proxy at
`http://127.0.0.1:8443/sandbox-agent/v1/...` with a bearer token. This
keeps the agent execution sandbox cleanly separated from the
HTTP-service runtime — brig isolates what needs isolating (the agent
processes) without conflating it with the rest of aitelier's
infrastructure.

**Status today:** brig SA cell starts cleanly on a fresh `brig system
up`, claude-acp authenticates and reaches `api.anthropic.com` through
warden's MITM, and streamed agent message chunks now arrive
incrementally through ingress. Aitelier-on-host + SA-in-brig is a
working deployment. The items below are the surprises we hit — most
are bugs brig has since shipped fixes for, with a couple of
adoption-quality items still open.


## 2026-06-04 — surprises after an overnight VM restart

aitelier-on-host + SA-in-brig was running fine. Came back the next day
and SA was unreachable; aitelier's `/v1/discovery` reported
`sandbox_agent.reachable: false`. Diagnosed + worked around. These are
sharper variants of the "exited cell → 502" item in *Smaller frictions*
below — worth their own writeup because the recovery path is different.

> **✅ Addressed in brig (commit `8dbb26b`, branch `feat/host-mounts`).**
> `allocate()` is now idempotent per cell name (subnet reclaim — §1);
> a `restart: always` cell field re-launches gone cells on `brig system up`
> (§3); the ingress returns a descriptive 404 + attributes rejects to the
> cell (§2); and `brig ps` / `brig cell ls` / `brig cell status` aliases
> landed (CLI §).
> **Verified by us:** CLI aliases work; relaunch no longer hits the
> subnet error (now blocked on an unrelated `mount_roots` VM-state issue
> from the in-flight host-mounts work — recreate the VM to clear it).
> **Adopted:** `restart: always` added to
> `docs/deploy/sandbox-agent.cell.yaml`.

### 1. Orphan subnet outlives the cell and blocks relaunch; no targeted cleanup

After the restart the `sandbox-agent` cell was **gone** from
`brig cell list` (not `exited` — fully absent; its image had been
evicted too). But the subnet allocation persisted, so relaunch from yaml
hard-failed:

```
$ brig run --file docs/deploy/sandbox-agent.cell.yaml -d
[ERROR] Failed to start cell 'sandbox-agent': Cell 'sandbox-agent' already has subnet allocated
```

`brig cell stop sandbox-agent` and `brig cell rm sandbox-agent` did **not**
free it (there's no live/stopped cell left to remove — only the leaked
subnet). The only thing that cleared it was the broad `brig system prune`:

```
$ brig system prune
  freeing subnet: 10.60.1.0/24 (sandbox-agent)
  removing orphan workspace: /Users/d0c/.brig/state/{sandbox-agent,test,cell-with-ingress,cell-without}
Pruned: 4 cells, 0 log files, 1 subnets
$ brig run --file docs/deploy/sandbox-agent.cell.yaml -d   # now succeeds
[INFO] Registered 1 ingress routes for 'sandbox-agent'
```

Pain points:
- `brig run` for a given name should **reclaim/reuse** an orphan subnet
  held by that same name instead of erroring — it already knows it's the
  same cell.
- The error should name the remedy (`brig system prune`), not just the
  symptom.
- `brig system prune` is too broad as the *only* fix: it also swept three
  unrelated orphan workspaces (`test`, `cell-with-ingress`,
  `cell-without`). A **targeted** `brig cell rm --force <name>` (or
  `brig cell network <name> --release`) that frees just that cell's
  subnet + workspace would let a consumer self-heal one cell without
  nuking every orphan.
- This recurs on **every** VM restart, so it's a daily papercut for any
  persistent brig-hosted service.

**Suggestion:** make `brig run --file` idempotent w.r.t. a same-named
orphan subnet, or add a per-cell force-cleanup.

### 2. Ingress returns 403 (not 502) when no cell is bound to the route

Different from the documented "exited cell → 502". With the cell fully
absent (post-restart, pre-relaunch), the ingress returned **403** for the
route — with *and* without the correct bearer token:

```
$ curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8443/sandbox-agent/v1/agents
403
$ curl -s -o /dev/null -w "%{http_code}\n" \
    http://127.0.0.1:8443/sandbox-agent/v1/agents          # no token → still 403
403
```

403 reads as auth failure — we spent time confirming the token still
matched the `sandbox-agent-ingress-token` secret (it did) before
realizing the route just had no upstream. The ingress now has two opaque
"no upstream" behaviors (502 when the cell exists-but-exited, 403 when no
cell is bound), neither of which says so.

**Suggestion:** distinguish auth (401/403) from missing-upstream
(502/503 with a body like `no cell bound to route 'sandbox-agent'`).
Today a consumer can't tell a bad token from a down cell.

### 3. Cell + image don't survive `brig system up`; no autostart

The restart left VM down, cell gone, image evicted. Recovery meant a slow
cold image rebuild through warden (see *Still blocking* item 2) **plus**
the subnet block above. There's no "persistent cell" / "re-launch on
`brig system up`" concept, and the VM doesn't autostart at login — rough
for a service you want always-on (we point a launchd supervisor at it).

**Suggestion:** a cell-yaml `restart: always` (or
`brig system up --restore-cells`) that re-launches declared cells and
re-registers their ingress after a VM bounce, plus optional VM autostart
(a brig launchd agent).

### Minor CLI ergonomics

`brig cell ls`, `brig ls`, and `brig cell status <name>` aren't
recognized (it's `brig cell list` / `brig cell inspect`). The argparse
error helpfully lists valid choices, but `ls`/`status` are near-universal
aliases — accepting them (or a top-level `brig ps`) would cut friction
during incident triage.


## Fixed since first report

### ✅ Ingress flows no longer killed by enforce.py rebinding check
Brig's `addons/enforce.py` now exempts flows tagged
`flow.metadata["ingress_route"]` in both `server_connected` (line ~590)
and `responseheaders` (line ~644). That was the one-line fix we
suggested; thank you for shipping it.

There's still a related-but-subtler issue with the rebinding check
that we hit while diagnosing — see the "DNS rebinding warning still
fires for warden's own routed flows" section below.

### ✅ `host_services` is now declared in cell yaml
The flattening from "global registry + per-cell ACL grant + cell yaml
policy.allow" down to just "cell yaml `host_services:` *is* the grant"
is a huge ergonomics win. We dropped two `brig policy set` steps from
our setup script. Cell yaml is now the single source of truth.

### ✅ `host_sockets` for unix-socket bind-mounts
This is the option we asked for as an alternative to raw TCP. We
haven't wired Postgres through it yet (Docker Postgres exposes TCP, not
a unix socket, so we'd need a host-side socat to expose one), but the
*primitive is there* — that's all we were missing.

### ✅ Missing ingress-token is now a hard error
`brig/cell/lifecycle.py` raises `BrigError` with a clear suggestion when
a cell declares `ingress: auth: token` but no `<cell-name>-ingress-token`
secret is registered. We hit the prior silent-401 behavior the first
time around — clean fix, thanks.

### ✅ Warden MITM CA is auto-trusted in cells (`trust_warden_ca: true`)
Cells now get a combined system+warden CA bundle mounted at
`/run/brig/ca-bundle.crt` with `SSL_CERT_FILE` /
`REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS`
auto-exported. We deleted our manual `warden-ca-cert` secret + the
entrypoint-side stitching when we migrated.

**Three implementation bugs caught mid-session and fixed by brig the
same day:**
- Bug A — `vm_run` auto-sudo didn't apply to `sh -c` scripts that
  contained `podman exec`; `brig/cell/ca_bundle.py:87` ran unprivileged
  and couldn't see the rootful warden container.
- Bug B — `WARDEN_CA_PATH_IN_CONTAINER` doesn't exist on a fresh
  warden (mitmproxy generates the CA lazily on first traffic), making
  `brig run` fail 100% of the time on the first cell after
  `brig system up`. Fix: brig now primes the CA at warden startup.
- Bug C — `/home/mitmproxy/.mitmproxy/` was owned by `root:root` so
  mitmproxy couldn't write its own state. Fixed in warden's image.

**Migration foot-gun worth documenting:** if a cell-author's entrypoint
ALSO sets `SSL_CERT_FILE` (common pre-0.3.0 workaround), it overrides
brig's auto-mount. When warden's CA rotates on the next `brig system up`,
the cell-author's cached secret goes stale → silent TLS hangs with no
error inside the cell. mitmproxy's MITM presents the client-side
handshake as "succeeded" (cell trusts warden), but the UPSTREAM
handshake fails and the proxy drops with no signal back. We burned
~30 minutes on this. Suggestions:
1. `brig system doctor` could include
   `[OK] Warden CA matches all cells' staged bundles`.
2. Cell-definition docs could include a "do not also set `SSL_CERT_FILE`
   in your entrypoint" note in the `trust_warden_ca:` section.
3. Fail (or warn loudly) when an entrypoint's `Config.Env` overrides
   `SSL_CERT_FILE` differently from what brig set.

### ✅ `tls_passthrough` in cell yaml (principled fix for credentialed flows)
Brig 0.3.0 shipped exactly the API we sketched: per-cell
`tls_passthrough: [<host>]` declarations let cells skip mitmproxy
termination for hosts whose TLS won't survive MITM (Cloudflare-fronted
strict TLS, HPKP, ECH). The cell-definition docs include the threat-
model contrast (URL audit vs. handshake compat + credential
confidentiality).

Verified end-to-end: with
```yaml
policy:
  tls_passthrough:
    - "chatgpt.com"
    - "auth.openai.com"
```
in the cell yaml, codex's OAuth refresh completes through warden and
the agent responds normally:
```
agent:codex (max_turns=1) → 'HI' in 6s
```
Pre-`tls_passthrough` this hung at the first TLS handshake against
chatgpt.com. Codex is now a viable brig backend alongside claude.

### ✅ `brig` pytest fixtures no longer clobber the real subnet-map.json
`_write_subnet_map` is now keyword-only on `map_file` and `allocate()` /
`free()` pass it explicitly. Our regeneration workaround in
`scripts/test-brig-mode.sh` has been removed.

### ✅ Ingress passes SSE through unbuffered
Brig now flushes streaming responses on ingress routes — `Content-Type:
text/event-stream` responses no longer get buffered by warden's
mitmproxy. End-to-end verification against the live brig SA:

```
$ curl -N -X POST aitelier/v1/chat/completions -d '{"model":"agent:claude",
    "messages":[{"role":"user","content":"Count from 1 to 5 separated by commas."}],
    "stream":true,"aitelier":{"max_turns":2}}'

data: {... "delta": {"role": "assistant"} ...}
data: {... "delta": {"content": ""} ...}
data: {... "delta": {"content": "1, 2, 3, 4, 5"} ...}
data: {... "delta": {}, "finish_reason": "stop", "usage": {...} ...}
```

3-second end-to-end. Chunks arrive incrementally during the agent run,
not in a single post-completion flush. This unblocks aitelier running
on brig SA — claude-acp's `session/update` notifications flow through
the ingress in real time. 🎉


## Still blocking real-shaped deployments

### 1. Raw TCP host_services still missing (sidestepped by SA-only refactor)

The `host_services` flattening is HTTP-only. Cells still can't reach
Postgres / Redis / MongoDB / MySQL / ssh / gRPC-over-h2c on the host.
In our first pass we co-located aitelier+SA in the cell and had to
fall back to InMemoryStore because the cell couldn't reach the host's
Postgres. **Moving aitelier out of the cell sidesteps the issue
entirely** for our use case (aitelier on host has direct Postgres
access) — but it's still a real gap for any cell that genuinely needs
to talk to a TCP service.

`host_sockets` unblocks the path *if* you've already got the upstream
listening on a unix socket; for the dockerized Postgres pattern (which
is what `make start` boots), we'd need either:

- Documentation/recipe for "expose a host-side socat from a docker
  service into a brig cell via host_sockets," or
- Native raw TCP host_services. Same trust model as HTTP host_services
  today; the only real cost is less per-request observability.

This is no longer blocking aitelier's deployment, but the next consumer
will hit it again.

### 2. Outbound TLS through warden is too slow for SA's install timeouts

This isn't a brig bug per se, but it's a pattern-collision worth
flagging. Sandbox Agent (Rivet's coding-agent runtime) does a lot of
first-run network work on agent dispatch:

1. Fetch agent CLI binary (`storage.googleapis.com`, ~230 MB for `claude`)
2. Fetch ACP registry manifest (`cdn.agentclientprotocol.com`)
3. `npm install <acp-bridge>` (npm registry + tarball)
4. Spawn bridge, which calls api.anthropic.com on every turn

Steps 1-3 are inside SA's 30-second install timeout. Through warden's
mitmproxy + Lima's user-mode networking, every single one took longer
than 30s on first run. Our cell would never start an agent successfully
until we pre-baked the binaries into the image at build time
(`brig image build` runs natively in the VM with no warden in the
path, so build-time downloads are fast).

The pre-baking we ended up doing:

- COPY a host-fetched `claude` binary into `/usr/local/bin/claude`
  (~230 MB; SA's `find_in_path("claude")` at agents.rs:504 shortcuts
  the install probe).
- `npm install -g @agentclientprotocol/claude-agent-acp@<pinned-from-registry>`
  during image build (SA's `find_in_path("claude-agent-acp")` at
  agents.rs:453 similarly shortcuts).

This works but it noticeably bloats the image and means the pre-baked
versions can go stale relative to the ACP registry. **A cleaner brig
story would be either:**

- A documented "cell egress passthrough" mode for specific large-blob
  hostnames (`storage.googleapis.com`, `registry.npmjs.org`,
  `objects.githubusercontent.com`) where Warden allows + logs but does
  not MITM. Trade-off: per-request URL granularity is lost, but cells
  hosting CLI runtimes need this to bootstrap in reasonable time.
- A way to feed warden's CA + http_proxy into `brig image build` so
  the build can fetch the same way the runtime does (and the build
  cache amortizes it). Today they're decoupled, so the build path
  is fast and unfiltered while the runtime path is slow and filtered
  — a fragile asymmetry.

Even with pre-baking, `npm install` of the ACP bridge through warden
on first cold cell still takes several minutes — most of our 5 m e2e
runtime was warden-MITM'd npm fetches. Cached after first run.


## Smaller frictions (now that we hit them, worth flagging)

### `brig cell start` doesn't re-register ingress routes after a brig restart

After `brig system up` (e.g., reboot, or `brig system down/up`), any
existing cells are present in `brig cell list` but in `exited` state.
Running `brig cell start <name>` flips them to `running` and the cell's
process binds its internal port — but the **ingress reverse proxy on
:8443 doesn't know how to route to it**, so external requests get
HTTP 502 indefinitely.

Reproduction (confirmed 2026-05-26 against latest 0.3.0):

```
$ brig cell list
NAME            STATUS    IMAGE
sandbox-agent   exited    localhost/sandbox-agent-brig:latest

$ brig cell start sandbox-agent
[INFO] Cell 'sandbox-agent' started

$ brig cell list
NAME            STATUS    IMAGE
sandbox-agent   running   localhost/sandbox-agent-brig:latest    # ✓ running

$ for i in $(seq 15); do curl -s -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8443/sandbox-agent/v1/agents; sleep 1; done
502
502
502   # … for at least 15 seconds with no recovery
```

Workaround: `brig cell rm <name> --keep-workspace && brig run --file
<cell.yaml> -d`. Workspace data is preserved, but the cell is rebuilt
from yaml and ingress routes get re-registered:

```
$ brig run --file docs/deploy/sandbox-agent.cell.yaml -d
[INFO] Registered 1 ingress routes for 'sandbox-agent'
[INFO] Cell 'sandbox-agent' started

$ curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8443/sandbox-agent/v1/agents
200
```

The `Registered 1 ingress routes` line is the diagnostic — it only
appears on `brig run --file`, not on `brig cell start`. The natural
mental model is that `start` should fully restore a cell to its prior
working state; "you must rm + run-from-yaml after every brig restart"
is surprising and not documented.

**Suggestion:** `brig cell start` should re-register ingress routes
from the cell's stored yaml (which brig already knows — it staged the
cell from yaml originally). Or, at minimum, surface a warning when
starting a cell with declared ingress: "ingress routes will not be
re-registered; use `brig run --file` if your ingress isn't reachable
after start."


### `brig cell network` only logs egress

When the ingress was 403-ing, `brig cell network aitelier` showed no
activity at all. We had to drop down to
`limactl shell brig sudo podman logs warden` to see the ingress addon's
log lines.

**Suggested:** include ingress hits in `brig cell network`, prefixed
distinctly (`INGRESS: ...`) so they're grep-able.

### DNS rebinding warning still fires for warden's own routed flows

We see this in `podman logs warden` on every host_service hit and every
ingress hit, even though the response succeeds:

```
[15:24:46.846] HOST_SERVICE: aitelier -> litellm (192.168.5.2:4000)
[15:24:46.846][10.60.1.43:37147] server connect 192.168.5.2:4000
[15:24:46.846] BLOCKED: DNS rebinding detected - resolved to 192.168.5.2
[15:24:46.846] BLOCKED: server_connected failed to validate IP, closing connection
[15:24:46.846] Addon error: 'Server' object has no attribute 'close'
```

The exemption check in `server_connected` looks at
`flow.metadata.get("host_service")` / `…("ingress_route")` — but in
practice that branch evaluates to False on the current mitmproxy
version (most likely `data.flow` is `None` at this hook, since the
exemption is reached via `flow = getattr(data, "flow", None)`).
So the rebinding-detect branch runs anyway.

The reason the request still succeeds is *not* the exemption:
`data.server.close()` raises `AttributeError` ("'Server' object has
no attribute 'close'"), the `except` re-tries the same call and
also fails, and the connection is left intact. **So the current
working behavior depends on a latent bug** — if `close()` ever
gets fixed in mitmproxy or the addon, ingress and host_service
flows will start failing again.

Cleanest fix: defer the rebinding check from `server_connected` to
`responseheaders`. The `responseheaders` exemption already works
(metadata is populated by then), and the check still catches actual
DNS-rebinding attacks (the response arrives from the rebound IP).
That also removes the misleading warden log lines.

## What worked great

- `brig run --file <yaml> -d` lifecycle.
- Secrets mount paths (`/run/secrets/<name>`) are predictable and survive
  rotation via `brig secrets add` without rebuilding the image.
- `brig image build` transparently tars + streams build context into
  the VM. Fast.
- `brig system doctor --quick` as a preflight.
- The host_services flattening — cleanest API surface change of the
  release.
- Warden logs in `podman logs warden` are very readable. Every blocked
  request prints a clear reason. This is how we diagnosed the
  rebinding bug originally and the cert-trust issue this round.


## Wishlist priority order for next brig release

| # | Severity | Item |
|---|---|---|
| 1 | ✅ Shipped 8dbb26b | Orphan subnet outlives a cell across VM restart and blocks `brig run`. Fixed: `allocate()` is idempotent per cell name, so `brig run` reclaims a same-named orphan. (2026-06-04 §1) |
| 2 | ⚠ Partial 8dbb26b | `restart: always` cell field now re-launches gone cells on `brig system up` (re-registering ingress). VM autostart at login was intentionally not added — our `lib.sh` self-heals via `brig system up`. (2026-06-04 §3) |
| 3 | Adoption | Raw TCP host_services, OR a documented socat-bridge recipe for unix sockets → host TCP services. Aitelier sidesteps by running outside the cell; the next consumer will hit this. |
| 4 | Quality | Feed warden into `brig image build` so first-run agent installs (`npm install`, agent CLI binary fetch) aren't multi-minute through MITM. Today the build/runtime asymmetry forces pre-baking large binaries into the image. |
| 5 | ✅ Shipped 8dbb26b | Ingress now returns a descriptive 404 body for a no-route request and attributes rejected attempts (auth/oversize) to the cell in `brig cell network`. (2026-06-04 §2) |
| 6 | Minor | `brig system doctor`: add `[OK] Warden CA matches all cells' staged bundles` to surface stale entrypoint-managed `SSL_CERT_FILE` overrides before they become silent TLS hangs. |
| 7 | Minor | `brig cell network` to include ingress hits (currently egress-only). |
| 8 | Minor | Move the DNS-rebinding check from `server_connected` to `responseheaders` so the ingress-route / host-service exemption metadata is actually populated when the check runs. |
| 9 | Minor | Document the `<cell-name>-ingress-token` naming convention in `brig run --help` / cell-yaml reference (currently only discoverable from source). |
| 10 | ✅ Shipped 8dbb26b | CLI aliases `brig cell ls` / `brig cell status` + top-level `brig ps` now accepted. Verified working. (2026-06-04) |

With SSE flowing, aitelier-on-host + SA-in-brig is a real, working
shape. Items 1 and 2 are about widening the adoption surface for the
next consumer; the rest are quality-of-life polish.
