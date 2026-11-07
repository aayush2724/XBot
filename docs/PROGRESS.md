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
