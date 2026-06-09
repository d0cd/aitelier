# Sandbox Agent (Rivet) in a container.
#
# SA is a 15 MB static Rust binary; we just need a base with curl + a
# place to extract it. Alpine is plenty. The agent backends (claude-code,
# codex, ...) get installed by SA itself on first use.
#
# Build:
#   docker compose --profile sa build
# Run (via aitelier):
#   set `[sandbox_agent] mode = "docker"` in aitelier.toml; `make start`
#   will start this service.

FROM alpine:3.20

RUN apk add --no-cache curl bash ca-certificates nodejs npm git \
 && curl -fsSL https://releases.rivet.dev/sandbox-agent/0.4.x/install.sh | bash \
 && cp "$(command -v sandbox-agent || echo /root/.local/bin/sandbox-agent)" /usr/local/bin/sandbox-agent \
 && chmod +x /usr/local/bin/sandbox-agent

# Credentials get mounted in by docker-compose, not baked in. SA reads
# ~/.claude/.credentials.json and ~/.codex/auth.json at agent dispatch.
ENV HOME=/root
WORKDIR /workspaces

EXPOSE 2468

# Subcommand + flags match what scripts/start.sh uses for host-mode SA:
#   sandbox-agent server --host <h> --port <p> --no-token
# Bind 0.0.0.0 so the docker bridge can route aitelier↔SA. `--no-token`
# is fine inside the container — the only consumer is aitelier on the
# same compose network. Add SANDBOX_TOKEN if you expose SA off-network.
CMD ["sandbox-agent", "server", "--host", "0.0.0.0", "--port", "2468", "--no-token"]
