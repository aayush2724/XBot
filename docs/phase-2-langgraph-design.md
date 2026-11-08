# XBot — Intelligence layer on LangGraph (architecture revision 3)

Date: 2026-11-08 · Status: **final draft under owner review; model client decided: Anthropic SDK directly**
Supersedes §6 (pipeline), §7 (approval flow) and the recovery rule in §9 of
`phase-2-architecture.md`. Everything else in that document stands. Advisor-reviewed; the changes
made after review are listed in §14.

Owner's instruction: build the pipeline on LangGraph, and research it completely first. This
document is the research record and the resulting design. No pipeline code exists yet; the
repository is at the M2 commit.

---

## 1. What was verified, and how

Checked on 2026-11-08 against the installed packages on Python 3.14.0 in a scratch environment,
the official docs at docs.langchain.com, installed source, and prototype graphs run against the
project's Docker Postgres.

### 1.1 Versions

| Package | Version | Notes |
|---|---|---|
| `langgraph` | 1.2.20 | 1.0 shipped 2025-10-22 with no breaking changes and a no-breaking-changes commitment until 2.0. Python ≥ 3.10. `langgraph.prebuilt` deprecated; not used. |
| `langgraph-checkpoint-postgres` | 3.2.0 | `AsyncPostgresSaver`; needs `psycopg` 3 and `psycopg-pool`. |
| `langchain-anthropic` | 1.7.10 | `ChatAnthropic` verified as a fallback client (§8). |
| `anthropic` | 2.6.0 | `messages.parse` with `output_format`, `thinking`, `output_config`, `system` verified in rev 2. |

All install and import on Python 3.14.0.

### 1.2 APIs confirmed by introspection

```python
StateGraph(state_schema, context_schema=None, *, input_schema=None, output_schema=None)
.add_node(name, fn, *, retry_policy=None, cache_policy=None, error_handler=None, timeout=None, ...)
.add_conditional_edges(source, path, path_map=None)
.compile(checkpointer=None, *, store=None, interrupt_before=None, interrupt_after=None, name=None)

from langgraph.types import interrupt, Command, Send, RetryPolicy, Durability, StateSnapshot
Command(update=..., goto=..., resume=...)         Send(node, arg)          Durability = "sync" | "async" | "exit"
StateSnapshot = (values, next, config, metadata, created_at, parent_config, tasks, interrupts)
from langgraph.runtime import Runtime             # node signature: fn(state, runtime: Runtime[Ctx])
graph.ainvoke(input | Command | None, config, *, context=..., durability=..., version="v2")
graph.aget_state(config)   graph.aupdate_state(config, values, as_node=None)   graph.aget_state_history(config)
AsyncPostgresSaver.adelete_thread(thread_id)
```

Durability (installed docstring): `"sync"` persists before the next step; `"async"` (default) while
the next step runs; `"exit"` only at graph exit. Node `timeout` raises `NodeTimeoutError` and only
works for async nodes. `error_handler` runs a handler node after a node fails; **it receives only
the state, not the exception** (verified), so it cannot record the error.

### 1.3 Postgres checkpointer

```python
pool = AsyncConnectionPool(dsn, open=False, max_size=4,
                           kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
await pool.open(); saver = AsyncPostgresSaver(pool); await saver.setup()
```

The three kwargs are exactly what `from_conn_string` uses internally. `setup()` creates
`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` and is idempotent.
The DSN is a plain `postgresql://…` string; XBot derives it from `XBOT_DATABASE_URL` by removing
`+psycopg`. `thread_id` must be under 255 characters.

### 1.4 Interrupt semantics (the rule that shapes the design)

`value = interrupt(payload)` persists state and stops; the payload is in
`aget_state(config).interrupts[0].value`. Resume with `ainvoke(Command(resume=value), config)` on
the same thread; the value becomes the return value of `interrupt()`. **On resume the node
re-executes from its first line**, so side effects before the interrupt must be idempotent.
Multiple interrupts in one node are matched by index.

