# Build aitelier core into a single-binary-ish container.
# Sandbox Agent and LiteLLM run separately (compose); this image is just
# the FastAPI service.
#
# Usage:
#   docker build -f docker/Dockerfile -t aitelier:latest .
#   docker run -p 7777:7777 \
#       -e AITELIER_HOST=0.0.0.0 \
#       -e AITELIER_API_KEY=<your-key> \
#       -e LITELLM_BASE_URL=http://litellm:4000 \
#       -e SANDBOX_AGENT_BASE_URL=http://sandbox-agent:2468 \
#       -v aitelier-runs:/app/runs \
#       aitelier:latest

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
ENV AITELIER_HOST=0.0.0.0
ENV AITELIER_PORT=7777
EXPOSE 7777
CMD ["aitelier", "serve", "--host", "0.0.0.0", "--port", "7777"]
