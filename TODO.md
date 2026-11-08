# XBot — TODO

Legend: [ ] pending · [~] in progress · [x] done · [!] blocked on user

## Phase 1 — Product discovery
- [x] Inspect environment (Python 3.14, Node 20, Postgres 18 local, Docker, gh auth)
- [x] Verify X API pricing model and OAuth 2.0 flow
- [x] Write `docs/phase-1-discovery.md`
- [x] Confirm Q1 (Opus 5 everywhere), Q2 (local now, VPS later), Q3 (public repos only)

## Phase 2 — Architecture
- [x] System architecture + module boundaries (`docs/phase-2-architecture.md`)
- [x] Data model
- [x] API contracts
- [x] AI pipeline design
- [x] Security review
- [x] Advisor review; 6 findings folded in (rev 2), plan locked

## Phase 3 — Implementation planning
- [x] Milestones with deliverables, dependencies, acceptance criteria (`docs/phase-3-milestones.md`)

## Phase 4 — Core MVP (pipeline on LangGraph per docs/phase-2-langgraph-design.md)
- [x] M0 Backend scaffold, config, DB, migrations, auth, health, compose (8 tests)
- [x] M1 Journal + quests + check-ins + preferences + CLI (16 tests)
- [x] M2 GitHub source: public-only sync, email-or-login author match, diff opt-in, repo settings (11 tests, real sync verified)
- [x] LangGraph research: versions, signatures, prototypes (interrupt/resume, crash recovery, pool saver), advisor review
- [x] D4 model client: Anthropic SDK directly
- [x] Owner reviewed the LangGraph design; go-ahead given
- [x] M3 provider + graph core (collect, understand, ask_checkin ⟂, discover) + runner + Postgres checkpointer (21 new tests, 56 total)
- [x] M4a strategy, generate (one draft-set call), evaluate (code checks + model), revise loop, drafts table, review interrupt, publish stub (17 new tests, 73 total)
- [ ] M4b review and drafts endpoints, CLI review

## Phase 5 — Publishing
- [ ] X OAuth 2.0 PKCE connect + token refresh
- [ ] Approval workflow + publish + history
- [ ] Duplicate prevention + failure handling

## Phase 6 — Dashboard
- [ ] Frontend scaffold
- [ ] Today / journal / drafts / history calendar / settings

## Phase 7 — Testing and deployment
- [ ] Critical-path tests, failure scenarios
- [ ] Docker Compose, README, credential setup guide
