# XBot — Phase 2: Architecture

Date: 2026-11-08 · Status: draft for advisor review, then locked

Decisions carried in from Phase 1: Claude Opus 5 for every stage, runs on the owner's machine now
with a deploy-ready Docker Compose for a VPS later, GitHub public repositories only, one post per
day maximum, approval-gated publishing.

---

## 1. System overview

```
                ┌──────────────────────────── FastAPI process ─────────────────────────────┐
                │                                                                          │
 Browser ──────►│  api/         (REST + session auth, serves built frontend in prod)        │
 CLI (xbot) ───►│                                                                          │
                │  scheduler/   APScheduler, Postgres-backed state, catch-up on startup     │
                │        │                                                                 │
                │        ▼                                                                 │
                │  ingestion/   ActivitySource[] ──► DayContext                            │
                │     journal, github                      │                               │
                │                                          ▼                               │
                │  intelligence/  S1 understand ─► S2 insights ─► S3 strategy              │
                │                 ─► S4 generate ─► S5 evaluate ─(revise ≤2)─► drafts      │
                │                        ▲ uses llm/ (LLMProvider: Anthropic | Mock)       │
                │                        ▲ uses memory/ (voice profile, history)           │
                │                                          │                               │
                │  publishing/   approve ─► Publisher (X OAuth2 | Mock) ─► published_posts │
                │                                                                          │
                └───────────────────────────────┬──────────────────────────────────────────┘
                                                │ SQLAlchemy 2 / psycopg 3
                                                ▼
                                           PostgreSQL 18
```

One process, one database. The scheduler lives inside the API process so a single `uv run` (or
one container) runs everything. No Redis, no queue, no agent framework.

## 2. Technology choices and why

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Backend | Python 3.14, FastAPI 0.162, Pydantic v2, SQLAlchemy 2.1, Alembic, psycopg 3 | Matches the brief; Pydantic models double as LLM output schemas, so one definition serves validation, persistence, and prompts. |
| LLM | `anthropic` SDK 2.x, `client.messages.parse` with Pydantic schemas, adaptive thinking, prompt caching | Structured outputs remove JSON-repair code. Caching makes the frozen voice/persona prefix ~10× cheaper after the first stage call of the day. |
| Orchestration | Plain Python, one `DailyPipeline` class | The graph is linear with one bounded loop. LangGraph would add a dependency and a checkpointer for no gain. Each stage persists its own row, which is all the "checkpointing" we need. |
| Scheduling | APScheduler 3.11 `AsyncIOScheduler` in the app lifespan + `scheduler_state` table | Lightweight, no broker. Catch-up logic is ours, in ~40 lines, and testable. |
| DB | PostgreSQL only (local 18.6 for dev, container for deploy) | Already running on this machine; JSONB for stage outputs; `UNIQUE(local_date)` gives duplicate prevention at the storage layer. |
| Frontend | Vite 8 + React 19 + TypeScript + Tailwind 4 + TanStack Query, built to static files served by FastAPI | Single-user dashboard needs no SSR or SEO. One runtime in production. Next.js can replace it later without touching the API. |
| Auth | Password from `.env`, signed HTTP-only `SameSite=Strict` cookie, server bound to `127.0.0.1` by default | Single user. A reverse proxy with TLS is added at VPS time, not before. |
| Secrets | `.env` + pydantic-settings; X tokens encrypted with Fernet under `XBOT_SECRET_KEY` | Tokens never sit in plaintext in the DB or logs. |
| Logging | structlog JSON to stdout, redaction processor | Grep-able locally, ship-able later. |
| Tests | pytest + pytest-asyncio, respx for GitHub/X HTTP, `MockLLMProvider` with fixture outputs, Postgres test database | Real DB in tests catches constraint and migration bugs that SQLite would hide. |
| Packaging | `uv` workspace for backend, `npm` for frontend, multi-stage Dockerfile, `docker-compose.yml` with `db` + `app` | Low-cost VPS deploy is `docker compose up -d`. |

## 3. Repository layout

