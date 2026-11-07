# XBot — Phase 1: Product Discovery

Date: 2026-11-08 · Status: **confirmed** (open questions resolved, see section 9)

XBot is a single-user, build-in-public assistant. Every day it collects what its owner actually
did (journal entries, GitHub activity, quick check-ins), finds the one thing worth sharing,
drafts 2–3 candidate X posts in the owner's voice, grades them for truthfulness and quality, and
publishes **only after explicit approval**. It never invents activity. On an uninteresting day it
says so and posts nothing.

---

## 1. Environment inspection (facts, verified 2026-11-08)

| Item | Finding | Consequence |
|---|---|---|
| Project dir | `~/Documents/Projects/Xbot`, empty, now `git init` on `main` | Greenfield; no existing conventions to respect |
| Python | 3.14.8, `uv` 0.11 | Backend on Python 3.14; `uv` for deps and venv |
| Node | 20.20, npm 10, bun 1.3 | Frontend tooling available; Next.js 16 scaffolder reachable |
| PostgreSQL | 18.6 **running locally** on 5432 | Dev DB with no Docker needed; Docker Postgres for deploy |
| Valkey (Redis) | installed, **not running** | Confirms we should not depend on Redis in MVP |
| Docker | 29.9 + Compose v5.6, daemon up | Deployment packaging is straightforward |
| GitHub | `gh` logged in as `aayush2724` (scopes: repo, read:org, workflow) | GitHub integration can be tested immediately; app will use its own fine-grained PAT, not the gh keyring token |
| Anthropic credentials | none (`ANTHROPIC_API_KEY` unset, `ant` CLI absent) | **User action required** before the intelligence layer can run for real |
| X credentials | none | **User action required** (developer account + app + credits) |
| TypeSafe (JEV) | `TYPESAFE_API_KEY` unset | Decision hooks fall back to local judgment |

## 2. Product understanding

Three daily questions drive everything: *what did I work on, what did I learn, what is worth
sharing?* The product is therefore a **pipeline with a human gate**, not a chatbot and not a
scheduler that posts whatever it can.

Non-negotiable properties pulled from the brief:

1. **Grounding.** Every factual claim in a draft must trace to a journal entry, a commit, or a
   check-in answer. The system tracks *confirmed / inferred / unknown* explicitly.
2. **Restraint.** Max one post per day. "No post today" is a first-class, recorded outcome.
3. **Approval gate.** Generate → Preview → Edit → Approve → Publish. Autonomous mode exists as a
   setting but is off by default and can never be flipped by the agent itself.
4. **Voice.** Reads like a CS undergrad documenting real growth. No hashtags by default, no
   emoji spam, no motivational filler, no "hook + bullets + CTA" template every time.
5. **Official APIs only.** X API v2 with OAuth 2.0. No scraping, no browser automation.

## 3. Ambiguities and proposed resolutions

| # | Ambiguity in the brief | Proposed resolution (default unless you object) |
|---|---|---|
| A1 | "One post per day" vs "quality over frequency" | At most one. Evening run may end in `NO_POST` with a stored reason. Consistency stats count *decisions made* and *posts published* separately so skipped days don't look like missed days. |
| A2 | What is a "day"? | A calendar date in `Asia/Kolkata`. All entities key on `local_date`. Server stores UTC timestamps. |
| A3 | Morning "daily quests" vs activity | Quests are *intentions*. They are shown to the LLM only as context labelled "planned, not confirmed". A quest becomes an activity only when you mark it done or a journal entry / commit evidences it. |
| A4 | "Weekly progress summaries" category | Data model supports it (posts link to a `local_date`; a week is a range query). Generation of weekly summaries is **post-MVP**. |
| A5 | How much GitHub detail goes to the LLM | Public repos only (Q3). Commit messages, repo name, file paths, and line stats always; diffs opt-in per repo. |
| A6 | "Fits within X's posting limits" | Configurable `max_post_chars`, default 280. If your account has Premium you can raise it. The evaluator rejects over-length drafts. |
| A7 | Threads | Out of MVP. Publisher interface accepts a list of segments so threads can be added without redesign. |
| A8 | "Memory and personalization" | Structured preferences + published-post history + rated drafts fed into prompts. No fine-tuning. Phrase-frequency tracking is simple n-gram counting over published posts. |
| A9 | Who can see the dashboard | Single user. One password from `.env`, HTTP-only session cookie. No multi-user model. |
| A10 | Links in posts | X charges **~13× more** for a post containing a link (see §5). Default: no links. Evaluator flags any URL and the UI asks you to confirm the extra cost. |
| A11 | Evening run when the machine is off | Scheduler stores `last_run` per job in Postgres and runs a catch-up on startup if a run was missed. Depends on open question Q2. |

