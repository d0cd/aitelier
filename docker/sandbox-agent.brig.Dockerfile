# Sandbox Agent in a brig cell.
#
# Variant of `docker/sandbox-agent.Dockerfile` with the bits brig
# specifically needs:
#   - Pre-bake the claude + codex agent CLIs and their ACP bridges
#     (warden's mitmproxy + Lima networking is too slow for SA's 30s
#     install timeout — verified empirically).
#   - Bundle a brig-aware entrypoint that relocates HOME under /tmp
#     (rootfs is read-only) and trusts warden's MITM CA.
#
# Build:
#   brig image build --tag sandbox-agent-brig:latest \
#     --file docker/sandbox-agent.brig.Dockerfile .
# Run:
#   brig run --file docs/deploy/sandbox-agent.cell.yaml -d

# Debian-slim (glibc) rather than alpine (musl). SA itself is musl-linked
# and runs on both, but the pre-baked claude binary is dynamically linked
# against glibc — it'd fail with "not found" on alpine.
FROM debian:bookworm-slim

# SA itself + node/npm for the claude-agent-acp bridge package, plus
# curl/ca-certificates for the entrypoint.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash ca-certificates nodejs npm git \
 && rm -rf /var/lib/apt/lists/* \
 && ARCH=$(uname -m) \
 && curl -fsSL \
        "https://releases.rivet.dev/sandbox-agent/0.4.x/binaries/sandbox-agent-${ARCH}-unknown-linux-musl" \
        -o /usr/local/bin/sandbox-agent \
 && chmod +x /usr/local/bin/sandbox-agent \
 && sandbox-agent --version

# Pre-bake the claude agent binary. SA's `find_in_path("claude")` at
# agent-management/src/agents.rs:504 shortcuts the install probe when
# the binary is already on PATH. ~230 MB; the file is fetched on the
# host (see scripts/test-brig-mode.sh) since the host filter may block
# the path the build itself takes through warden.
COPY docker/prebaked-agents/claude/claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude && /usr/local/bin/claude --version

# Pre-install the codex CLI + both ACP bridges as global npm packages
# so their binaries land on PATH. SA's `find_in_path(<name>)` then
# short-circuits the runtime `npm install` (which would otherwise go
# through warden's mitmproxy and miss SA's 30s install window).
# Pins match the ACP registry's `npx.package` field — bump when the
# registry bumps.
#   - `@openai/codex`: codex CLI itself (claude is pre-baked above)
#   - `@agentclientprotocol/claude-agent-acp`: ACP bridge for claude
#   - `@zed-industries/codex-acp`: ACP bridge for codex
RUN npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000 \
 && npm config set fetch-timeout 600000 \
 && npm install -g \
        @openai/codex@0.132.0 \
        @agentclientprotocol/claude-agent-acp@0.36.1 \
        @zed-industries/codex-acp@0.14.0 \
 && command -v codex \
 && command -v claude-agent-acp \
 && command -v codex-acp

WORKDIR /app
COPY scripts/cell-entrypoint.sh /app/cell-entrypoint.sh
RUN chmod +x /app/cell-entrypoint.sh

EXPOSE 2468

# Brig cell command (in docs/deploy/sandbox-agent.cell.yaml) overrides
# this; the default makes the image runnable for local smoke too.
CMD ["/app/cell-entrypoint.sh"]
