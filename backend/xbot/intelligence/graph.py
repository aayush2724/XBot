"""The XBot pipeline as a LangGraph StateGraph (design: docs/phase-2-langgraph-design.md).

Nodes: collect → understand → ask_checkin ⟂ / discover → strategy → generate → evaluate →
review ⟂ / publish → finish.  (⟂ = interrupt)

Rules that keep this graph safe:
- State values are JSON-only. Pydantic models are dumped at node boundaries; the `drafts` table
  is authoritative for draft text, so state carries draft ids only.
- Side effects before an `interrupt()` must be idempotent, because the node re-runs on resume.
- Every exit from a deciding node is a `Command(goto=...)`; regeneration never re-bills strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from xbot.config import Settings
from xbot.db.base import Database
from xbot.db.models import DraftRecord, DraftStatus, PublishStatus
from xbot.ingestion.base import DayContext, SourceItem, collect_day_context
from xbot.ingestion.github import GitHubSource
from xbot.ingestion.journal import JournalSource
from xbot.intelligence import prompting
from xbot.intelligence.checks import code_checks
from xbot.intelligence.guards import apply_fact_guards, score_insights, select_insight
from xbot.intelligence.schemas import (
    ActivityUnderstanding,
    Claim,
    DraftSet,
    Evaluation,
    FactWithId,
    InsightSet,
    Strategy,
)
from xbot.intelligence.stages import annotate_stage, call_stage
from xbot.llm.base import LLMProvider
from xbot.logging import get_logger
from xbot.memory.inputs import load_memory_inputs
from xbot.preferences import Preferences, PublishingMode
from xbot.publishing.base import PublishError
from xbot.publishing.service import AlreadyPublished, PublishInProgress, publish_draft
from xbot.services.days import ensure_checkins

log = get_logger("xbot.graph")

GRAPH_VERSION = "v3"
MAX_CHECKIN_ROUNDS = 2
MAX_AUTO_REVISIONS = 2  # evaluate may send generate back at most twice → 3 attempts
MAX_HUMAN_REGENERATIONS = 2
MAX_DRAFTS = 3
DEFAULT_CHECKIN_QUESTIONS = [
    "What did you actually work on today?",
    "Did you learn or figure out anything, even something small?",
]


class PipelineState(TypedDict, total=False):
    run_id: int
    local_date: str
    checkin_round: int
    checkin_questions: list[str]
    context_items: list[dict[str, Any]]
    memory: dict[str, Any]
    sufficiency: str
    summary: str
    facts: list[dict[str, Any]]
    selected_insight: dict[str, Any] | None
    insight_reason: str
    strategy: dict[str, Any] | None
    generation_attempt: int
    auto_revisions: int
    human_regenerations: int
    revision_hints: list[str]
    draft_ids: list[int]
    accepted_draft_ids: list[int]
    review: dict[str, Any] | None
    published: dict[str, Any] | None
    outcome: str | None
    outcome_reason: str | None


@dataclass
class PipelineContext:
    db: Database
    settings: Settings
    prefs: Preferences
    llm: LLMProvider
    publisher: Any | None = None


Ctx = Runtime[PipelineContext]


def _ctx_from_items(local_date: str, items: list[dict[str, Any]]) -> DayContext:
    return DayContext(
        local_date=date.fromisoformat(local_date),
        items=[SourceItem.model_validate(i) for i in items],
    )


def _facts(state: PipelineState) -> list[FactWithId]:
    return [FactWithId.model_validate(f) for f in state.get("facts", [])]


def _no_post(update: dict[str, Any], reason: str) -> Command[Any]:
    return Command(update={**update, "outcome": "no_post", "outcome_reason": reason}, goto="finish")


# ---- nodes ---------------------------------------------------------------------------------


async def collect(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["understand", "ask_checkin", "finish"]]:
    ctx = runtime.context
    local_date = date.fromisoformat(state["local_date"])
    async with ctx.db.session_factory() as session:
        day_ctx = await collect_day_context(session, local_date, [JournalSource(), GitHubSource()])
        memory = (await load_memory_inputs(session, ctx.prefs)).model_dump(mode="json")
    items = [i.model_dump(mode="json") for i in day_ctx.items]
    update: dict[str, Any] = {"context_items": items, "memory": memory}
    if items:
        return Command(update=update, goto="understand")
    if state.get("checkin_round", 0) < MAX_CHECKIN_ROUNDS:
        update["checkin_questions"] = DEFAULT_CHECKIN_QUESTIONS
        return Command(update=update, goto="ask_checkin")
    return _no_post(update, "no evidence for the day after check-ins")


async def understand(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["ask_checkin", "discover", "finish"]]:
    ctx = runtime.context
    run_id = state["run_id"]
    shared = prompting.shared_system(state.get("memory", {}))
    user = prompting.stage1_user(state["local_date"], state.get("context_items", []))
    result = await call_stage(
        db=ctx.db,
        llm=ctx.llm,
        run_id=run_id,
        stage=1,
        schema=ActivityUnderstanding,
        system_shared=shared,
        user=user,
    )
    s1 = result.parsed
    day_ctx = _ctx_from_items(state["local_date"], state.get("context_items", []))
    facts, report = apply_fact_guards(s1.facts, day_ctx)
    facts_json = [f.model_dump(mode="json") for f in facts]
    await annotate_stage(ctx.db, run_id, 1, {"facts": facts_json, "guards": report.model_dump()})
    update: dict[str, Any] = {
        "facts": facts_json,
        "sufficiency": s1.sufficiency,
        "summary": s1.summary,
    }
    if s1.sufficiency == "insufficient" or not facts:
        if state.get("checkin_round", 0) < MAX_CHECKIN_ROUNDS:
            update["checkin_questions"] = (s1.checkin_questions or DEFAULT_CHECKIN_QUESTIONS)[:3]
            return Command(update=update, goto="ask_checkin")
        return _no_post(update, f"insufficient evidence after {MAX_CHECKIN_ROUNDS} check-in rounds")
    return Command(update=update, goto="discover")


async def ask_checkin(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["collect", "discover", "finish"]]:
    """Idempotent side effect first (upsert), then interrupt. Re-execution on resume is safe."""
    ctx = runtime.context
    questions = list(state.get("checkin_questions") or DEFAULT_CHECKIN_QUESTIONS)
    async with ctx.db.session_factory() as session:
        await ensure_checkins(
            session, date.fromisoformat(state["local_date"]), state["run_id"], questions
        )
    decision = interrupt({"kind": "checkin", "run_id": state["run_id"], "questions": questions})
    action = decision.get("action") if isinstance(decision, dict) else decision
    next_round = state.get("checkin_round", 0) + 1
    if action == "skip":
        if state.get("facts"):
            return Command(update={"checkin_round": next_round}, goto="discover")
        return _no_post({"checkin_round": next_round}, "check-in skipped with no usable evidence")
    return Command(update={"checkin_round": next_round}, goto="collect")


async def discover(state: PipelineState, runtime: Ctx) -> Command[Literal["strategy", "finish"]]:
    ctx = runtime.context
    run_id = state["run_id"]
    shared = prompting.shared_system(state.get("memory", {}))
    facts_json = state.get("facts", [])
    user = prompting.stage2_user(facts_json, state.get("memory", {}), ctx.prefs.min_insight_score)
    result = await call_stage(
        db=ctx.db,
        llm=ctx.llm,
        run_id=run_id,
        stage=2,
        schema=InsightSet,
        system_shared=shared,
        user=user,
    )
    scored, report = score_insights(result.parsed, _facts(state))
    chosen, reason = select_insight(result.parsed, scored, ctx.prefs.min_insight_score)
    await annotate_stage(
        ctx.db,
        run_id,
        2,
        {
            "scored": [s.model_dump(mode="json") for s in scored],
            "guards": report.model_dump(),
            "selected": chosen.model_dump(mode="json") if chosen else None,
            "reason": reason,
        },
    )
    if chosen is None:
        return _no_post({"selected_insight": None, "insight_reason": reason}, reason)
    return Command(
        update={"selected_insight": chosen.model_dump(mode="json"), "insight_reason": reason},
        goto="strategy",
    )


async def strategy(state: PipelineState, runtime: Ctx) -> Command[Literal["generate"]]:
    ctx = runtime.context
    memory = state.get("memory", {})
    shared = prompting.shared_system(memory)
    user = prompting.stage3_user(
        state.get("selected_insight") or {},
        state.get("facts", []),
        memory,
        max_chars=ctx.prefs.max_post_chars,
        cooldown_days=ctx.prefs.category_cooldown_days,
        last_categories=list(memory.get("last_categories", [])),
    )
    result = await call_stage(
        db=ctx.db,
        llm=ctx.llm,
        run_id=state["run_id"],
        stage=3,
        schema=Strategy,
        system_shared=shared,
        user=user,
    )
    strat = result.parsed
    target = max(60, min(strat.target_chars, ctx.prefs.max_post_chars))
    return Command(
        update={
            "strategy": {**strat.model_dump(mode="json"), "target_chars": target},
            "generation_attempt": state.get("generation_attempt") or 1,
            "auto_revisions": state.get("auto_revisions", 0),
            "human_regenerations": state.get("human_regenerations", 0),
            "revision_hints": state.get("revision_hints", []),
        },
        goto="generate",
    )


async def generate(state: PipelineState, runtime: Ctx) -> Command[Literal["evaluate", "finish"]]:
    ctx = runtime.context
    run_id = state["run_id"]
    attempt = state.get("generation_attempt") or 1
    memory = state.get("memory", {})
    shared = prompting.shared_system(memory)
    user = prompting.stage4_user(
        state.get("selected_insight") or {},
        state.get("strategy") or {},
        state.get("facts", []),
        memory,
        attempt=attempt,
        revision_hints=state.get("revision_hints", []),
        max_chars=ctx.prefs.max_post_chars,
        allow_links=ctx.prefs.allow_links,
    )
    result = await call_stage(
        db=ctx.db,
        llm=ctx.llm,
        run_id=run_id,
        stage=4,
        schema=DraftSet,
        system_shared=shared,
        user=user,
    )
    drafts = result.parsed.drafts[:MAX_DRAFTS]
    if not drafts:
        return _no_post({"draft_ids": []}, "the model produced no drafts")
    category = (state.get("strategy") or {}).get("category")
    local_date = date.fromisoformat(state["local_date"])
    ids: list[int] = []
    async with ctx.db.session_factory() as session:
        await session.execute(
            update(DraftRecord)
            .where(
                DraftRecord.run_id == run_id,
                DraftRecord.attempt < attempt,
                DraftRecord.status.in_([DraftStatus.candidate, DraftStatus.accepted]),
            )
            .values(status=DraftStatus.discarded)
        )
        for d in drafts:
            stmt = insert(DraftRecord).values(
                run_id=run_id,
                local_date=local_date,
                attempt=attempt,
                label=d.label[:8] or "A",
                category=category,
                text=d.text.strip(),
                style_note=d.style_note,
                claims=[c.model_dump(mode="json") for c in d.claims],
                status=DraftStatus.candidate,
                evaluation=None,
            )
            upsert = stmt.on_conflict_do_update(
                constraint="uq_drafts_run_attempt_label",
                set_={
                    "text": stmt.excluded.text,
                    "style_note": stmt.excluded.style_note,
                    "claims": stmt.excluded.claims,
                    "status": DraftStatus.candidate,
                    "evaluation": None,
                },
            ).returning(DraftRecord.id)
            ids.append(int((await session.execute(upsert)).scalar_one()))
        await session.commit()
    return Command(update={"draft_ids": ids, "generation_attempt": attempt}, goto="evaluate")


async def evaluate(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["review", "publish", "generate", "finish"]]:
    ctx = runtime.context
    run_id = state["run_id"]
    memory = state.get("memory", {})
    shared = prompting.shared_system(memory)
    facts = _facts(state)
    facts_by_id = {f.id: f for f in facts}
    facts_json = state.get("facts", [])
    recent_texts = [p.get("text", "") for p in memory.get("recent_posts", [])]
    accepted: list[int] = []
    hints: list[str] = []
    async with ctx.db.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(DraftRecord).where(DraftRecord.id.in_(state.get("draft_ids", [])))
                )
            )
            .scalars()
            .all()
        )
        for row in sorted(rows, key=lambda r: r.label):
            claims = [Claim.model_validate(c) for c in row.claims]
            issues = code_checks(
                text=row.text,
                claims=claims,
                facts_by_id=facts_by_id,
                max_chars=ctx.prefs.max_post_chars,
                allow_links=ctx.prefs.allow_links,
                recent_texts=recent_texts,
            )
            if issues:
                row.status = DraftStatus.rejected
                row.evaluation = {"verdict": "reject", "code_issues": issues, "source": "code"}
                hints.extend(issues[:2])
                continue
            ev = await call_stage(
                db=ctx.db,
                llm=ctx.llm,
                run_id=run_id,
                stage=5,
                schema=Evaluation,
                system_shared=shared,
                user=prompting.stage5_user(
                    row.text, row.claims, facts_json, memory, max_chars=ctx.prefs.max_post_chars
                ),
            )
            verdict = ev.parsed
            row.evaluation = {**verdict.model_dump(mode="json"), "code_issues": [], "source": "llm"}
            if verdict.verdict == "accept":
                row.status = DraftStatus.accepted
                accepted.append(row.id)
            else:
                row.status = DraftStatus.rejected
                if verdict.revision_hint:
                    hints.append(verdict.revision_hint)
                hints.extend(verdict.issues[:1])
        await session.commit()

    if accepted:
        goto: Literal["review", "publish"] = (
            "publish" if ctx.prefs.publishing_mode == PublishingMode.autonomous else "review"
        )
        return Command(update={"accepted_draft_ids": accepted, "revision_hints": []}, goto=goto)
    auto = state.get("auto_revisions", 0)
    attempt = state.get("generation_attempt") or 1
    if auto < MAX_AUTO_REVISIONS:
        unique_hints = list(dict.fromkeys(h for h in hints if h))[:6]
        return Command(
            update={
                "generation_attempt": attempt + 1,
                "auto_revisions": auto + 1,
                "revision_hints": unique_hints,
                "accepted_draft_ids": [],
            },
            goto="generate",
        )
    return _no_post(
        {"accepted_draft_ids": []},
        f"no draft passed evaluation after {attempt} attempts",
    )


async def review(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["publish", "generate", "finish"]]:
    accepted = list(state.get("accepted_draft_ids", []))
    decision = interrupt(
        {
            "kind": "review",
            "run_id": state["run_id"],
            "attempt": state.get("generation_attempt") or 1,
            "draft_ids": accepted,
        }
    )
    decision = decision if isinstance(decision, dict) else {"action": str(decision)}
    action = decision.get("action")
    if action == "approve":
        draft_id = decision.get("draft_id")
        if draft_id not in accepted:
            return Command(
                update={
                    "review": decision,
                    "outcome": "failed",
                    "outcome_reason": f"approved draft {draft_id} is not an accepted draft",
                },
                goto="finish",
            )
        return Command(update={"review": decision}, goto="publish")
    if action == "regenerate":
        n = state.get("human_regenerations", 0)
        if n >= MAX_HUMAN_REGENERATIONS:
            return _no_post({"review": decision}, "regeneration limit reached")
        feedback = str(decision.get("feedback") or "The owner asked for different drafts.")
        return Command(
            update={
                "review": decision,
                "human_regenerations": n + 1,
                "auto_revisions": 0,
                "generation_attempt": (state.get("generation_attempt") or 1) + 1,
                "revision_hints": [feedback],
                "accepted_draft_ids": [],
            },
            goto="generate",
        )
    reason = str(decision.get("reason") or "rejected by owner")
    return _no_post({"review": decision}, f"rejected by owner: {reason}")


async def publish(state: PipelineState, runtime: Ctx) -> Command[Literal["finish"]]:
    """Approve the chosen draft and publish it through the service, whose claim-before-network
    design makes a re-execution of this node find the claim instead of posting twice."""
    ctx = runtime.context
    accepted = state.get("accepted_draft_ids", [])
    chosen = (state.get("review") or {}).get("draft_id") or (accepted[0] if accepted else None)
    if chosen is None:
        return Command(
            update={"outcome": "failed", "outcome_reason": "publish reached without a draft"},
            goto="finish",
        )
    async with ctx.db.session_factory() as session:
        await session.execute(
            update(DraftRecord).where(DraftRecord.id == chosen).values(status=DraftStatus.approved)
        )
        await session.commit()
    if ctx.publisher is None:
        return Command(
            update={
                "outcome": "publish_failed",
                "outcome_reason": "draft approved but no publisher is configured",
            },
            goto="finish",
        )
    try:
        post = await publish_draft(ctx.db, ctx.publisher, int(chosen))
    except AlreadyPublished as e:
        return Command(
            update={
                "published": {"id": e.post.id, "x_post_id": e.post.x_post_id, "url": e.post.x_url},
                "outcome": "published",
                "outcome_reason": "already published for this day",
            },
            goto="finish",
        )
    except PublishInProgress as e:
        return Command(
            update={"outcome": "publish_failed", "outcome_reason": str(e)}, goto="finish"
        )
    except PublishError as e:
        return Command(
            update={"outcome": "publish_failed", "outcome_reason": str(e)}, goto="finish"
        )
    if post.status == PublishStatus.published:
        return Command(
            update={
                "published": {"id": post.id, "x_post_id": post.x_post_id, "url": post.x_url},
                "outcome": "published",
                "outcome_reason": f"published as {post.x_post_id}",
            },
            goto="finish",
        )
    return Command(
        update={
            "published": {"id": post.id, "x_post_id": None, "url": None},
            "outcome": "publish_failed",
            "outcome_reason": post.last_error or "publish failed",
        },
        goto="finish",
    )


async def finish(state: PipelineState) -> dict[str, Any]:
    return {}


def build_graph(checkpointer: Any) -> CompiledStateGraph[Any, Any, Any, Any]:
    g: StateGraph[PipelineState, PipelineContext] = StateGraph(
        PipelineState, context_schema=PipelineContext
    )
    g.add_node("collect", collect, destinations=("understand", "ask_checkin", "finish"))
    g.add_node(
        "understand", understand, destinations=("ask_checkin", "discover", "finish"), timeout=600
    )
    g.add_node("ask_checkin", ask_checkin, destinations=("collect", "discover", "finish"))
    g.add_node("discover", discover, destinations=("strategy", "finish"), timeout=600)
    g.add_node("strategy", strategy, destinations=("generate",), timeout=600)
    g.add_node("generate", generate, destinations=("evaluate", "finish"), timeout=900)
    g.add_node(
        "evaluate", evaluate, destinations=("review", "publish", "generate", "finish"), timeout=1200
    )
    g.add_node("review", review, destinations=("publish", "generate", "finish"))
    g.add_node("publish", publish, destinations=("finish",))
    g.add_node("finish", finish)
    g.add_edge(START, "collect")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer, name=f"xbot-{GRAPH_VERSION}")
