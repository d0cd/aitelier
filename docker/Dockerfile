# Build aitelier core into a container image.
#
# Sandbox Agent runs separately. In docker mode it ships as the `sa`
# profile in docker-compose.yml (docker/sandbox-agent.Dockerfile). In
# brig mode SA lives in its own cell (docker/sandbox-agent.brig.Dockerfile
# + docs/deploy/sandbox-agent.cell.yaml). This image is just the
# FastAPI service.
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
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=build /usr/local/bin/aitelier /usr/local/bin/aitelier
COPY core/ ./core/
COPY schemas/ ./schemas/
RUN mkdir -p /app/runs
EXPOSE 7777
CMD ["aitelier", "serve", "--host", "0.0.0.0", "--port", "7777"]
