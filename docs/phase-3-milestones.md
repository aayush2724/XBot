# XBot — Phase 3: Milestones

Status: **locked** (revision 2; M3–M5 rows superseded by `phase-2-langgraph-design.md` §13, revision 3). Each milestone is independently testable and ends with a summary
(implemented, files, tests run, limitations, next). Order is strict.

| # | Milestone | Deliverables | Acceptance criteria | Depends on |
|---|---|---|---|---|
| M0 | Scaffold | `backend/` uv project, `config.py`, structlog, async SQLAlchemy engine, Alembic baseline, `/api/health`, session auth, `.env.example`, `docker-compose.yml`, `Dockerfile`, ruff + mypy, pytest harness with Postgres test DB | `uv run pytest` green; `uvicorn xbot.main:app` serves `/api/health` against local Postgres; `docker compose config` valid | — |
| M1 | Days, journal, quests, settings, CLI | Models + migrations for `days`, `quests`, `journal_entries`, `checkins`, `settings`; typed `Settings` model with validation; routes; `xbot log/quests/today` CLI | CRUD tests; 3-quest limit; `planned` quests never appear done; settings reject bad times and bad modes; autonomous mode requires the confirmation string; CLI round-trip | M0 |
| M2 | GitHub source | `ingestion/base.py`, `journal.py`, `github.py`, `repo_settings`, `github_commits`, `github_syncs`, sync route | respx tests: public-only filter, day-bound commit query, **email-or-login author matching**, diff opt-in truncation, rate-limit handling; one real sync against `aayush2724` shows this week's commits | M1 |
| M3 | LLM provider + Stages 1–2 | `llm/` (Anthropic + Mock + pricing), schemas, prompts, Stage 1 with code-assigned fact ids, Stage 2, guards, `pipeline_runs`/`stage_runs` tables with the partial unique index | Guard unit tests (planned-quest downgrade, unsourced facts dropped, bad `source_refs` rejected, authenticity cap); integration with `MockLLMProvider` covering `needs_checkin` and `no_post`; cost accounting; `max_tokens` stop handled; second concurrent run rejected by the index; real Stage 1 call once `ANTHROPIC_API_KEY` exists | M2 |
| M4 | Stages 3–5 + orchestration | Stage 3, 4, 5; sentence-coverage guard; secret scanner; duplicate detector; revise loop; `drafts` table; `DailyPipeline.run()` as background task; startup marks running runs failed; routes for run/drafts/no-post | Integration: fixture day → 3 drafts → `drafts_ready`; uncovered sentence rejected before the LLM; unhedged inferred claim rejected; 2 failed revisions → `no_post`; previous candidates discarded on new run; secret scanner cases; interrupted-run recovery | M3 |
| M5 | X publishing | `publishing/` (OAuth PKCE, row-locked refresh, publisher with `find_recent`, mock publisher, service with claim/retry), `published_posts`, `x_credentials`, `oauth_states`, routes, history + stats | Tests: OAuth callback (respx); refresh under lock with single-use token saved before use; `invalid_grant` → reconnect, 5xx → backoff; concurrent publish blocked by unique index; **crash after POST before DB write → retry finds the post via `find_recent`, no repost**; **timeout → `find_recent` → no repost**; 429 auto-retry; stale `publishing` claim; history query. Real publish once credentials exist | M4 |
| M6 | Scheduler | Advisory-lock leader, 10-minute catch-up tick with atomic claim, follower mode, `scheduler_state` | Tests: tick runs today's evening job once even when called repeatedly or from two instances; never runs past days; follower never schedules; job skipped before `evening_time` | M5 |
| M7a | Dashboard core | Vite app: login, Today (quests, journal, check-ins, GitHub sync, run status), Drafts (preview/edit/rate/approve/publish/no-post), MOCK badge | `npm run build` type-checks; backend serves `dist`; full Generate → Preview → Edit → Approve → Publish walkthrough with the mock publisher; Vitest for the char counter | M6 |
| M7b | Dashboard history + settings | History calendar + streaks, undecided days, Settings (voice profile, publishing prefs with autonomous confirmation, X connect, repos) | Settings round-trip; calendar renders a month from `/history`; X connect button starts the OAuth flow | M7a |
| M8 | Hardening + docs + deploy | README (setup, X credential guide, GitHub token guide, deploy), Docker image build, final lint/type/test, `docs/PROGRESS.md` final | `docker compose up` serves the dashboard; all tests green; ruff + mypy clean | M7b |

## Blockers requiring your action (not needed before M3/M5 real runs)

| Needed by | Item | Where it goes |
|---|---|---|
| M3 real run | Anthropic API key | `ANTHROPIC_API_KEY` in `.env` |
| M5 real run | X app Client ID + Secret, OAuth 2.0 enabled, callback `http://127.0.0.1:8000/api/x/callback`, credits purchased | `X_CLIENT_ID`, `X_CLIENT_SECRET` in `.env` |
| M2 (optional) | GitHub fine-grained token for 5,000 req/h instead of 60 | `GITHUB_TOKEN` in `.env` |

Everything through the full pipeline and publishing flow is testable with the clearly labelled mock
providers before any of these exist.