### 1.5 Prototype results

| Mechanic | Result |
|---|---|
| `RetryPolicy(max_attempts=3, retry_on=Flaky)` on a node failing twice | ran 3 times, then succeeded |
| `interrupt()` in an approval node | paused with `next == ("approve",)`, payload available |
| `Command(resume=…)` from a new compiled graph object | continued; earlier nodes not re-run |
| Node raises with `durability="sync"`, then `ainvoke(None, config)` | prior state intact; only the failed node re-ran; 6 checkpoints in history |
| Pool-based saver | identical behaviour to `from_conn_string` |
| `Send` fan-out of 3 with a list reducer | works; **not used** in the final design (§6) |
| `ChatAnthropic` request payload built offline for `with_structured_output(method="json_schema")` with constructor `thinking` and `output_config.effort` | payload contains `thinking: adaptive`, `output_config: {effort, format(json_schema)}`, and the first system block with `cache_control` |

---

## 2. Decision: Graph API over Functional API

Fixed stages, two human gates, two bounded loops: the Graph API makes the topology explicit and
inspectable (`aget_state().next`) for the dashboard. The Functional API hides it.

## 3. Topology

```
START ─► collect ─► understand ─┬─ insufficient, round < 2 ─► ask_checkin ⟂ ─► collect
                                ├─ insufficient, round ≥ 2 ─► finish (no_post)
                                └─ enough / thin ─► discover ─┬─ nothing ─► finish (no_post)
                                                              └─ chosen ─► strategy ─► generate ─► evaluate ─┬─ accepted, approval mode ─► review ⟂ ─┬─ approve ─► publish ─► finish
                                                                                                             │                                       ├─ regenerate (≤ 2) ─► generate
                                                                                                             │                                       └─ reject ─► finish (no_post)
                                                                                                             ├─ accepted, autonomous mode ─► publish ─► finish
                                                                                                             ├─ none accepted, attempt < 3 ─► generate (with hints)
                                                                                                             └─ exhausted ─► finish (no_post)
```

`⟂` marks interrupt nodes. Every edge out of `evaluate` and `review` is a conditional edge or a
`Command(goto=…)`; regeneration never passes through `strategy` again (no re-billing of Stage 3).
`recursion_limit` is set explicitly (50). Autonomous mode is read from preferences in the
context, never from the model.

## 4. State

A `TypedDict` of JSON-only values. Pydantic models are validated at node boundaries and stored
as `model_dump()`. The **`drafts` table is authoritative** for draft text, edits and ratings; state
holds ids only, because the owner edits drafts while the run waits. Usage and cost live in
`pipeline_stage_runs`, not in state.

```python
class PipelineState(TypedDict, total=False):
    run_id: int
    local_date: str
    checkin_round: int                 # written by ask_checkin as state["checkin_round"] + 1
    context_items: list[dict]          # SourceItem dumps
    memory: dict                       # MemoryInputs dump
    sufficiency: str
    facts: list[dict]                  # FactWithId dumps after guards
    selected_insight: dict | None
    insight_reason: str
    strategy: dict | None
    generation_attempt: int            # 1..3, written by evaluate when routing back
    revision_hints: list[str]
    draft_ids: list[str]               # drafts.id for the current attempt, deterministic "{attempt}:{label}"
    accepted_draft_ids: list[str]
    review: dict | None                # last resume payload
    outcome: str | None                # no_post | published | publish_failed | failed
    outcome_reason: str | None
```

A test asserts every node's update survives `json.dumps`.

## 5. Context: dependencies, not state

```python
@dataclass
class PipelineContext:
    db: Database; settings: Settings; prefs: Preferences
    llm: LLMProvider; publisher: Publisher | None
```

Passed as `context=` on every invocation; never checkpointed. Tests inject the mock providers.

## 6. Nodes, side effects, idempotency

