# XBot — Phase 2: Architecture

Date: 2026-11-08 · Status: **locked** (revision 2, after advisor review; see §14). **§6, §7 and the recovery rule in §9 are superseded by `phase-2-langgraph-design.md` (revision 3, LangGraph).**

Decisions carried in from Phase 1: Claude Opus 5 for every stage, runs on the owner's machine now
with a deploy-ready Docker Compose for a VPS later, GitHub public repositories only, one post per
day maximum, approval-gated publishing.

---

## 1. System overview

```
                ┌──────────────────────────── FastAPI process (1 worker) ──────────────────┐
                │                                                                          │
 Browser ──────►│  api/         (REST + session auth, serves built frontend in prod)        │
 CLI (xbot) ───►│                                                                          │
                │  scheduler/   APScheduler (in-memory jobs) + Postgres advisory lock       │
                │               leader; 10-min catch-up tick; state in scheduler_state      │
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
                                                │ SQLAlchemy 2 async / psycopg 3
                                                ▼
                                           PostgreSQL 18
```

One process, one database. The scheduler lives inside the API process so a single `uv run` (or
one container) runs everything. No Redis, no queue, no agent framework. The process is started
with exactly one uvicorn worker; a Postgres advisory lock makes a second accidental instance
(`--workers 2`, `--reload` child, a stray terminal) a passive follower that never schedules.

## 2. Technology choices and why

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Backend | Python 3.14, FastAPI 0.162, Pydantic v2, SQLAlchemy 2.1 **async**, Alembic, psycopg 3 (async) | Verified installing on 3.14 today. Async end to end: LLM and HTTP calls are the long waits, and one event loop handles API, scheduler, and pipeline without thread hand-offs. Sessions are short-lived: **never held across an LLM or HTTP call**. |
| LLM | `anthropic` SDK 2.6, `client.messages.parse` with Pydantic schemas, adaptive thinking, `max_tokens=16000` | Structured outputs remove JSON-repair code. `stop_reason == "max_tokens"` is handled as a stage failure with a retry at `effort="medium"`. Verified today that `parse` accepts `output_format`, `output_config`, `thinking`, `system`. |
| Orchestration | Plain Python, one `DailyPipeline` class, run as an asyncio background task | The graph is linear with one bounded loop. Each stage persists its own row, which is all the checkpointing we need. |
| Scheduling | APScheduler 3.11 `AsyncIOScheduler`, in-memory job store, started inside the FastAPI lifespan (it needs the running loop), `scheduler_state` table, `pg_try_advisory_lock` for leadership | No broker. Laptops sleep through 20:00 and APScheduler silently drops misfired jobs, so the real trigger is a **10-minute catch-up tick** that claims work atomically in Postgres. |
| DB | PostgreSQL only (local 18.6 for dev, container for deploy) | JSONB for stage outputs; unique and partial-unique indexes give duplicate prevention at the storage layer. |
| Frontend | Vite 8 + React 19 + TypeScript + Tailwind 4 + TanStack Query, static files served by FastAPI | Single-user dashboard needs no SSR. One runtime in production. |
| Auth | Password from `.env`, signed HTTP-only `SameSite=Strict` cookie, server bound to `127.0.0.1` | Single user. TLS reverse proxy is added at VPS time. |
| Secrets | `.env` + pydantic-settings; X tokens encrypted with Fernet under `XBOT_SECRET_KEY` | Tokens never sit in plaintext in the DB or logs. |
| Logging | structlog JSON to stdout, redaction processor | Grep-able locally, ship-able later. |
| Tests | pytest + pytest-asyncio, respx for GitHub/X HTTP, `MockLLMProvider` with fixture outputs, Postgres test database | A real DB in tests catches constraint and migration bugs that SQLite would hide. |
| Packaging | `uv` for backend, `npm` for frontend, multi-stage Dockerfile (`tzdata` included), `docker-compose.yml` with `db` + `app` | VPS deploy is `docker compose up -d`. |

## 3. Repository layout

