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

## 2026-11-08 — M1 complete

**Implemented.** Tables `days`, `quests`, `journal_entries`, `checkins`, `settings` (migration
0002); day service with lazy day creation, 3-quest limit with status preservation on re-submit,
journal CRUD, check-in answers, no-post decision; typed `Preferences` with validation and the
autonomous-mode confirmation phrase; routes under `/api/days`, `/api/quests`, `/api/entries`,
`/api/checkins`, `/api/settings`; `xbot` CLI (`today`, `log`, `quests`, `done`, `keygen`).

**Tests.** 24 passing in total; ruff and mypy strict clean. Real CLI round-trip against a live
server verified.

## 2026-11-08 — M2 complete

**Implemented.** Ingestion contracts (`SourceItem`, `ActivitySource`, `DayContext`); journal
source; GitHub client with retries and rate-limit detection; sync that lists the owner's repos
(plus `extra_repos`), keeps only `private == false`, skips excluded or stale repos, queries
commits in the IST day window converted to UTC, matches author by configured email or login
(the `author` API filter is not used), fetches stats and files, stores diffs only for repos with
`include_diffs`; `GitHubSource` renders commits as source items; routes for sync and per-repo
settings; commits included in the day view. Migration 0003.

**Tests.** 35 passing in total (11 new, GitHub API mocked with respx): public-only filter,
day-bound query params, email-or-login matching, diff truncation and idempotent re-sync, excluded
repos, rate limit recorded as error, 5xx retry, missing username, route and repo-settings flow.

**Real run.** Live sync against `aayush2724` with the gh token: 2026-11-07 found 1 commit,
2026-11-05 found 5 commits, across Portfolio and Vetra; private repos never queried.

**Limitations.** Default branch only; org repos only via `extra_repos`; diff truncation is
character-based.

**Next.** M3: LLM provider, Stage 1 activity understanding, Stage 2 insight discovery, pipeline
run tables.

## 2026-11-08 — LangGraph research complete, design revision 3 ready

Owner asked for the pipeline to be built on LangGraph with complete research first. Verified
versions and signatures on Python 3.14, ran prototype graphs against Docker Postgres (retry
policy, Send fan-out, interrupt and resume from a new graph object, crash recovery by `None`
resume, pool-based saver), checked the `ChatAnthropic` request payload offline, and found that
`error_handler` does not receive the exception. Advisor review folded in. Result:
`docs/phase-2-langgraph-design.md` (rev 3 final) with a status model, compare-and-set resumes,
one draft-set call instead of fan-out, bounded recovery, and M3/M4a/M4b/M5 redefined. No pipeline
code written; awaiting the owner's go-ahead and the model-client decision.
