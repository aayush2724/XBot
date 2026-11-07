# XBot — Phase 3: Milestones

Each milestone is independently testable and ends with a summary (implemented, files, tests run,
limitations, next). Order is strict; later milestones depend on earlier ones.

| # | Milestone | Deliverables | Acceptance criteria | Depends on |
|---|---|---|---|---|
| M0 | Scaffold | `backend/` with `uv` project, `config.py`, structlog, SQLAlchemy engine, Alembic baseline, `/api/health`, session auth, `.env.example`, `docker-compose.yml`, `Dockerfile`, ruff + mypy config, pytest harness with Postgres test DB | `uv run pytest` green; `uv run uvicorn xbot.main:app` serves `/api/health` against local Postgres; `docker compose config` valid | — |
| M1 | Journal, quests, days, CLI | Models + migrations for `days`, `quests`, `journal_entries`, `checkins`, `settings`; routes; `xbot log/quests/today` CLI | API tests for CRUD and the 3-quest limit; `planned` quests never appear as done; CLI round-trip against a running server | M0 |
| M2 | GitHub source | `ingestion/base.py`, `journal.py`, `github.py`, `repo_settings`, `github_commits`, sync route | respx tests: public-only filter, day-bound commit query, diff opt-in truncation, rate-limit handling; one real sync against `aayush2724` shows this week's Portfolio/Vetra commits | M1 |
| M3 | LLM provider + Stages 1–2 | `llm/` (Anthropic + Mock + pricing), `intelligence/schemas.py`, prompts, Stage 1, Stage 2, guards, `pipeline_runs`/`stage_runs`/`activity_facts`/`insights` tables | Unit tests for guards (planned-quest downgrade, unsourced facts dropped, authenticity cap); integration test with `MockLLMProvider` covering `needs_checkin` and `no_post` paths; cost accounting matches usage; a real Stage 1 call succeeds once `ANTHROPIC_API_KEY` exists (user action) | M2 |
| M4 | Stages 3–5 + orchestration | Stage 3, 4, 5; secret scanner; duplicate detector; revise loop; `drafts` table; `DailyPipeline.run()`; pipeline + drafts routes; `no-post` route | Integration test: fixture day → 3 drafts → evaluation → `drafts_ready`; test that a draft with an unknown-fact claim is rejected before the LLM; test that 2 failed revisions end in `no_post`; secret scanner tests | M3 |
| M5 | X publishing | `publishing/` (OAuth PKCE, token refresh, publisher, mock publisher, service), `published_posts`, `x_credentials`, `oauth_states`, routes, history + stats routes | Tests: OAuth callback with respx-mocked token + users/me; refresh when near expiry; duplicate publish blocked by unique constraint under concurrency; 429/5xx retry then fail; `failed`/stale `publishing` retry path; `history` calendar query. Real publish once credentials exist (user action) | M4 |
| M6 | Scheduler + settings + modes | APScheduler jobs, `scheduler_state`, catch-up, settings model + validation, autonomous-mode confirmation guard, interrupted-run recovery | Tests: catch-up runs today's missed evening job and never past days; settings reject bad times; autonomous mode cannot be set without the confirmation string; interrupted runs marked failed on startup | M5 |
| M7 | Dashboard | Vite app: login, Today (quests, journal, check-ins, GitHub sync, run status), Drafts (preview/edit/rate/approve/publish/no-post), History (calendar + streaks), Settings (voice profile, publishing prefs, X connect, repos) | `npm run build` passes type-check; backend serves `frontend/dist`; manual walkthrough of the full Generate → Preview → Edit → Approve → Publish flow with the mock publisher; Vitest for the draft editor char counter | M6 |
| M8 | Hardening + docs + deploy | README (setup, X credential guide, GitHub PAT guide, deploy), failure-scenario tests, Docker image build, final lint/type/test run, `docs/PROGRESS.md` final | `docker compose up` brings up db + app and the dashboard loads; all tests green; ruff + mypy clean | M7 |

## Blockers requiring your action (not needed before M3/M5)

| Needed by | Item | Where it goes |
|---|---|---|
| M3 real run | Anthropic API key | `ANTHROPIC_API_KEY` in `.env` |
| M5 real run | X app Client ID + Secret, OAuth 2.0 enabled, callback `http://127.0.0.1:8000/api/x/callback`, credits purchased | `X_CLIENT_ID`, `X_CLIENT_SECRET` in `.env` |
| M2 (optional) | GitHub fine-grained PAT for 5,000 req/h instead of 60 | `GITHUB_TOKEN` in `.env` |

Everything up to and including the full pipeline and publishing flow is testable with the clearly
labelled mock providers before any of these exist.