| Node | Does | Side effects | Idempotency |
|---|---|---|---|
| `collect` | reads quests, entries, answered check-ins, commits; builds memory inputs | none | pure |
| `understand` | Stage 1 via `llm`, `apply_fact_guards` | one `pipeline_stage_runs` row per model attempt | append-only by design |
| `ask_checkin` | upsert `checkins` keyed `(run_id, question)`, then `interrupt({"kind": "checkin", "questions": [...]})`; resume → `Command(goto="collect", update={"checkin_round": state["checkin_round"] + 1})` | checkins rows | upsert before interrupt, so re-execution creates nothing |
| `discover` | Stage 2, scoring, selection | stage row | append-only |
| `strategy` | Stage 3 | stage row | append-only |
| `generate` | **one** Stage 4 call producing 2–3 drafts (cheaper than fan-out, drafts kept diverse by style directives in the prompt); upserts `drafts` rows keyed `(run_id, attempt, label)` | drafts rows | upsert |
| `evaluate` | code checks then Stage 5 for the current attempt's drafts (concurrently); writes evaluations to `drafts`; routes; bumps `generation_attempt` when looping | drafts rows | upsert |
| `review` | `interrupt({"kind": "review", "run_id", "draft_ids": accepted})`; resume payload `{action, draft_id, feedback, reason}` → `Command(goto=…)`; human regenerations capped at 2 | none before interrupt | nothing to repeat |
| `publish` | `publishing.service.publish(draft_id)` reading `edited_text or text` from the drafts table; no retry policy | claim row + X call | the service's claim-before-network design (rev 2 §7) makes re-execution verify instead of re-post; failure → `outcome = publish_failed`, manual retry via the publishing endpoint, outside the graph |
| `finish` | sets `outcome`, `outcome_reason` | none (the runner reconciles) | pure |

Error handling: nodes do not catch provider errors. A failing node raises to the runner, which
marks the run `failed` with the message and leaves the checkpoint in place so **Retry** can resume
with `None`. `LLMTruncated` is retried once at lower effort inside the provider call; transient
429/5xx/connection errors are retried by the SDK (`max_retries=2`). **No `RetryPolicy` on LLM
nodes**, so there is exactly one retry layer. LLM nodes get `timeout=600`.

## 7. Runner, statuses, threads

`PipelineRunner` owns the compiled graph, the checkpointer and a per-run `asyncio.Lock`. It is the
only caller of the graph.

**Statuses of `pipeline_runs`:** `running`, `waiting_checkin`, `waiting_review` (the former
`drafts_ready`), `published`, `no_post`, `publish_failed`, `failed`, `superseded`, `cancelled`.
Live statuses are `running`, `waiting_checkin`, `waiting_review`. The partial unique index becomes
`UNIQUE (local_date) WHERE status IN ('running','waiting_checkin','waiting_review')`: one live run
per day.

| Operation | Rule |
|---|---|
| `start(local_date, trigger, supersede=False)` | if a live run exists: 409, unless `supersede`, which marks it `superseded` first (its thread is left intact). Insert the run with `thread_id = f"run:{id}"` and `graph_version`, launch the task. The evening scheduled run **skips** when a live run exists and logs why. |
| `resume(run_id, kind, value)` | compare-and-set `UPDATE pipeline_runs SET status='running' WHERE id=:id AND status=:expected RETURNING id`; zero rows → 409. Then check `aget_state().interrupts[0].value["kind"] == kind` (409 on mismatch). Under the per-run lock, launch `Command(resume=value)`. Check-in resume requires all questions answered, or an explicit `skip`. |
| `retry(run_id)` | allowed from `failed`; CAS to `running`; invoke with `None`. |
| `cancel(run_id)` | from any live status; CAS to `cancelled`; a pending interrupt is simply never resumed. |
| `recover()` | leader-only (M6 adds the advisory lock; until then the process is assumed single). For `running` rows at startup: no checkpoint → `failed`; last task error transient (`LLMUnavailable`, connection, timeout) and `recover_attempts < 2` → `None`-resume with the counter incremented; otherwise `failed`. For waiting rows: verify the interrupt is still pending, else reconcile. |
| reconcile after every stream | pending interrupt kind → `waiting_checkin` / `waiting_review`; `outcome` → `no_post` / `published` / `publish_failed`; exception → `failed` with the error text. |