```
Xbot/
├── backend/
│   ├── pyproject.toml, alembic.ini, alembic/
│   ├── xbot/
│   │   ├── main.py            app factory, lifespan (DB check, leader lock, scheduler, static files)
│   │   ├── config.py          Settings (pydantic-settings), timezone, feature flags
│   │   ├── logging.py         structlog setup + secret redaction
│   │   ├── db/                engine, session, models.py, enums
│   │   ├── ingestion/         base.py (ActivitySource, DayContext), journal.py, github.py
│   │   ├── intelligence/      schemas.py, prompts/*.md, stage1..stage5, guards.py, pipeline.py
│   │   ├── llm/               base.py, anthropic_provider.py, mock_provider.py, pricing.py
│   │   ├── memory/            voice_profile.py, history.py
│   │   ├── publishing/        base.py, x_oauth.py, x_publisher.py, mock_publisher.py, service.py
│   │   ├── scheduler/         leader.py, jobs.py, catchup.py
│   │   ├── api/               deps.py, routes/
│   │   └── cli.py
│   └── tests/                 unit/, integration/, fixtures/
├── frontend/
├── docs/
├── Dockerfile, docker-compose.yml, .env.example, README.md, TODO.md
```

Dependency rule: `api → services → db`. `intelligence` depends on `llm` and `memory` only through
interfaces. `publishing` never imports `intelligence`. `ingestion` never calls the LLM.

## 4. Data model

All timestamps are `timestamptz` in UTC. Day-scoped tables carry `local_date` (DATE) computed in
`Asia/Kolkata` at write time. The `days` row is created lazily on first write for that date.

| Table | Key columns | Notes |
|---|---|---|
| `days` | `local_date` PK, `decision` (pending/post/no_post), `no_post_reason`, `decided_at` | One row per calendar day, created lazily. |
| `quests` | id, `local_date`, `text`, `position` 1–3, `status` (planned/done/dropped), `completed_at` | Intentions only. `planned` never counts as an activity. |
| `journal_entries` | id, `local_date`, `kind` (update/worked_on/learned/challenge/plan), `content`, `source`, `created_at` | Free text. |
| `checkins` | id, `local_date`, `run_id`, `question`, `answer` nullable, `asked_at`, `answered_at` | Created by Stage 1 when data is insufficient. |
| `repo_settings` | `repo_full_name` PK, `include_diffs`, `excluded` | Per-repo opt-ins. |
| `github_commits` | `sha` PK, `repo_full_name`, `local_date`, `authored_at`, `author_email`, `author_login`, `message`, `additions`, `deletions`, `files` JSONB, `html_url`, `diff` nullable | Public repos only. |
| `github_syncs` | id, `local_date`, `synced_at`, `status`, `commits_found`, `error` | Audit. |
| `pipeline_runs` | id, `local_date`, `trigger`, `status` (running/needs_checkin/drafts_ready/no_post/failed), `outcome_reason`, token + cost totals, `started_at`, `finished_at`, `error` | **Partial unique index** `(local_date) WHERE status='running'`: one live run per day. |
| `pipeline_stage_runs` | id, `run_id`, `stage` 1–5, `attempt`, `status`, `input` JSONB, `output` JSONB, `model`, token counts, `cost_usd`, `duration_ms`, `request_id`, `error` | Full audit trail. Stage 1 facts and Stage 2 insights live here as JSONB; no separate tables. |
| `drafts` | id, `run_id`, `local_date`, `insight` JSONB snapshot, `category`, `label`, `text`, `edited_text`, `claims` JSONB, `evaluation` JSONB, `status` (candidate/rejected/approved/published/discarded), `rating`, `feedback` | A new run marks the previous run's `candidate` drafts `discarded`. |
| `published_posts` | id, `local_date` **UNIQUE**, `draft_id`, `final_text`, `category`, `x_post_id`, `x_url`, `status` (publishing/published/failed), `attempts`, `last_error`, `updated_at`, `published_at` | The uniqueness constraint is the duplicate guard. |
| `x_credentials` | id=1, `x_user_id`, `username`, `access_token_enc`, `refresh_token_enc`, `expires_at`, `scopes`, `connected_at`, `needs_reconnect` | Single row, encrypted, row-locked during refresh. |
| `oauth_states` | `state` PK, `code_verifier`, `created_at` | 10-minute TTL, single use. |
| `settings` | `key` PK, `value` JSONB, `updated_at` | Typed via a `Settings` model (see §8); lives in M1 because later stages read thresholds from it. |
| `scheduler_state` | `job_name` PK, `last_run_local_date`, `last_run_at`, `last_status` | Drives atomic catch-up claims. |

