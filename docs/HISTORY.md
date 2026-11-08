# XBot — Project history: what happened and why

This file is the narrative record of the project: every significant event, decision, incident
and change of direction, each with the reason behind it. It complements the other docs:

- `PROGRESS.md` says what each milestone delivered and how it was tested.
- `phase-1-discovery.md`, `phase-2-architecture.md`, `phase-2-langgraph-design.md`,
  `phase-3-milestones.md` hold the plans themselves.
- `HISTORY.md` (this file) says **why** things are the way they are, in time order.

Maintenance rule: append an entry whenever a milestone completes, a decision is made or
reversed, an incident changes the code or the process, or the owner changes direction. Each
entry has **What** and **Why**. Never rewrite old entries; add a later entry that supersedes them.

---

## 2026-11-08

### 1. Kickoff and renaming

**What.** The owner provided a full product brief for a personal build-in-public agent
("DailyQuest") and asked to name it **XBot**. The brief required discovery before code, grounding
(never invent activity), one approved post a day, official X API only, and a phased plan.

**Why.** The owner wants a tool they will use daily, built like an engineering project rather than
a one-shot generation.

### 2. Environment inspection

**What.** Found Python 3.14.8 with `uv`, Node 20 with `bun`, Docker 29 with Compose v5, a local
PostgreSQL 18 on port 5432 that requires a password we do not have, Valkey installed but not
running, the `gh` CLI logged in as `aayush2724`, and no Anthropic or X credentials anywhere.

**Why it mattered.** It fixed the stack (Python backend, Docker available), told us the LLM key
and X app are owner actions, and later forced the decision to run Postgres in Docker.

### 3. X API pricing finding

**What.** Research showed X retired its Free, Basic and Pro tiers during 2026 and moved to
pay-per-use: about $0.015 per post, about $0.20 per post containing a link, about $0.005 per
read. The official pricing page returned HTTP 402 so numbers came from several agreeing
third-party sources plus the official "pay-per-usage, purchase credits" statement.

**Why it mattered.** Daily text posting costs under $1 a month, so cost is not a design driver,
but a link costs roughly 13 times more. Decision: **no links in posts by default**; the evaluator
flags any URL.

### 4. Phase 1 decisions confirmed by the owner

**What.** Q1 Claude Opus 5 for every pipeline stage. Q2 run on the owner's machine now, VPS
later. Q3 GitHub **public repositories only**.

**Why.** Opus for best grounding and voice quality at roughly $6–12 a month. Local-first because
the owner is the only user and a laptop is the realistic host, which is why catch-up scheduling
exists. Public-only so private code never reaches a model provider.

### 5. Architecture revision 1, advisor review, revision 2

**What.** Wrote the architecture (FastAPI, async SQLAlchemy, Postgres, in-process scheduler,
five-stage pipeline, approval-gated publishing, Vite+React dashboard) and milestones, then ran
the mandatory advisor review. It found six real problems, all fixed in revision 2.

**Why each fix.**
- Publish claim is committed before the network call and retries are claimed by a conditional
  update: otherwise a crash rolls back the "publishing" marker and a retry can double-post.
- Failures are split into "provably not sent" (429, connection refused → auto-retry) and "maybe
  sent" (5xx, timeout, duplicate 403 → look up the timeline with `find_recent` before any
  re-post): X may have created the post even when the client saw an error.
- Scheduler leadership through a Postgres advisory lock, a 10-minute catch-up tick with an
  atomic claim, and a partial unique index on live runs: `--reload` or extra workers would
  otherwise double-schedule, and APScheduler silently drops misfired jobs when a laptop sleeps.
- Token refresh under `SELECT … FOR UPDATE` with the new refresh token saved before use: X refresh
  tokens are single-use, so an in-process lock is not enough.
- GitHub author matching by configured emails or login instead of the `author` API filter: the
  filter only matches emails linked to the account, and the owner's git email differs.
- Sentence-level grounding coverage with code-assigned fact ids: the model proposes, code checks.

Vite was chosen over Next.js because a single-user dashboard has no SSR or SEO need and one
runtime simplifies deployment. LangGraph was *not* chosen at this point because the graph looked
linear with one loop; this was reversed later (entry 15).

### 6. KryxAI question

**What.** Mid-task the owner asked whether cloning x.getkryxai.com would be resume-worthy.
Verdict: no; it is a content workspace that cannot publish yet, and content tools are a crowded
portfolio category. Advised keeping XBot's grounding and reliability angle.