```
Xbot/
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini, alembic/
│   ├── xbot/
│   │   ├── main.py            app factory, lifespan (DB check, scheduler start, static files)
│   │   ├── config.py          Settings (pydantic-settings), timezone, feature flags
│   │   ├── logging.py         structlog setup + secret redaction
│   │   ├── db/                engine, session, models.py (all tables), enums
│   │   ├── ingestion/         base.py (ActivitySource, DayContext), journal.py, github.py
│   │   ├── intelligence/      schemas.py, prompts/*.md, stage1..stage5, guards.py, pipeline.py
│   │   ├── llm/               base.py (LLMProvider, LLMResult), anthropic_provider.py,
│   │   │                      mock_provider.py, pricing.py
│   │   ├── memory/            voice_profile.py, history.py (recent posts, category freq, phrases)
│   │   ├── publishing/        base.py (Publisher), x_oauth.py, x_publisher.py,
│   │   │                      mock_publisher.py, service.py (approve/publish/idempotency)
│   │   ├── scheduler/         jobs.py, catchup.py
│   │   ├── api/               deps.py (auth), routes/ (auth, days, journal, github, pipeline,
│   │   │                      drafts, publish, history, settings, x_auth, health)
│   │   └── cli.py             `xbot` quick-entry CLI (talks to the local API)
│   └── tests/                 unit/, integration/, fixtures/
├── frontend/                  Vite + React + TS + Tailwind
├── docs/                      phase docs, PROGRESS.md
├── Dockerfile, docker-compose.yml, .env.example, README.md, TODO.md
```

Dependency rule: `api → services in each package → db`. `intelligence` depends on `llm` and
`memory` only through interfaces. `publishing` never imports `intelligence`. `ingestion` sources
never call the LLM.

## 4. Data model

All timestamps are `timestamptz` in UTC. Every day-scoped table carries `local_date` (DATE) computed
in `Asia/Kolkata` at write time.

| Table | Key columns | Notes |
|---|---|---|
| `days` | `local_date` PK, `status` (open/consolidated/decided), `decision` (pending/post/no_post), `no_post_reason` | One row per calendar day; created by the morning job or on first write. |
| `quests` | id, `local_date`, `text`, `position` 1–3, `status` (planned/done/dropped), `completed_at` | Intentions only. Status `planned` never counts as an activity. |
| `journal_entries` | id, `local_date`, `kind` (update/worked_on/learned/challenge/plan), `content`, `source` (dashboard/cli), `created_at` | Free text. |
| `checkins` | id, `local_date`, `question`, `answer` nullable, `asked_at`, `answered_at` | Created by Stage 1 when data is insufficient. |
| `repo_settings` | `repo_full_name` PK, `include_diffs` bool, `excluded` bool | Per-repo opt-ins. |
| `github_commits` | `sha` PK, `repo_full_name`, `local_date`, `authored_at`, `message`, `additions`, `deletions`, `files` JSONB, `html_url`, `diff` text nullable | Public repos only, enforced in `github.py`. |
| `github_syncs` | id, `local_date`, `synced_at`, `status`, `commits_found`, `error` | Audit of sync attempts. |
| `pipeline_runs` | id, `local_date`, `trigger` (scheduled/manual), `status` (running/needs_checkin/drafts_ready/no_post/failed), `outcome_reason`, token + cost totals, `started_at`, `finished_at`, `error` | One per run; several runs per day allowed. |
| `pipeline_stage_runs` | id, `run_id`, `stage` 1–5, `attempt`, `status`, `input` JSONB, `output` JSONB, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `duration_ms`, `error` | Full audit trail; dashboard shows it. |
| `activity_facts` | id, `run_id`, `local_date`, `kind`, `text`, `confidence` (confirmed/inferred/unknown), `source_refs` JSONB | Stage 1 output, normalised for querying. |
| `insights` | id, `run_id`, `local_date`, `title`, `what_happened`, `why_interesting`, `fact_ids` JSONB, `scores` JSONB, `similar_post_id` nullable, `rank`, `selected` | Stage 2 output. |
| `drafts` | id, `run_id`, `local_date`, `insight_id`, `category`, `label` (A/B/C), `text`, `edited_text` nullable, `claims` JSONB, `evaluation` JSONB, `status` (candidate/rejected/approved/published/discarded), `rating` 1–5 nullable, `feedback` text nullable | Stage 4 + 5 output. |
| `published_posts` | id, `local_date` **UNIQUE**, `draft_id`, `final_text`, `category`, `x_post_id` nullable, `x_url` nullable, `status` (publishing/published/failed), `attempts`, `last_error`, `published_at` | The uniqueness constraint is the duplicate guard. |
| `x_credentials` | id=1, `x_user_id`, `username`, `access_token_enc`, `refresh_token_enc`, `expires_at`, `scopes`, `connected_at` | Single row. Encrypted. |
| `oauth_states` | `state` PK, `code_verifier`, `created_at` | 10-minute TTL, deleted on use. |
| `settings` | `key` PK, `value` JSONB, `updated_at` | Typed via a `Settings` Pydantic model: `publishing_mode`, `max_post_chars`, `allow_links`, `morning_time`, `evening_time`, `github_username`, `voice_profile`, `topics_to_explore`, `phrases_to_avoid`, `category_cooldown_days`. |
| `scheduler_state` | `job_name` PK, `last_run_local_date`, `last_run_at`, `last_status` | Drives catch-up. |