Durability is `"sync"` for every invocation. Threads are not pruned in the MVP (one run a day is
small); `adelete_thread` is available when it matters. A thread from an older `graph_version` is
finished as `failed: superseded by graph vN` at startup rather than resumed.

## 8. LLM calls

The `LLMProvider` protocol and `MockLLMProvider` stay as designed; nodes see only the protocol.

**Decided (owner, 2026-11-08): the Anthropic SDK directly** (`AsyncAnthropic().messages.parse`
with `output_format=Schema`, `thinking={"type": "adaptive"}`, `output_config={"effort": ...}`,
system blocks with one `cache_control`), as verified in rev 2. Reasons: one layer fewer, exact
control of `stop_reason` and `stop_details`, cache-token accounting straight from
`usage.cache_read_input_tokens`, and alignment with Anthropic's own guidance. LangSmith tracing of
model calls, if wanted, is `langsmith.wrappers.wrap_anthropic(client)` behind the
`LANGSMITH_TRACING` flag.

**Verified alternative:** `ChatAnthropic(...).with_structured_output(Schema,
method="json_schema", include_raw=True)`. The payload keeps thinking, effort, format and cache
control (§1.5). If used: read `raw.response_metadata["stop_reason"]` before `parsing_error`; map
`ModelRefusalError` → `LLMRefusal`; price with `input_tokens − cache_read − cache_creation` as
uncached input because LangChain's `usage_metadata.input_tokens` is the total; store
`response_metadata["id"]` as the request reference. Switching between the two is one class.

Cost accounting (both): uncached input, cache reads, cache writes and output are priced
separately from the table in `llm/pricing.py`; a unit test checks the arithmetic.

## 9. Persistence and Alembic

LangGraph owns the four `checkpoint*` tables in the same database. `alembic/env.py` gets an
`include_object` filter that ignores tables whose name starts with `checkpoint`, and M3's
acceptance includes "autogenerate produces an empty diff with those tables present". App tables:
`pipeline_runs` (adds `thread_id`, `graph_version`, `recover_attempts`), `pipeline_stage_runs`,
`drafts`, `checkins.run_id`, later `published_posts`. The LangGraph `Store` API is not used.

## 10. Observability

structlog with `run_id` bound; `pipeline_stage_runs` as the cost and audit ledger; the dashboard
polls `GET /pipeline/runs/{id}`, which merges the row with `aget_state().next` and any pending
interrupt payload. No custom progress streaming in the MVP. LangSmith optional.

## 11. Testing

- Pure: guards, routing functions, pricing arithmetic, JSON-only state updates.
- Graph with `InMemorySaver` + mocks: check-in loop (interrupt → resume → `collect` re-runs →
  `understand` again) with the round cap; no-post; revise loop capped at 3; autonomous bypass;
  review approve / regenerate / reject; a raising node leaves the checkpoint and `retry` resumes
  without repeating `understand`.
- Postgres: one integration test with `AsyncPostgresSaver` on the test DB (interrupt, resume from
  a new compiled graph, `None`-resume after failure); `setup()` idempotent; DSN conversion.
- Runner: double resume → 409; mismatched interrupt kind → 409; second start the same day → 409;
  supersede path; recover marks deterministic failures `failed` and resumes transient ones once.
- Provider: offline request-payload test; cost test with cache tokens.
- API and CLI: run, submit/skip check-ins, retry, review, cancel.

## 12. LangGraph-specific risks

