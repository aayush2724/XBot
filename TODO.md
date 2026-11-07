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

## Phase 4 — Core MVP
- [ ] Backend scaffold, config, DB, migrations
- [ ] Journal + quests + check-ins
- [ ] GitHub source
- [ ] Stage 1 activity understanding
- [ ] Stage 2 insight discovery
- [ ] Stage 3 content strategy
- [ ] Stage 4 generation
- [ ] Stage 5 evaluation

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