Derived at read time, not stored: category frequency (from `published_posts`), repeated phrases
(bigram/trigram counts over `published_posts.final_text`), consistency stats.

## 5. Ingestion layer

```python
class ActivitySource(Protocol):
    name: str
    async def collect(self, local_date: date) -> list[SourceItem]: ...

class SourceItem(BaseModel):
    source: Literal["journal", "quest", "checkin", "commit"]
    ref_id: str                 # e.g. "journal:42", "commit:abc123"
    occurred_at: datetime
    content: str                # what the LLM sees
    status: str | None = None   # quest status, so planned ≠ done is visible
    metadata: dict = {}
```

`DayContext` = the union of all `SourceItem`s + memory inputs. Adding a calendar source later is one
new class registered in `ingestion/registry.py`.

**GitHub source algorithm** (public only):
1. `GET /users/{username}/repos?sort=pushed&per_page=50` → keep `private == false`, not excluded,
   `pushed_at` within the local day ± 1 day.
2. For each: `GET /repos/{o}/{r}/commits?author={username}&since=&until=` (day bounds in UTC).
3. For each new sha: `GET /repos/{o}/{r}/commits/{sha}` for stats and file list; include the patch
   only when `repo_settings.include_diffs` is true, truncated to 4 kB per file, 20 kB per commit.
4. Upsert into `github_commits`. Rate limit headers respected; 3 retries with backoff on 5xx/403-abuse.

Commits are presented to the LLM as *evidence of work*, with explicit instruction that a commit
does not imply a feature is complete.

## 6. Intelligence pipeline

Every stage is an async function `(ctx, previous outputs) -> PydanticModel`, called through one
`LLMProvider` method, with inputs and outputs persisted in `pipeline_stage_runs`.

```python
class LLMProvider(Protocol):
    async def complete_structured(
        self, *, system: str, user: str, schema: type[T],
        effort: Literal["low","medium","high","xhigh"] = "high",
        max_tokens: int = 8000, stage: str,
    ) -> LLMResult[T]: ...
# LLMResult: parsed: T, model, usage (input/output/cache_read/cache_write), cost_usd, request_id
```

Anthropic implementation: `client.messages.parse(model="claude-opus-5", system=[{text, cache_control}],
messages=[user], output_format=Schema, thinking={"type":"adaptive"}, output_config={"effort": ...})`.
`stop_reason == "refusal"` is surfaced as a stage failure with the category, not retried. Server-side
refusal fallbacks are **not** enabled in MVP: the content is a developer's own notes, and keeping the
non-beta `parse` path is simpler; this is a one-line change if it ever matters.

Prompt layout for caching: `system` = persona + stage instructions + voice profile (frozen for the
day, ≥512 tokens, one `cache_control` breakpoint). `user` = the day context, serialised with
`sort_keys=True`. No timestamps in `system`.

### Stage 1 — Activity understanding → `ActivityUnderstanding`
- Output: `facts: [Fact{id, kind ∈ {activity, project, technology, concept, problem, discovery, unfinished}, text, confidence ∈ {confirmed, inferred, unknown}, source_refs}]`, `summary`, `sufficiency ∈ {enough, thin, insufficient}`, `checkin_questions: [str]` (≤3).
- Code guard (`guards.py`): a fact whose only sources are quests with status `planned` is forced to
  `confidence = unknown`. Facts with no `source_refs` are dropped and logged.
- If `sufficiency == insufficient` → run status `needs_checkin`, questions stored in `checkins`,
  pipeline stops. After answers arrive, a new run starts from Stage 1.

### Stage 2 — Insight discovery → `InsightSet`
- Output: `insights: [Insight{title, what_happened, why_interesting, fact_ids, scores{technical_value, novelty, relatability, authenticity, relevance} ∈ 1–5, similar_post_id?}]`, `nothing_worth_sharing: bool`, `reason`.
- Input includes the last 30 published posts (text + category + date) so "already shared" is
  judged against real history.
- Code guard: insights citing only `inferred`/`unknown` facts get `authenticity` capped at 2 and are
  never selected. If `nothing_worth_sharing` or the best weighted score < threshold (setting,
  default 3.0) → run ends `no_post` with the reason.