Derived at read time: category frequency, repeated phrases, consistency stats.

## 5. Ingestion layer

```python
class ActivitySource(Protocol):
    name: str
    async def collect(self, local_date: date) -> list[SourceItem]: ...

class SourceItem(BaseModel):
    source: Literal["journal", "quest", "checkin", "commit"]
    ref_id: str                 # "journal:42", "commit:abc123"
    occurred_at: datetime
    content: str
    status: str | None = None   # quest status so planned ≠ done is visible
    metadata: dict = {}
```

**GitHub source algorithm** (public only):
1. `GET /users/{username}/repos?sort=pushed&per_page=50` plus any `extra_repos` from settings
   (covers org repos). Keep `private == false`, not excluded, `pushed_at` within the local day ± 1 day.
2. For each: `GET /repos/{o}/{r}/commits?since=&until=` **without** the `author` filter (that filter
   only matches emails linked to the account, and the owner's git email differs). Filter locally:
   `commit.author.email in settings.github_author_emails or author.login == username`.
3. For each new sha: `GET /repos/{o}/{r}/commits/{sha}` for stats and files; patch only when
   `include_diffs`, truncated to 4 kB per file and 20 kB per commit.
4. Upsert into `github_commits`. Rate-limit headers respected; 3 retries with backoff on 5xx.

Limitation, documented in the README: the commits endpoint lists the default branch only. Work on
feature branches appears after merge, or can be journaled.

## 6. Intelligence pipeline

Every stage is an async function `(ctx, previous outputs) -> PydanticModel` called through one
`LLMProvider` method. Inputs and outputs are persisted in `pipeline_stage_runs` by the orchestrator
in a short session opened after the call returns.

```python
class LLMProvider(Protocol):
    async def complete_structured(
        self, *, system_shared: str, system_stage: str, user: str, schema: type[T],
        effort: Literal["low","medium","high","xhigh"] = "high",
        max_tokens: int = 16000, stage: str,
    ) -> LLMResult[T]: ...
# LLMResult: parsed: T, model, usage(input/output/cache_read/cache_write), cost_usd, request_id, stop_reason
```

Anthropic implementation: `client.messages.parse(model="claude-opus-5", system=[shared_block,
stage_block], messages=[user], output_format=Schema, thinking={"type": "adaptive"},
output_config={"effort": effort}, max_tokens=max_tokens)`.

- `system_shared` = persona + voice profile + grounding rules (identical for all five stages,
  ≥512 tokens, carries the single `cache_control` breakpoint). `system_stage` = that stage's
  instructions. Because the shared block comes first, stages 2–5 and every regeneration attempt read
  the cached prefix within the 5-minute TTL. If `usage.cache_read_input_tokens` stays 0 in practice
  the marker is removed; nothing else depends on it.
- `stop_reason == "refusal"` → stage failure with category, not retried. `"max_tokens"` → one retry
  at `effort="medium"`, then failure. Server-side refusal fallbacks are not enabled in MVP.
- User message = day context serialised with `sort_keys=True` under a clear "untrusted data" delimiter.

### Stage 1 — Activity understanding → `ActivityUnderstanding`
- LLM output: `facts: [Fact{kind, text, confidence ∈ {confirmed, inferred, unknown}, source_refs}]`,
  `summary`, `sufficiency ∈ {enough, thin, insufficient}`, `checkin_questions` (≤3).
- **Code assigns fact ids** (`F1..Fn`) after validation, so later stages can only cite ids that exist.
- Guards: facts whose only sources are `planned` quests → `unknown`; facts with no `source_refs` →
  dropped and logged; `source_refs` must match real `ref_id`s from the context.
- `insufficient` → run status `needs_checkin`, questions stored, pipeline stops. Answers trigger a new run.

### Stage 2 — Insight discovery → `InsightSet`
- `insights: [Insight{title, what_happened, why_interesting, fact_ids, scores{technical_value,
  novelty, relatability, authenticity, relevance} 1–5, similar_post_id?}]`, `nothing_worth_sharing`, `reason`.
- Input includes the last 30 published posts. Guard: insights citing only `inferred`/`unknown`
  facts get `authenticity` capped at 2 and are never selected. Best weighted score below
  `settings.min_insight_score` (default 3.0) or `nothing_worth_sharing` → `no_post`.

### Stage 3 — Content strategy → `Strategy`
- `category` (10-value enum), `angle`, `style`, `length_target`, `avoid`, `reasoning`.
- Input: category counts for 14 days and the last 3 categories; cooldown rule from settings.

### Stage 4 — Generation → `DraftSet`
- 2–3 `Draft{label, text, style_note, claims: [Claim{sentence, kind ∈ {fact, opinion}, fact_ids,
  hedged: bool}]}`.
- **Coverage guard (code):** the draft text is split into sentences; every sentence must match a
  claim (normalised, fuzzy ≥0.85). A `fact` claim must cite existing ids, none `unknown`; a claim
  whose facts are all `inferred` must be `hedged`. An uncovered sentence rejects the draft before
  the LLM evaluator sees it.
- Input: voice profile, 5 best-rated and 3 worst-rated past drafts with feedback, phrases to avoid.

### Stage 5 — Evaluation → `Evaluation` per draft
- Code checks first: char count vs `max_post_chars`; URL present and `allow_links` false; secret
  patterns; near-duplicate of a published post (token Jaccard > 0.6); the Stage 4 coverage guard.
- LLM check: `grounding: [{claim, supported, note}]`, `worth_sharing`, `sounds_human`,
  `technically_correct`, `repetitive`, `reveals_sensitive`, `issues`, `verdict`, `revision_hint`.
- No `accept` → Stage 4 again with hints, at most 2 extra attempts → else `no_post` (`quality`).

Run outcomes: `needs_checkin`, `drafts_ready`, `no_post`, `failed`. `POST /pipeline/run` inserts the
`running` row (the partial unique index rejects a second concurrent run), schedules an asyncio task,
and returns the run id immediately.

## 7. Publishing

```python
class Publisher(Protocol):
    name: str
    async def publish(self, payload: PostPayload) -> PublishResult      # segments: list[str]
    async def find_recent(self, text: str, since: datetime) -> str | None  # external id if found
```

**Approve → publish flow** (`publishing/service.py`), designed so that no path can create two X posts:

1. Validate: `publishing_mode`, draft status `candidate|approved`, final text = `edited_text or text`,
   re-run the Stage 5 code checks on the final text.
2. **Claim, in its own committed transaction:**
   - first attempt: `INSERT published_posts (local_date, ..., status='publishing')` — the unique
     index fails a concurrent second request before any network call;
   - retry: `UPDATE published_posts SET status='publishing', attempts=attempts+1, updated_at=now()
     WHERE local_date=:d AND (status='failed' OR (status='publishing' AND updated_at < now()-interval '5 min'))
     RETURNING id` — zero rows means someone else holds the claim.
3. Call `Publisher.publish` **outside any DB transaction**.
4. Success → `status='published'`, `x_post_id`, `x_url`; draft `published`; day `post`.
5. Failure handling by class:
   - **429 or connection refused** (request provably never reached X): automatic retry, 2× with backoff.
   - **5xx, timeout, 403 duplicate-content, unknown**: X may have created the post. Call
     `find_recent(final_text, since=started_at - 1h)` (reads the owner's own timeline, ~$0.005).
     Found → mark `published` with that id. Not found → `status='failed'`, `last_error`; retry is
     **manual** from the dashboard and goes through step 2 again.
6. Autonomous mode uses the identical service call; it is not a separate path.

**X OAuth 2.0 PKCE** (`x_oauth.py`): `GET /api/x/connect` builds the authorize URL (S256), stores
`state` + verifier; `GET /api/x/callback` exchanges the code (confidential client, Basic auth),
fetches `GET /2/users/me`, stores encrypted tokens.

**Token refresh** (refresh tokens are single-use):
```
async with session.begin():
    creds = SELECT ... FROM x_credentials WHERE id=1 FOR UPDATE
    if creds.expires_at - now() > 5 min: return creds.access_token      # re-check under the lock
    resp = POST /2/oauth2/token grant_type=refresh_token                 # inside the lock, short call
    on 400 invalid_grant: creds.needs_reconnect = True; commit; raise ReconnectRequired
    on network / 5xx: commit nothing; raise Transient (caller backs off, max 3)
    creds.access_token_enc, creds.refresh_token_enc, creds.expires_at = new values   # saved before use
```
Only `invalid_grant` flips the dashboard to "reconnect needed".

Autonomous mode: `publishing_mode = autonomous` publishes the top accepted draft at the end of the
evening run. The setting changes only through the settings API with the confirmation string
`I UNDERSTAND XBOT WILL POST WITHOUT ASKING`; no code path in the pipeline or scheduler writes settings.

## 8. Memory and personalisation

`settings` keys, typed by a Pydantic model: `publishing_mode`, `max_post_chars` (280),
`allow_links` (false), `evening_time` ("20:00"), `github_username`, `github_author_emails`,
`extra_repos`, `min_insight_score` (3.0), `category_cooldown_days` (3), `voice_profile`,
`topics_to_explore`, `phrases_to_avoid`. Derived each run: last 30 posts, category counts, top
repeated bigrams/trigrams, top/bottom rated drafts with feedback. All enter prompts as text. No
training, no embeddings in MVP.

## 9. Scheduling and daily workflow

| Mechanism | Behaviour |
|---|---|
| Leadership | On startup the process tries `pg_try_advisory_lock(<xbot key>)` on a dedicated connection held for the process lifetime. Winner runs the scheduler; losers log "follower" and serve the API only. `--reload` children and extra workers therefore cannot double-schedule. |
| Startup recovery | Leader marks every `pipeline_runs.status='running'` row `failed` (`interrupted`) and every `published_posts.status='publishing'` row older than 5 minutes stays claimable per §7 step 2. |
| Catch-up tick | Every 10 minutes the leader evaluates: if local time ≥ `evening_time` and `scheduler_state.evening.last_run_local_date < today`, claim with `UPDATE scheduler_state SET last_run_local_date=today, last_run_at=now() WHERE job_name='evening' AND (last_run_local_date IS NULL OR last_run_local_date < today) RETURNING` and, on a returned row, run GitHub sync → pipeline. This replaces both the cron trigger and startup-only catch-up, so a laptop that slept through 20:00 runs at the next tick. |
| Past days | Never auto-run. The dashboard lists undecided past days with a manual "run" button. |
| Morning | No job. The `days` row is created when you first write to today; the dashboard shows the empty quest list. |
| After approval | Publish, store, update history (§7). |

## 10. API surface (prefix `/api`, JSON, cookie auth except `/auth/login` and `/health`)

| Method & path | Purpose |
|---|---|
| `POST /auth/login`, `POST /auth/logout` | Password → session cookie |
| `GET /health` | DB, leader/follower, X connection, LLM key presence, mock flags |
| `GET /days/today`, `GET /days/{date}`, `GET /days/undecided` | Day views |
| `PUT /days/{date}/quests`, `PATCH /quests/{id}` | Up to 3 quests, mark done/dropped |
| `POST /days/{date}/entries`, `DELETE /entries/{id}` | Journal |
| `GET /days/{date}/checkins`, `POST /checkins/{id}/answer` | Evening check-in |
| `POST /days/{date}/github/sync`, `GET /github/repos`, `PATCH /github/repos/{owner}/{repo}` | GitHub |
| `POST /days/{date}/pipeline/run` (202 + run id), `GET /pipeline/runs/{id}` | Pipeline |
| `GET /days/{date}/drafts`, `PATCH /drafts/{id}`, `POST /drafts/{id}/publish`, `POST /days/{date}/no-post` | Drafts and decisions |
| `POST /published/{id}/retry` | Manual retry per §7 |
| `GET /history?from&to`, `GET /stats/consistency` | Calendar + streaks |
| `GET /settings`, `PUT /settings` | Preferences, voice profile, publishing mode |
| `GET /x/connect`, `GET /x/callback`, `GET /x/status`, `DELETE /x/connection` | X OAuth |

Mutating requests require an `X-Requested-With: xbot` header in addition to the cookie.

## 11. Security review

| Concern | Control |
|---|---|
| Secrets in repo | `.env` git-ignored; `.env.example` placeholders; startup refuses the example `XBOT_SECRET_KEY`. |
| X tokens at rest | Fernet; key only in `.env`; structlog redaction on `token|secret|key|password`. |
| Dashboard exposure | `127.0.0.1:8000` default; password; signed cookie; CSRF header. TLS proxy at VPS time. |
| OAuth CSRF | 32-byte `state`, server-side, single use, 10-minute expiry; PKCE S256. |
| Prompt injection via commits/journal | Context is data inside the user message under a delimiter; stage prompts say it is untrusted; outputs are schema-constrained; the only external action is behind the human gate. |
| Secret leakage into posts | Regex scanner + LLM `reveals_sensitive` + preview. |
| Private code | Never fetched. Diffs opt-in per public repo. |
| Autonomous mode | Off by default; confirmation string; pipeline cannot change it; UI banner when on. |
| Mocks in production | Selected only by `XBOT_LLM_PROVIDER=mock` / `XBOT_PUBLISHER=mock`; `/health` and the dashboard show a "MOCK" badge. |
| Double scheduling / double posting | Advisory lock, partial unique index on running runs, unique `published_posts.local_date`, committed claim before network, `find_recent` before any re-post. |

## 12. Observability

structlog JSON with `run_id`, `stage`, `local_date` bound; `pipeline_runs` + `pipeline_stage_runs`
as the audit trail with tokens, cost, duration, Anthropic `request_id`; `/api/health`; the
dashboard shows per-stage cost and raw output behind a toggle.

## 13. Known limitations accepted for MVP

- GitHub: default branch only; org repos only via `extra_repos`.
- Character counting approximates X's weighted rule (CJK/emoji count 2).
- `find_recent` costs one timeline read (~$0.005) per ambiguous failure.
- Prompt-cache benefit is modest (within-run regeneration and same-day re-runs); it is kept
  because it is one marker and is removed if measurement shows no hits.

## 14. Revision log

**Rev 2 (2026-11-08, after advisor review).** Publish claim committed before the network call and
retries claimed by conditional update; failure classes split into "provably not sent" vs
"maybe sent → `find_recent`"; `verify(id)` replaced by `find_recent(text, since)`. Advisory-lock
leadership, 10-minute catch-up tick with atomic claim, partial unique index on running runs,
startup marks all running runs failed. Token refresh under `SELECT … FOR UPDATE`, new refresh token
saved before use, only `invalid_grant` means reconnect. GitHub matching by configured emails or
login instead of the `author` param; default-branch limit documented; `extra_repos` for org repos.
Code assigns fact ids; sentence-coverage guard with `opinion` kind and `hedged` flag; older
candidate drafts discarded on a new run. `max_tokens` 16000 with `max_tokens` stop handling; cache
breakpoint moved to a shared first system block so it can actually hit across stages; pipeline run
as a background task; async SQLAlchemy chosen; APScheduler started in lifespan; `tzdata` in the
image. Cut: `activity_facts` and `insights` tables (JSONB on stage runs), the morning job, the
30-minute interrupted-run threshold.
