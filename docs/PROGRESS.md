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

## 2026-11-08 — M3 complete: LLM provider, graph core, runner

**Implemented.** `xbot/llm/` (provider protocol, Anthropic SDK provider with adaptive thinking,
effort, one cache breakpoint and typed errors; mock provider with scripted outputs and
`[MOCK]`-labelled defaults; pricing with separate cached and uncached rates; factory).
`xbot/intelligence/` (Stage 1 and Stage 2 schemas, prompts, guards, prompt assembly, `call_stage`
persisting every attempt with a truncation retry at lower effort, the LangGraph graph
`collect → understand → ask_checkin ⟂ / discover → finish` with `Command` routing and a check-in
round cap of 2, and `PipelineRunner` owning the compiled graph, the psycopg-pool Postgres
checkpointer, per-run locks, compare-and-set status transitions, start/resume/retry/cancel/
recover and post-stream reconciliation). Tables `pipeline_runs` (thread id, graph version,
recover attempts, live-status partial unique index), `pipeline_stage_runs`, `checkins.run_id`
(migration 0004). Alembic ignores LangGraph's `checkpoint*` tables. Endpoints for run, run
detail (with pending-interrupt payload and next nodes), list, submit/skip check-ins, retry,
cancel. CLI `run`, `status`, `answer`, `submit`, `skip`, `retry`. Health reports the model and
runner.

**Tests.** 56 passing (21 new): provider payload (cache breakpoint, thinking, effort, schema),
refusal/truncation/empty-output mapping, cache-aware pricing; guard rules; DSN conversion;
empty day → check-ins → resume with no duplicate check-in rows; round cap → no_post; skip; nothing
worth sharing → no_post; second start 409 and supersede; concurrent resume → one 202 one 409;
failure keeps the checkpoint and retry re-runs only the failed node; startup recovery resumes a
transient failure once and fails a run with no checkpoint; cancel frees the day; every checkpoint
is JSON-only; `alembic check` reports no drift with LangGraph tables present; `setup()` twice.
ruff and mypy strict clean.

**Live run (mock model).** `xbot run` on an empty day → two check-in questions → `xbot submit`
refused until answered → answered → resumed → `insights_ready` with two succeeded stage rows.

**Limitations.** Runs end at `insights_ready` until M4a adds strategy, generation and
evaluation. Recovery is single-process until M6 adds the leader lock. No real model call has
been made yet (no API key); the provider's request shape is covered by the payload test.

**Next.** M4a: strategy, generate (one draft-set call), evaluate with code checks, revise loop,
`drafts` table, `review` interrupt node, autonomous bypass.

## 2026-11-08 — M4a complete: strategy, generation, evaluation, review gate

**Implemented.** Stage 3 `strategy` (category, angle, style, length target), Stage 4 `generate`
(one call producing two or three drafts, each sentence mapped to a claim with cited fact ids
and a hedged flag), Stage 5 `evaluate` (deterministic checks first: weighted length with links
counted as 23, link policy, secret patterns, near-duplicate against published posts, sentence
coverage and claim soundness; then one model evaluation per surviving draft), the revise loop
(at most two automatic revisions, so three attempts), the `drafts` table (authoritative for
text, edits and ratings; unique per run, attempt and label; earlier attempts and earlier runs'
unreviewed drafts are discarded), the `review` interrupt (approve / regenerate with feedback,
capped at two / reject), the autonomous bypass, and a `publish` node that approves the chosen
draft and ends the run as `publish_failed` with "no publisher is configured" until M5 wires the
publisher. Rated drafts feed Stage 4 as liked and disliked examples. Run detail now includes the
drafts. Migration 0005. The unreachable `insights_ready` status was removed.

**Tests.** 73 passing (17 new): weighted length, URL and secret detection, token-Jaccard
duplicates, sentence splitting, coverage rules (uncovered sentence, unknown fact id, unhedged
inferred claim, multi-line sentence covered by one claim); full run to `waiting_review` with two
accepted drafts and six stage rows; code checks reject an uncovered and a leaky draft before any
model call; an insight with no confirmed fact is refused before drafting; three failed attempts
end in `no_post` with revision hints carried into the next prompt; autonomous mode skips review
and reaches publish; a superseding run discards the previous run's drafts; review approve,
reject, regenerate-with-feedback and the regeneration cap; approving a non-accepted draft id
fails safely. ruff and mypy strict clean.

**Limitations.** No publisher yet, so approval ends in `publish_failed` by design until M5. The
review and draft endpoints arrive in M4b; review routing is tested at the graph level. Recent
published posts and category cooldown inputs are empty until M5 provides them.

**Next.** M4b: drafts endpoints (list, edit, rate), review endpoint with compare-and-set, CLI
review, then M5 publishing.

## 2026-11-08 — M4b complete: review surface

**Implemented.** `GET /api/days/{date}/drafts` and `PATCH /api/drafts/{id}` (edit text only while
the run waits for review and the draft is still a candidate; rating 1–5 and feedback any time;
reverting to the original clears the edit). `POST /api/pipeline/runs/{id}/review` with
`approve | regenerate | reject`: validates the action against the pending interrupt, requires
the approved draft id to be one of the accepted ids, re-runs the final-text checks on the edited
text (length, links, secrets, duplicates; coverage is skipped because the owner's words are the
ground truth) and returns 422 on failure, then resumes the graph under a compare-and-set so only
one caller wins (409 for the loser). CLI: `drafts`, `edit`, `rate`, `review approve|regenerate|
reject`.

**Tests.** 79 passing (6 new): list/edit/rate rules, concurrent approve → one 202 one 409,
post-approval edit refused while rating still works, approve validation (missing id, foreign id,
too-long edit, link in edit) with the run staying reviewable, regenerate with feedback → attempt
2 with attempt 1 discarded → reject → `no_post`, review on a run waiting for check-ins → 409,
CLI round trip through drafts, rate, edit and approve. ruff and mypy strict clean.

**Live walk (mock model).** Journal → run → `xbot drafts` → rate → edit → `xbot review approve`
→ `publish_failed` ("no publisher is configured"), which is the expected end until M5.

**Limitations.** Approval still ends in `publish_failed` until M5 wires the publisher. Duplicate
checks against published posts receive an empty list until M5.

**Next.** M5: publishing (X OAuth 2.0 PKCE, row-locked token refresh, publisher with
`find_recent`, mock publisher, claim-before-network service, `publish` node, history and
consistency endpoints).