## 4. MVP boundaries

**In scope (Phases 4–7):**

- Journal: free-text entries, quick updates, morning quests (3 goals), evening check-in questions.
- GitHub source: commits for the day across your repos; per-repo diff opt-in; clearly separated "confirmed" facts.
- Intelligence pipeline, five stages, every stage output persisted with token usage and cost.
- 2–3 drafts per day, evaluator with reject-and-regenerate (bounded to 2 retries).
- X publishing via OAuth 2.0 PKCE, approval gate, idempotency key per day, publish history.
- Dashboard: today view, journal input, insights, drafts (edit/rate/approve), history calendar, consistency stats, settings.
- Scheduler: morning prompt, evening consolidation, in-process, Postgres-backed state.
- Tests for: grounding/eval rejection, duplicate prevention, OAuth token refresh, publish failure paths, scheduler catch-up.
- Docker Compose deployment + README with exact X and GitHub credential steps.

**Explicitly out of MVP (designed for, not built):**

- Media/image posts, threads, scheduled posting at a chosen time.
- Calendar or other productivity integrations.
- Autonomous publishing (setting exists, defaults off, UI shows a warning when enabled).
- Weekly summaries, engagement analytics (reads cost money on X).
- Multi-user, mobile app, notifications beyond the dashboard (email/Telegram are easy later).

## 5. External dependencies and costs

### 5.1 X API v2 (publishing) — pay-per-use

X retired its Free/Basic/Pro tiers during 2026. The official "About X API" page now states that
v2 uses **pay-per-usage pricing: purchase credits, deducted per request.** The official pricing
page itself was not fetchable from here (HTTP 402), so the per-request rates below come from
several independent write-ups that agree with each other; **verify them in the developer console
before buying credits.**

| Operation | Reported rate | XBot usage | Monthly estimate |
|---|---|---|---|
| Create post (text only) | $0.015 | ≤ 31 / month | ≈ $0.47 |
| Create post containing a link | $0.20 | opt-in only | $0 by default |
| Read a post | $0.005 | ~1 per publish (verification) | ≈ $0.16 |

Practical total: **under $1/month** for text-only daily posting. Credits must be pre-purchased
(card on file). There is no free write tier for new developers any more.

Auth: **OAuth 2.0 Authorization Code with PKCE**, scopes `tweet.read tweet.write users.read
offline.access`. Access tokens last 2 hours; `offline.access` yields a refresh token, which XBot
stores encrypted and rotates automatically. `http://localhost:<port>/...` is an accepted callback
for development.

What you must do (exact steps will be in the README):
1. Create a developer account at developer.x.com, create a Project + App.
2. Enable **User authentication settings** → OAuth 2.0, type *Web App*, add the callback URL.
3. Copy `Client ID` and `Client Secret` into `.env`.
4. Buy a small credit pack.

### 5.2 LLM — Anthropic Claude (recommended; provider is abstracted)

Pricing (Anthropic first-party, cached 2026-06-24):

| Model | Input $/1M | Output $/1M |
|---|---|---|
| `claude-opus-5` | 5.00 | 25.00 |
| `claude-sonnet-5` | 2.00 | 10.00 |

Per evening run: five stages, roughly 15k input tokens and 5k output tokens in total, up to 2× on
a regeneration day, plus thinking tokens billed as output.

| Configuration | Est. per run | Est. per month |
|---|---|---|
| Opus 5 everywhere | $0.20–0.40 | **$6–12** |
| Opus 5 for drafting + evaluation, Sonnet 5 for extraction + insight scoring | $0.12–0.25 | $4–8 |
| Sonnet 5 everywhere | $0.08–0.15 | $2.5–5 |

Why Claude first: native structured outputs (`output_config.format` with Pydantic schemas), which
the five-stage pipeline relies on; adaptive thinking for the evaluator; prompt caching for the
stable voice-profile prefix. The `LLMProvider` interface is small (one structured-completion
method) so OpenAI, Gemini, or a local Ollama adapter can be added without touching the pipeline.

What you must do: create an API key at console.anthropic.com and put it in `.env`.

### 5.3 GitHub REST API — free

Fine-grained PAT with `Contents: read` and `Metadata: read` on the repos you want tracked.
Authenticated limit 5,000 req/hour; XBot uses well under 100/day. Private repo *contents* are
never fetched unless you enable diffs for that repo.

### 5.4 Infrastructure

