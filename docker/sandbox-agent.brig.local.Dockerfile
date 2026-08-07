# Local-source Brig image for Aitelier development.
#
# This file is built through scripts/build-local-agent-image.sh. That script
# stages only the two explicitly selected source worktrees plus the runtime
# assets below into a temporary build context; it never sends the user's home
# directory to Brig.

FROM docker.io/library/rust:1.97-bookworm AS sandbox-agent-builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates clang cmake git libclang-dev \
      libsqlite3-dev libssl-dev pkg-config protobuf-compiler zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src/sandbox-agent
COPY sandbox-agent/ ./
ENV SANDBOX_AGENT_SKIP_INSPECTOR=1
RUN cargo build --locked --release -p sandbox-agent --bin sandbox-agent

FROM docker.io/library/rust:1.97-bookworm AS codex-acp-builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates clang cmake git libclang-dev \
      libsqlite3-dev libssl-dev pkg-config protobuf-compiler zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src/codex-acp
COPY codex-acp/ ./
RUN cargo build --locked --release --bin codex-acp

FROM docker.io/library/debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git libgcc-s1 libsqlite3-0 libssl3 nodejs npm \
 && rm -rf /var/lib/apt/lists/*

# Keep the native CLIs and Claude adapter pinned exactly as in the normal Brig
# image. codex-acp itself comes from the local Rust build below.
COPY aitelier/docker/prebaked-agents/claude/claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude \
 && /usr/local/bin/claude --version \
 && npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000 \
 && npm config set fetch-timeout 600000 \
 && npm install -g \
      @openai/codex@0.137.0 \
      @agentclientprotocol/claude-agent-acp@0.36.1

COPY --from=sandbox-agent-builder \
     /src/sandbox-agent/target/release/sandbox-agent \
     /usr/local/bin/sandbox-agent

# Retain package metadata around the locally built adapter. Sandbox Agent uses
# the package.json nearest the resolved executable to report the exact adapter
# version without trying to launch the stdio ACP server with `--version`.
COPY --from=codex-acp-builder \
     /src/codex-acp/target/release/codex-acp \
     /opt/local-codex-acp/bin/codex-acp
COPY codex-acp/npm/package.json /opt/local-codex-acp/package.json
RUN chmod +x /opt/local-codex-acp/bin/codex-acp \
 && ln -s /opt/local-codex-acp/bin/codex-acp /usr/local/bin/codex-acp \
 && sandbox-agent --version \
 && command -v codex \
 && command -v claude-agent-acp \
 && command -v codex-acp

ENV HOME=/tmp/home \
    SANDBOX_AGENT_ACP_REQUEST_TIMEOUT_MS=3660000
WORKDIR /app
COPY aitelier/scripts/cell-entrypoint.sh /app/cell-entrypoint.sh
RUN chmod +x /app/cell-entrypoint.sh

EXPOSE 2468
CMD ["/app/cell-entrypoint.sh"]
