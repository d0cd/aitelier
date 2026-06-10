# Build aitelier core into a container image.
#
# Sandbox Agent and LiteLLM run separately (compose / brig / other);
# this image is just the FastAPI service.
#
# CONFIG: aitelier reads ZERO env vars by design — all configuration
# lives in TOML. Mount a config file into the container:
#
#   docker build -f docker/Dockerfile -t aitelier:latest .
#   docker run -p 7777:7777 \
#       -v "$PWD/aitelier.toml:/app/aitelier.toml:ro" \
#       -v "$PWD/aitelier.secrets.toml:/app/aitelier.secrets.toml:ro" \
#       -v aitelier-runs:/app/runs \
#       aitelier:latest
#
# Or mount a user-global config:
#       -v "$PWD/aitelier.toml:/root/.config/aitelier/config.toml:ro"
#
# Inside aitelier.toml, point at the docker-compose service names:
#   [database]      url = "postgresql://aitelier:aitelier_local@postgres:5432/aitelier"
#   [litellm]       base_url = "http://litellm:4000"
#   [sandbox_agent] base_url = "http://sandbox-agent:2468"
#   [service]       host = "0.0.0.0"  port = 7777

FROM python:3.11-slim AS build
ENV UV_SYSTEM_PYTHON=1
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY sdks/python/ ./sdks/python/
COPY schemas/ ./schemas/
RUN uv pip install --system "./core" "./sdks/python"

FROM python:3.11-slim
WORKDIR /app
# Install curl + bash for the Rivet sandbox-agent installer, then bake
# SA into the image so the cell entrypoint can run both services
# without a network fetch on every cold start.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ARCH=$(uname -m) \
 && curl -fsSL \
        "https://releases.rivet.dev/sandbox-agent/0.4.x/binaries/sandbox-agent-${ARCH}-unknown-linux-musl" \
        -o /usr/local/bin/sandbox-agent \
 && chmod +x /usr/local/bin/sandbox-agent \
 && sandbox-agent --version
# Rivet's `curl install.sh | bash` ignores `uname -m` and always pulls
# x86_64 — broken on arm64 hosts. Direct fetch by detected arch is
# reliable on both Apple Silicon (aarch64) and Intel/AMD64.
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=build /usr/local/bin/aitelier /usr/local/bin/aitelier
COPY core/ ./core/
COPY schemas/ ./schemas/
COPY scripts/cell-entrypoint.sh /app/cell-entrypoint.sh
RUN mkdir -p /app/runs && chmod +x /app/cell-entrypoint.sh
EXPOSE 7777
# Default CMD is the FastAPI service alone — useful for docker-compose
# deployments where SA runs as a separate compose service. Brig cells
# override with `command: ["/app/cell-entrypoint.sh"]` to run SA + the
# service together (brig has no inter-cell routing primitive yet).
CMD ["aitelier", "serve", "--host", "0.0.0.0", "--port", "7777"]
