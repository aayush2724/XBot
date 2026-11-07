# XBot — Progress log

## 2026-11-08 — Phase 1 complete (pending 3 confirmations)

- Environment inspected; repo initialised.
- Found that X API moved to pay-per-use in 2026 (~$0.015/post, ~$0.20/post with a link). Daily
  text posting costs well under $1/month. No free write tier exists any more.
- Decided defaults: Asia/Kolkata days, max one post/day with explicit no-post outcome, OAuth 2.0
  PKCE for X, no links by default, diffs opt-in per repo, no Redis/LangGraph in MVP.
- Deliverables: `docs/phase-1-discovery.md`, `TODO.md`, `.gitignore`.
- Waiting on: LLM configuration, hosting model, GitHub privacy scope.
- Decisions confirmed by owner: Claude Opus 5 for every stage; runs locally now with catch-up
  scheduling, VPS later; GitHub source reads public repositories only.

## 2026-11-08 — Phases 2 and 3 locked

- Architecture and milestones written, reviewed by the advisor, revised (rev 2). Main fixes:
  committed publish claim before the network call with `find_recent` before any re-post; advisory
  lock scheduler leadership with a 10-minute atomic catch-up tick; row-locked single-use refresh
  token handling; GitHub author matching by email or login; sentence-coverage grounding guard.
- Verified Python 3.14 compatibility of every backend dependency and the SDK `parse` signature.
- Owner asked about cloning KryxAI; advised to keep XBot's grounded-pipeline angle instead.

## 2026-11-08 — M0 Scaffold complete

**Implemented.** `backend/` uv project (Python 3.14); `xbot.config.Settings` (pydantic-settings,
repo-root `.env`, vendor-named credential vars, refuses example secrets outside tests);
structlog with secret redaction; async SQLAlchemy `Database`; Alembic async env with baseline
migration; FastAPI app factory with lifespan, CSRF header guard, session-cookie login
(`/api/auth/login|logout|me`), `/api/health`; `docker-compose.yml` (Postgres 18 on 127.0.0.1:5433
with an init script that also creates `xbot_test`), `Dockerfile`, `.env.example`, README quick start.

**Decision.** Local PostgreSQL 18 on 5432 requires a password I do not have, so dev and tests use
the compose database on 5433. Switch by setting `XBOT_DATABASE_URL`.

**Tests.** 8 passing: health, login paths, tamper-proof cookie, CSRF guard, startup refusals.
ruff and mypy (strict) clean. Real uvicorn smoke test: health `db: ok`, wrong password 401, login
200, `/api/auth/me` 200 with cookie. `docker compose config` valid.

**Limitations.** No tables yet; Dockerfile has no frontend stage until M7.

**Next.** M1: days, quests, journal, check-ins, preferences, CLI.