| Risk | Mitigation |
|---|---|
| Concurrent invocations on one thread | status CAS + per-run lock + interrupt-kind check (§7) |
| Renamed nodes or keys strand in-flight threads | `graph_version` column; older threads finished as superseded |
| Re-execution on resume duplicates side effects | idempotency column in §6; tests assert row counts after resume |
| `recover()` loops on a deterministic bug | transient-only, `recover_attempts` cap |
| Alembic tries to drop LangGraph tables | `include_object` filter, tested |
| Non-JSON state | TypedDict of JSON values, tested |
| Two sources of truth | drafts table authoritative; runner reconciles status after every stream |
| Pins | `langgraph>=1.2,<2`, `langgraph-checkpoint-postgres>=3.2,<4`, `anthropic>=2.6,<3`; `langchain-anthropic>=1.7,<2` only if the alternative client is chosen |

## 13. Milestone changes (replace the M3–M5 rows of phase-3-milestones.md)

| # | Milestone | Deliverables | Acceptance |
|---|---|---|---|
| M3 | Provider + graph core + runner | `llm/` (SDK provider, mock, pricing), stage schemas, prompts, guards; graph `collect → understand → ask_checkin ⟂ / discover → finish`; `PipelineRunner` with start/resume(checkin)/retry/recover (single-process); pool checkpointer in lifespan; Alembic filter; tables `pipeline_runs` (+thread_id, graph_version, recover_attempts), `pipeline_stage_runs`, `checkins.run_id`; endpoints run / submit-checkins / skip / retry / cancel / get run; CLI `run`, `answer` | autogenerate empty diff with LangGraph tables present; `setup()` idempotent; DSN conversion; interrupt → resume creates no duplicate check-ins and the round cap holds; double resume 409; second start 409; supersede works; Postgres crash → `recover()` without a repeated `understand`; deterministic failure → `failed`; payload and cache-adjusted cost tests; JSON-only state test; existing 35 tests, ruff, mypy green |
| M4a | Stages 3–5 | `strategy`, `generate` (one DraftSet call), `evaluate` (code checks, secret scan, duplicate check, LLM eval), revise loop, `drafts` table, `review` interrupt node, autonomous bypass | fixture day → 2–3 drafts → `waiting_review`; uncovered claim rejected before the LLM; 2 failed revisions → `no_post`; autonomous path reaches `publish` stub |
| M4b | Review surface | drafts endpoints (list, edit, rate), review endpoint (approve / regenerate / reject) with CAS, regenerate cap, CLI `review` | approve → `publish` with mock publisher; regenerate → new attempt; reject → `no_post`; concurrent approve → one 409 |
| M5 | Publishing | unchanged from rev 2 plus the `publish` node and `publish_failed` + manual retry endpoint | unchanged tests plus "publish node re-execution finds the claim and does not re-post" |

M6 adds the advisory lock and makes `recover()` leader-only. M7a/M7b/M8 unchanged.

## 14. Revision log

- Rev 3 draft (2026-11-08): research record and LangGraph design after the owner's instruction.
- Rev 3 final (2026-11-08, after advisor review and two more verifications): status
  compare-and-set and per-run lock on resume; full status table with a live-status partial index;
  `Send` fan-out replaced by one draft-set call; regeneration wired as conditional edges that skip
  `strategy`; check-in round cap and explicit `recursion_limit`; `recover()` transient-only and
  bounded, leader-only from M6; `drafts` table authoritative, usage removed from state; single
  retry layer (SDK only); no `RetryPolicy` on `publish`, `publish_failed` with manual retry; raw
  Anthropic SDK recommended with `ChatAnthropic` as a verified alternative and the cache-token
  pricing correction; skip/cancel/retry operations and CLI `answer`/`review` added; progress
  streaming and 30-day pruning cut; M4 split into M4a/M4b; `error_handler` rejected after
  verifying it does not receive the exception.
