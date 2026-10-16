FROM python:3.12.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.15 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY recorder ./recorder

RUN useradd --system --uid 1000 --no-create-home app \
    && mkdir -p /data && chown app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    PORT=8000
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

CMD ["sh", "-c", "exec uvicorn recorder.web:create_app --factory --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 25"]
