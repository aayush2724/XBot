# XBot backend image. The frontend build stage is added in milestone M7.
FROM python:3.14-slim AS backend
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ /app/backend/
RUN uv sync --frozen --no-dev
COPY docker/entrypoint.sh /app/entrypoint.sh
EXPOSE 8000
CMD ["/app/entrypoint.sh"]