**Why recorded.** It started the owner's thinking about resume value that led to entry 11.

### 7. M0 scaffold and its incidents

**What.** Backend scaffold, settings, structlog with secret redaction, async database, Alembic,
session-cookie auth with a CSRF header, health endpoint, Docker Compose, tests. Three incidents:
- Local Postgres needs a password we do not know → **dev and tests use the Compose Postgres on
  port 5433**, switchable by `XBOT_DATABASE_URL`.
- The Postgres container exited on first start: SELinux on Fedora blocked the bind-mounted init
  script directory → added the `:z` mount label and recreated the empty volume.
- `docker compose config` failed because `.env` did not exist → `env_file` marked optional.
- A smoke test hit a port already used by an unrelated local Python server → smoke tests pick a
  free port programmatically.

### 8. M1 journal, quests, check-ins, preferences, CLI

**What.** Day model created lazily, three quests with status preserved on re-submit, journal
entries, check-in answers, typed preferences with the autonomous-mode confirmation phrase, CLI.
Two test-found bugs: whitespace-only content passed the length check (added a blank-content
validator), and validation errors contained a non-serialisable context object (dropped it).

**Why the autonomous phrase.** The brief forbids silently switching to autonomous posting. The
setting can only change through the API with an explicit confirmation string, and no pipeline
code path writes settings.

### 9. M2 GitHub ingestion and the misplaced-files incident

**What.** Ingestion contracts, journal source, GitHub client and sync (public-only filter, day
window converted from IST to UTC, email-or-login author match, diff opt-in per repo, rate-limit
detection), repo settings, commits in the day view. A real sync found 1 commit on 2026-11-07 and
5 on 2026-11-05 across Portfolio and Vetra.

**Incident.** Four ingestion files were written to the repository root instead of `backend/`
because two shell commands ran in parallel and one changed the shared working directory. Fix:
every script now uses absolute paths. The files were moved into place before the M2 commit.

### 10. Owner paused the build to research direction

**What.** The owner interrupted: "dont build this yet, lets first research about similar but
better agentic ai projects which can be resume worthy in saas companies." The build stopped at
once; the half-written M2 was left uncommitted.

**Why.** The owner's primary goal for side projects is standing out to SaaS AI-engineering hiring
teams. Research found that those teams screen for eval design, cost modelling, MCP integration,
orchestration failure modes, OWASP LLM06 (excessive agency), and observability, and that
feature-list clones score poorly.

### 11. Project-direction research and the Engineering Auditor plan

**What.** Scored candidate projects against the hiring screens. Top two: an MCP agent security
gateway with a red-team harness, and an **Engineering Auditor** (agentic SAST triage measured on
SastBench, OpenSSF CVE Benchmark and eyeballvul). The owner chose the auditor. Its plan was
written to `~/Documents/Projects/engineering-auditor/PLAN.md`, advisor-reviewed, and revised
(input parity to avoid label leakage, single-commit fetch, repo-level dev/test split,
compare against the paper under matching conditions, the SastBench release found at
RivalSecurity/sastbench with published SOTA MCC 0.148).

**Why it is in this file.** It explains why XBot's resume value is no longer a consideration: the
auditor is the portfolio flagship; XBot is for daily use.

### 12. Owner resumed XBot for personal daily use

**What.** "in this chat, i'd like to continue with the XBot for my personal day to day use."
The misplaced M2 files were moved, M2 was finished, verified against live GitHub, and committed.

**Why.** Both projects continue with different purposes. XBot is now optimised for usability, not
for impressing anyone.

### 13. Owner redirected the pipeline to LangGraph, research first

**What.** As the plain-Python pipeline (M3) was about to be written, the owner said: "first
research completely to build perfectly using langgraph before starting to build it." All M3 code
writing stopped; the partially written files were rejected before they existed on disk.

**Why.** The owner wants LangGraph in this project and prefers deep research to iterating in
code. This reversed the revision-2 decision against LangGraph (entry 5).

### 14. LangGraph research record

**What.** Verified on Python 3.14: `langgraph` 1.2.20, `langgraph-checkpoint-postgres` 3.2.0,
`langchain-anthropic` 1.7.10. Introspected every API the design uses rather than recalling it.
Ran prototype graphs against the project's Docker Postgres: retry policy, `Send` fan-out,
interrupt and resume from a new graph object, crash recovery by invoking with `None`, pool-based
saver. Found that nodes re-execute from their first line on resume (so pre-interrupt side effects
must be idempotent), that the node `error_handler` never receives the exception (so it is
useless for recording failures), and that `ChatAnthropic` keeps thinking, effort, JSON-schema
format and cache control in the real request payload.