### Stage 3 — Content strategy → `Strategy`
- Output: `category` (enum of the 10 categories), `angle`, `style ∈ {plain_note, mini_story, question_led, before_after, code_mention, reflection}`, `length_target`, `avoid: [str]`, `reasoning`.
- Input includes category counts for the last 14 days and the last 3 categories used. Prompt rule:
  do not pick a category used in the last `category_cooldown_days` unless no other fits.

### Stage 4 — Generation → `DraftSet`
- Output: 2–3 `Draft{label, text, style_note, claims: [{sentence, fact_ids}]}`.
- The `claims` map is what makes grounding checkable: every sentence that asserts something must
  point to fact ids. Input includes voice profile, 5 best-rated past drafts and 3 worst with their
  feedback, phrases to avoid, repeated-phrase list.

### Stage 5 — Evaluation → `Evaluation` per draft
- **Code checks first** (free, deterministic): char count vs `max_post_chars`; URL present and
  `allow_links` false → reject; secret patterns (AWS/GitHub/Slack/OpenAI/Anthropic key shapes,
  JWTs, `password=` etc.) → reject; near-duplicate of a published post (normalised token Jaccard
  > 0.6) → reject; every claim's `fact_ids` must exist and none may be `unknown` → reject.
- **LLM check**: `grounding: [{claim, supported, note}]`, `worth_sharing` 1–5, `sounds_human` 1–5,
  `technically_correct`, `repetitive`, `reveals_sensitive`, `issues`, `verdict ∈ {accept, revise, reject}`,
  `revision_hint`.
- If no draft is `accept`ed: re-run Stage 4 with the revision hints, at most 2 extra attempts.
  Still nothing → `no_post` with reason `quality`.

Run outcomes: `needs_checkin`, `drafts_ready`, `no_post`, `failed`. Token usage and USD cost are
accumulated on the run.

## 7. Publishing

```python
class Publisher(Protocol):
    name: str
    async def publish(self, payload: PostPayload) -> PublishResult   # PostPayload.segments: list[str]
    async def verify(self, external_id: str) -> bool | None
```
`segments` lets threads and media arrive later without changing callers. MVP sends one segment.

**Approve → publish flow** (`publishing/service.py`):
1. `POST /drafts/{id}/publish` → check `publishing_mode`, check draft status `candidate|approved`,
   final text = `edited_text or text`, re-run the code checks from Stage 5 on the final text (the
   user may have edited it).
2. `INSERT published_posts (local_date, draft_id, final_text, status='publishing')`. The unique index
   on `local_date` makes a second concurrent request fail here, before any network call.
3. Call `Publisher.publish`. On success: `status='published'`, store `x_post_id`, `x_url`, mark the
   draft `published`, mark the day `decided/post`.
4. On failure: `status='failed'`, `last_error`, `attempts += 1`. Retry is **manual** from the
   dashboard unless the error is clearly transient (429/5xx), in which case up to 2 automatic retries
   with backoff happen inside the same request. A row in `failed` state can be retried; a row in
   `publishing` state older than 5 minutes is treated as crashed and can be retried.
5. Before any retry, if the X API returned an id or the verify call finds a post with identical text
   in the last hour, mark `published` instead of re-posting.

**X OAuth 2.0 PKCE** (`x_oauth.py`): `GET /api/x/connect` builds the authorize URL with S256
challenge, stores `state` + verifier; `GET /api/x/callback` exchanges the code (confidential client:
Basic auth with client id/secret), fetches `GET /2/users/me`, stores encrypted tokens. Access tokens
are refreshed when < 5 minutes from expiry, under a lock, using the refresh token; a refresh failure
flips the dashboard's X status to "reconnect needed".

Autonomous mode: `publishing_mode = autonomous` publishes the top accepted draft automatically at
the end of the evening run. The setting can only be changed through the settings API with an
explicit confirmation string; no code path in the pipeline or scheduler writes settings.

## 8. Memory and personalisation

Stored in `settings.voice_profile` (editable in the dashboard) plus derived history:

- `voice_profile`: tone notes, sentence-length preference, things to never say, example posts.
- `topics_to_explore`, `phrases_to_avoid`.
- Derived each run: last 30 posts, category counts, top repeated bigrams/trigrams, top/bottom rated
  drafts with feedback.

All of this enters prompts as text. No training, no embeddings in MVP. If history grows large,
retrieval by category/recency keeps the prompt bounded (cap: 30 posts, 8 rated drafts).

