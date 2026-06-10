# Feedback from running Sandbox Agent in a brig cell

**First report:** 2026-05-18 (brig 0.3.0 base, aitelier+SA co-located in cell)
**Update:** 2026-05-19 (refactored to SA-only cell + brig fixes shipped)
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

**Live e2e status: 30 / 30 tests pass, 0 skip, 0 fail.** After
removing all skips and switching LLM-mode tests to Ollama (no Anthropic
key needed), the suite is fully green against the SA-in-brig +
aitelier-on-host deployment. The items below are the things that
surprised us along the way — either bugs that brig has since fixed, or
design gaps still open.


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

### 2. Warden MITM CA isn't auto-trusted in cells

Every cell egress goes through Warden's mitmproxy, which terminates
TLS with warden's own CA. Cells using HTTPS need warden's CA in their
trust store, or the TLS handshake fails with:

```
Client TLS handshake failed. The client does not trust the proxy's
certificate for api.anthropic.com (tlsv1 alert unknown ca)
```

We worked around it by extracting warden's CA from
`/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem`, registering it as
the brig secret `warden-ca-cert`, mounting it in the cell yaml, and
having `cell-entrypoint.sh` concatenate it onto `/etc/ssl/certs/...`
into `/tmp/ca-bundle.crt`, then exporting `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`. Works,
but every brig-cell consumer is going to rediscover this and roll
their own fragile copy.

**Suggested:** brig should mount its CA into every cell automatically
(e.g., at `/run/brig/warden-ca.pem`), and ideally also export `SSL_CERT_FILE`
in the cell's default env so common Python/Rust/Go/Node clients pick it
up without per-image work.

### 3. Outbound TLS through warden is too slow for SA's install timeouts

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


## ✅ Fixed: brig's own pytest tests clobber the real subnet-map.json

Reported during this session. Brig flipped `_write_subnet_map` to
keyword-only `map_file` and updated `allocate()`/`free()` to pass it
explicitly. Our test script's regeneration workaround has been
removed.

## Smaller frictions (now that we hit them, worth flagging)

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
| 1 | **Adoption** | Auto-mount Warden CA in cells + export `SSL_CERT_FILE` so common HTTPS clients trust it without per-image work |
| 2 | Adoption | Raw TCP host_services, OR a documented socat-bridge recipe for unix sockets → host TCP services. (Aitelier sidesteps by running outside the cell; the next consumer will hit this.) |
| 3 | **Quality** | TLS passthrough mode for specific allowed large-blob hosts (or feed warden into build path) so first-run agent installs aren't multi-minute |
| 4 | Minor | `brig cell network` to include ingress hits |
| 5 | Minor | Move the DNS-rebinding check from `server_connected` to `responseheaders` so the exemption metadata is actually populated when the check runs |
| 6 | Minor | Document the `<cell-name>-ingress-token` naming convention in `brig run --help` / cell-yaml reference (currently only discoverable from source) |

Items 1–3 are the difference between "brig hosts a real service" and
"brig hosts a service that mostly works after a lot of build-side
gymnastics."