**Why so much verification.** LangGraph's 2025–2026 API churn makes recalled code unreliable;
every claim in the design cites an installed signature, a doc page or a prototype result.

### 15. Design revision 3 after advisor review

**What.** The design doc `phase-2-langgraph-design.md` went through advisor review, which found a
race on resumes, an ambiguous status model, an unbounded check-in loop, stacked retry layers, and
a parallel draft fan-out that cost three calls where one suffices. Revision 3: compare-and-set
status transitions plus a per-run lock; a full status table with a live-status partial unique
index; one draft-set call; regeneration wired as conditional edges that skip strategy; check-in
round cap of 2; recovery transient-only and bounded; the `drafts` table authoritative; a single
retry layer (the SDK's); progress streaming and 30-day pruning cut.

**Owner decisions.** Model client: the **Anthropic SDK directly** (one layer fewer, exact control
of stop reasons and cache tokens; `ChatAnthropic` kept as a verified alternative). The owner
reviewed the document before giving the go-ahead.

### 16. M3 built on LangGraph

**What.** Provider package, Stage 1 and 2 with guards, the graph `collect → understand →
ask_checkin ⟂ / discover → finish`, the runner with the Postgres checkpointer, tables and
migration 0004, Alembic filter for LangGraph's tables, endpoints and CLI commands, 21 new tests
(56 total). Live walk with the mock model from `xbot run` through answered check-ins to
`insights_ready`.

**Incidents and why they happened.**
- The project's pre-command safety hooks block shell commands containing destructive keywords
  and file edits without stated facts; the test-fixture change that clears LangGraph's tables
  between tests had to be made with the file-edit tool after presenting the facts. Reason: the
  fixture must clear those tables because run ids restart at 1 between tests and a reused id
  must never resume a thread left by an earlier test. (Thread ids also carry a random suffix for
  the same reason.)
- A test inserted a synthetic run for a day with no `days` row and hit the foreign key; the test
  now creates the day first. The constraint is correct and stays.
- Ruff asked for the PEP 695 generic syntax and no `timeout` parameter names on async functions;
  both were adopted rather than suppressed.
- The prototype scratch database `xbot_lg` was dropped after M3; it held no project data.

### 17. This file

**What.** The owner asked for "a file which has all the information of whatever has happened
with why it has happened." `docs/HISTORY.md` was created with the entries above and the
maintenance rule at the top; a memory note ensures future sessions keep it current.

### 18. Commit-message attribution removed

**What.** The owner asked that commit messages carry no "Co-Authored-By: Claude" trailer. All ten
existing commits were rewritten locally to drop the line (messages only; trees unchanged), and
new commits are written without it. The repository had no remote, so the rewrite affected nothing
else.

**Why.** The owner wants the project history to read as their own work log.

### 19. First push to GitHub

**What.** The owner had already created `github.com/aayush2724/XBot` as a **public** repository.
Before pushing, the full history was scanned for key-shaped strings (none found) and the owner's
email in a GitHub-sync test fixture was replaced with a placeholder. `main` (13 commits) was
pushed and now tracks `origin/main`.

**Why public is acceptable.** The only personal detail in the history is the owner's git email,
which is also the author email on every commit and on their other public repositories, so there
was nothing further to scrub. Secrets live only in the git-ignored `.env`. The advice to start
private stands as a preference, not a requirement; the owner chose public.

### 20. M4a: the pipeline reaches the review gate

**What.** Stages 3 to 5, the revise loop, the drafts table, the review interrupt and the
autonomous bypass were built on the M3 graph. 73 tests pass.

**Why these shapes.**
- One draft-set call instead of a parallel fan-out: the advisor showed it costs a third as much
  and the style directives in the prompt keep the drafts distinct (design rev 3).
- Deterministic checks run before the model: a leaked secret, an over-long post or a sentence
  with no supporting claim should never cost a model call or reach the owner.
- The `drafts` table is authoritative and the graph holds ids only, because the owner edits and
  rates drafts while the run waits at the interrupt.
- `publish` ends as `publish_failed` with an explicit reason when no publisher is configured,
  rather than pretending to post; M5 replaces the reason with a real publication.

**Incidents.** The M3 tests asserted an `insights_ready` terminal state that stopped existing
once the graph continued past Stage 2; the assertions moved to `waiting_review` and the status
was removed. Two tests then failed because the mock's default fact text carried a newline from
the check-in "Q/A" format, which the sentence splitter treated as two sentences; the mock now
emits single-line text, and the coverage rule also accepts a fragment contained in a claimed
sentence so real multi-line posts are not rejected unfairly.

### 21. M4b: the owner can review, edit, rate and decide

**What.** Draft endpoints, the review endpoint and CLI commands were added on top of the M4a
graph; 79 tests pass.

**Why these rules.**
- Text edits are allowed only while the run waits for review: after approval the text is what
  was (or will be) published, and rewriting it would break the audit trail. Ratings and feedback
  stay editable forever because they feed future prompts.
- Edited text is re-checked for length, links and secrets at approval time, but not for claim
  coverage: the owner's own words are the ground truth, while the model's words needed proof.
- The approval endpoint resumes the graph through a compare-and-set because the advisor's
  review of the design showed that a double click or a slow network would otherwise drive one
  thread twice.

### 22. M5: publishing with no path to a double post

**What.** The publishing layer, X OAuth and the real `publish` node were built; 98 tests pass.

**Why it looks like this.**
- Claim before network: the advisor's review of revision 1 showed that an uncommitted
  "publishing" marker rolls back on a crash and a retry can double-post. The claim is committed
  first; the unique day index stops a concurrent second claim before any request.
- Two failure classes: a 429 or a refused connection means X never created the post, so a retry
  is safe; a 5xx, a timeout or a "duplicate content" 403 means X may have, so the service looks
  at the owner's timeline first and records what it finds instead of posting again.
- Refresh under a row lock with the new token committed before use: X refresh tokens are
  single-use, so two concurrent refreshes with the same token would lock the owner out.
- The `needs_reconnect` flag is committed before the error reaches the caller, because the
  first implementation raised inside the transaction and the flag silently rolled back; a test
  caught it.
- Edited text is what gets posted (`edited_text or text`), and the day is marked `post` only
  after X confirms.

**Test expectation changes.** Earlier milestones asserted `publish_failed` because no publisher
existed; with the mock publisher configured in tests, approval now ends in `published`, and
those assertions were updated rather than kept as historical artefacts.

## 2026-11-09

### 23. Model provider: OpenRouter with Gemini 3.5 Flash-Lite

**What.** The owner asked whether a free key could replace Anthropic, then compared free and
low-cost OpenRouter models, and chose `google/gemini-3.5-flash-lite`. An OpenRouter provider was
added beside the Anthropic one; switching is a `.env` change.

**Why this model.** Pulled OpenRouter's live model list: of 461 models, 16 were free, and only a
few free ones enforce JSON schemas; free endpoints also carry daily caps and may log prompts
(here, journal notes). Among paid models with structured-output support, XBot's volume (about
20k input and 6k output tokens a day) makes nearly everything cheap; Flash-Lite at roughly $0.60
a month was the owner's balance of quality and cost. Claude Sonnet 5 (about $3) and Opus 5
(about $7.50) remain one setting away.

