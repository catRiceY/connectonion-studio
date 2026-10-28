FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim@sha256:36cdfbf910c8b0f651355c013e7ece9678f4ecbf030a9fd9e6779de421189805
RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends nginx ca-certificates libgomp1 \
    && apt-get clean && useradd --create-home --uid 10001 studio
WORKDIR /app
ENV UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# Resolve the frozen third-party environment before frequently-changing source.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra history --extra mcp --extra knowledge
COPY README.md ./
COPY co_studio ./co_studio
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra history --extra mcp --extra knowledge \
    && mkdir -p /home/studio/.co-studio && chown -R studio:studio /home/studio
COPY deploy ./deploy
ENV PATH="/app/.venv/bin:$PATH" STUDIO_AUTO_AUTH=0 PYTHONUNBUFFERED=1
USER studio
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=8s --start-period=30s CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:9900/api/knowledge/status',timeout=5)); assert r['ready']"
CMD ["python", "/app/deploy/serve.py"]