## 9. Daily workflow mapping

| Time (IST) | Job | What happens |
|---|---|---|
| 08:00 | `morning` | Ensure `days` row exists; dashboard "Today" shows an empty quest list to fill. |
| any time | — | Journal entries via dashboard or `xbot log "..."`; optional GitHub sync button. |
| 20:00 | `evening` | GitHub sync → pipeline run → status `drafts_ready` / `needs_checkin` / `no_post`. Dashboard shows the outcome. |
| after approval | — | Publish, store, update history. |
| next startup | `catchup` | If today's `evening` has not run and it is past 20:00, run it. Past days are never auto-run; the dashboard lists undecided days. |

## 10. API surface (prefix `/api`, JSON, cookie auth except `/auth/login` and `/health`)

| Method & path | Purpose |
|---|---|
| `POST /auth/login`, `POST /auth/logout` | Password → session cookie |
| `GET /health` | DB, scheduler, X connection, LLM key presence, mock flags |
| `GET /days/today`, `GET /days/{date}` | Full day view |
| `PUT /days/{date}/quests`, `PATCH /quests/{id}` | Set up to 3 quests, mark done/dropped |
| `POST /days/{date}/entries`, `DELETE /entries/{id}` | Journal |
| `GET /days/{date}/checkins`, `POST /checkins/{id}/answer` | Evening check-in |
| `POST /days/{date}/github/sync`, `GET /github/repos`, `PATCH /github/repos/{owner}/{repo}` | GitHub |
| `POST /days/{date}/pipeline/run`, `GET /pipeline/runs/{id}`, `GET /days/{date}/insights` | Pipeline |
| `GET /days/{date}/drafts`, `PATCH /drafts/{id}`, `POST /drafts/{id}/publish`, `POST /days/{date}/no-post` | Drafts and decisions |
| `GET /history?from&to`, `GET /stats/consistency` | Calendar + streaks |
| `GET /settings`, `PUT /settings` | Preferences, voice profile, publishing mode |
| `GET /x/connect`, `GET /x/callback`, `GET /x/status`, `DELETE /x/connection` | X OAuth |

Mutating requests require an `X-Requested-With: xbot` header in addition to the cookie (CSRF guard
that `SameSite=Strict` already mostly covers).

## 11. Security review

| Concern | Control |
|---|---|
| Secrets in repo | `.env` git-ignored; `.env.example` has placeholders only; settings loader refuses to start if `XBOT_SECRET_KEY` is the example value. |
| X tokens at rest | Fernet encryption; key only in `.env`; tokens never logged (structlog redaction on keys matching `token|secret|key`). |
| Dashboard exposure | Binds `127.0.0.1:8000` by default; password required; signed cookie; CSRF header. VPS deploy adds Caddy with TLS. |
| OAuth CSRF | `state` random 32 bytes, stored server-side, single use, 10-minute expiry; PKCE S256. |
| LLM prompt injection via commit messages or journal | Day context is data inside the user message under a clear delimiter; stage prompts instruct that content is untrusted; outputs are schema-constrained so injected text cannot trigger actions. The only action with external effect (publish) is behind the human gate. |
| Secret leakage into posts | Regex scanner + LLM `reveals_sensitive` + user preview. |
| Private code | Not fetched at all (public repos only). Diffs opt-in per public repo. |
| Autonomous mode | Off by default; confirmation string required; pipeline cannot change it; UI banner when on. |
| Mocks in production | Mock providers are selected only by `XBOT_LLM_PROVIDER=mock` / `XBOT_PUBLISHER=mock`; `/health` and the dashboard header show a "MOCK" badge when either is active. |

## 12. Observability

- structlog JSON logs with `run_id`, `stage`, `local_date` bound to every pipeline log line.
- `pipeline_runs` + `pipeline_stage_runs` are the audit trail; the dashboard shows per-stage tokens,
  cost, duration, and the raw output JSON behind a toggle.
- `/api/health` for liveness; a `stats` endpoint for consistency.
- Anthropic `request_id` stored on each stage run for support.

## 13. Known limitations accepted for MVP

- Single process: a crash mid-run leaves `pipeline_runs.status = running`; on startup any run older
  than 30 minutes in that state is marked `failed` with reason `interrupted`.
- No retries across process restarts for publishing (the dashboard retry button covers it).
- Character counting uses X's weighted rule approximately (CJK/emoji count as 2); exact parity with
  X's `twitter-text` library is a later improvement.
