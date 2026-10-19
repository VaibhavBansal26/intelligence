# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin intelligence \
 && mkdir -p /data/models && chown -R intelligence /data
WORKDIR /app
COPY --from=build --chown=intelligence /app/.venv /app/.venv
COPY --from=build --chown=intelligence /app/app /app/app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 ENV=production DATABASE_URL=sqlite:////data/intelligence.db MODEL_DIR=/data/models WORKER_ENABLED=true
USER intelligence
EXPOSE 8000
VOLUME /data
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/health').status==200 else 1)"
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