**Why the adapter looks like this.** Gemini's structured outputs are stricter than Anthropic's
about schema shape, so a translator inlines references and closes every object. OpenAI-style
usage counts cached tokens inside prompt tokens, so they are subtracted before pricing (the same
double-count the advisor caught for the LangChain path). A pricing row was added because the
unknown-model fallback prices at Opus rates and would have overstated cost about fifteen-fold.
If strict schemas are ever rejected, JSON mode plus Pydantic validation keeps the pipeline
working instead of failing.

**Incident.** The local date rolled over to 2026-11-09 during the work, which broke a CLI test
that seeded drafts for a fixed date while the CLI asks the server for "today". That test now
uses the real local date.

### 24. Live verification of OpenRouter, and a bug only the live API could show

**What.** With the owner's OpenRouter key in `.env`, each stage schema was sent to Gemini 3.5
Flash-Lite once with clearly synthetic evidence. The first run failed at Stage 2; after the fix
all five stages passed in strict mode for about two cents.

**Why it failed.** The strict-schema translator removed `title` keys to drop Pydantic's
metadata, but it also removed the *field* `Insight.title`. The unit test shared the same blind
spot, walking field-name maps as if they were schema nodes, so mocked tests passed. This is
exactly the gap the advisor flagged: mocked HTTP cannot prove a provider accepts the schemas.
The translator and the test now treat `properties` maps as field names.
