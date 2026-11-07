# XBot

Personal build-in-public agent: collects what you actually did each day (journal + public GitHub
commits), finds the one thing worth sharing, drafts 2–3 X posts in your voice, grades them for
truthfulness, and publishes only after you approve.

Status: under construction. See `docs/` for the discovery, architecture and milestone plan, and
`docs/PROGRESS.md` for what works today.

## Quick start (development)

```bash
cp .env.example .env            # then edit XBOT_SECRET_KEY and XBOT_DASHBOARD_PASSWORD
docker compose up -d db         # Postgres 18 on 127.0.0.1:5433 (creates xbot and xbot_test)
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn xbot.main:create_app --factory --reload
curl http://127.0.0.1:8000/api/health
```

Tests:

```bash
cd backend && uv run pytest
```
