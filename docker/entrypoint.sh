#!/bin/sh
set -e
cd /app/backend
uv run --no-dev alembic upgrade head
exec uv run --no-dev uvicorn xbot.main:create_app --factory --host "${XBOT_HOST:-0.0.0.0}" --port "${XBOT_PORT:-8000}"