| Component | Dev | Deploy |
|---|---|---|
| Postgres | local 18.6 | Docker container, volume-backed |
| Backend + scheduler | `uv run` | one Docker image |
| Frontend | Vite dev server | static files served by the backend |
| Hosting | your machine | any ~$4–6/month VPS (Hetzner CX22, etc.) or keep it local |

Nothing else. No Redis, no queue, no LangGraph.

## 6. Assumptions

1. Single owner, single X account, single GitHub identity (`GITHUB_USERNAME` is configurable;
   `gh` is logged in as `aayush2724` but your git identity uses a different email, so I will not
   assume the handle).
2. Timezone `Asia/Kolkata`; morning job 08:00, evening job 20:00, both configurable.
3. Posts are plain text ≤ 280 characters unless you raise the limit.
4. The dashboard is used on one or two devices by you alone.
5. A "no post" day is acceptable and expected several times a week.
6. Mock providers exist for development and tests, are named `Mock*`, and are enabled only by an
   explicit `XBOT_USE_MOCK_*` flag that the dashboard displays as a banner.
7. Python 3.14 is new enough that a few libraries may lag; fallback is a 3.13 venv via `uv`.

## 7. Risks and blockers

| Risk | Impact | Mitigation |
|---|---|---|
| X pricing or OAuth rules shift again (they changed 3 times in 2026) | Publishing breaks or costs change | Publisher is isolated behind an interface; rates are config, not code; README dates its claims |
| LLM fabricates or over-claims | Core trust failure | Stage 1 facts carry `confidence`; Stage 5 rejects any claim without a source id; regeneration bounded; human gate |
| Secret leakage in a post (e.g. a commit message with a token) | Public exposure | Regex secret scan + LLM check in Stage 5; private-repo diffs opt-in |
| Machine off at 20:00 | Missed evening run | Catch-up on startup; manual "run now" button |
| Duplicate publish on retry | Two identical posts | Idempotency key = `local_date`; publish row created *before* the API call with status `publishing`; retries only when no `x_post_id` and the last attempt is older than N minutes |
| Token refresh failure | Silent publish outage | Health check surfaces token state on the dashboard; refresh retried with backoff; re-connect flow one click away |

## 8. Preliminary architecture direction (to be finalised in Phase 2)

- **Backend:** Python 3.14, FastAPI, SQLAlchemy 2 (async) + Alembic, Pydantic v2, `httpx`, APScheduler
  (in-process, in-memory job store, Postgres `scheduler_state` table + advisory lock for leadership), `structlog`.
- **Intelligence:** plain Python orchestration of five stages, each a pure function
  `(inputs) -> StructuredOutput` persisted as a `pipeline_stage_run` row. No LangGraph: the graph
  is linear with one bounded loop (evaluate → regenerate), which does not justify the dependency.
- **Frontend:** Vite + React 19 + TypeScript + Tailwind, built to static files and served by
  FastAPI in production. Chosen over Next.js because there is no SSR, SEO, or multi-page routing
  need, and a single runtime simplifies deployment. Easy to swap if you prefer Next.js.
- **Persistence:** PostgreSQL only. Encrypted-at-rest OAuth tokens (Fernet key from `.env`).
- **Packaging:** one `docker-compose.yml` with `db` and `app`.

## 9. Open questions — resolved 2026-11-08

| # | Question | Decision |
|---|---|---|
| Q1 | LLM configuration | **Claude Opus 5 for every stage** (`claude-opus-5`). Provider stays abstracted. |
| Q2 | Where it runs | **Local machine now, VPS later.** Scheduler does catch-up on startup; Docker Compose kept deploy-ready; reverse-proxy work deferred. |
| Q3 | GitHub scope | **Public repositories only.** Private activity is never fetched; the GitHub source filters on `repo.private == false` at the API level. Diffs for public repos are still opt-in per repo. |

## 10. Your action items (not needed until Phase 4/5)

1. Anthropic API key.
2. X developer app with OAuth 2.0 enabled, Client ID + Secret, a credit top-up.
3. GitHub fine-grained PAT scoped to the repos you want tracked.

Sources consulted for X pricing (third-party, cross-checked):
[postproxy.dev](https://postproxy.dev/blog/x-api-pricing-2026/),
[outstand.so](https://www.outstand.so/blog/x-api-pricing),
[blotato.com](https://www.blotato.com/blog/twitter-api-pricing),
[twitterapi.io](https://twitterapi.io/blog/can-you-use-x-api-for-free).
Official confirmation of pay-per-usage: [docs.x.com — About X API](https://docs.x.com/x-api/getting-started/about-x-api).
OAuth 2.0 details: [docs.x.com — Authorization Code with PKCE](https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/authorization-code).
