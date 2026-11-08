"""The XBot pipeline as a LangGraph StateGraph (design: docs/phase-2-langgraph-design.md).

M3 scope: collect → understand → ask_checkin ⟂ / discover → finish. M4a inserts strategy,
generate, evaluate and review between discover and finish.

Rules that keep this graph safe:
- State values are JSON-only. Pydantic models are dumped at node boundaries.
- Side effects before an `interrupt()` must be idempotent, because the node re-runs on resume.
- Every exit from a deciding node is a `Command(goto=...)`; routing never re-bills a stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from xbot.config import Settings
from xbot.db.base import Database
from xbot.ingestion.base import DayContext, SourceItem, collect_day_context
from xbot.ingestion.github import GitHubSource
from xbot.ingestion.journal import JournalSource
from xbot.intelligence import prompting
from xbot.intelligence.guards import apply_fact_guards, score_insights, select_insight
from xbot.intelligence.schemas import ActivityUnderstanding, InsightSet
from xbot.intelligence.stages import annotate_stage, call_stage
from xbot.llm.base import LLMProvider
from xbot.logging import get_logger
from xbot.memory.inputs import build_memory_inputs
from xbot.preferences import Preferences
from xbot.services.days import ensure_checkins

log = get_logger("xbot.graph")

GRAPH_VERSION = "v3"
MAX_CHECKIN_ROUNDS = 2
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
    from datetime import date

    return DayContext(
        local_date=date.fromisoformat(local_date),
        items=[SourceItem.model_validate(i) for i in items],
    )


async def collect(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["understand", "ask_checkin", "finish"]]:
    ctx = runtime.context
    from datetime import date

    local_date = date.fromisoformat(state["local_date"])
    async with ctx.db.session_factory() as session:
        day_ctx = await collect_day_context(session, local_date, [JournalSource(), GitHubSource()])
    items = [i.model_dump(mode="json") for i in day_ctx.items]
    memory = build_memory_inputs(ctx.prefs).model_dump(mode="json")
    update: dict[str, Any] = {"context_items": items, "memory": memory}
    if items:
        return Command(update=update, goto="understand")
    if state.get("checkin_round", 0) < MAX_CHECKIN_ROUNDS:
        update["checkin_questions"] = DEFAULT_CHECKIN_QUESTIONS
        return Command(update=update, goto="ask_checkin")
    update.update(outcome="no_post", outcome_reason="no evidence for the day after check-ins")
    return Command(update=update, goto="finish")


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
        update.update(
            outcome="no_post",
            outcome_reason=f"insufficient evidence after {MAX_CHECKIN_ROUNDS} check-in rounds",
        )
        return Command(update=update, goto="finish")
    return Command(update=update, goto="discover")


async def ask_checkin(
    state: PipelineState, runtime: Ctx
) -> Command[Literal["collect", "discover", "finish"]]:
    """Idempotent side effect first (upsert), then interrupt. Re-execution on resume is safe."""
    ctx = runtime.context
    from datetime import date

    questions = list(state.get("checkin_questions") or DEFAULT_CHECKIN_QUESTIONS)
    async with ctx.db.session_factory() as session:
        await ensure_checkins(
            session, date.fromisoformat(state["local_date"]), state["run_id"], questions
        )
    decision = interrupt({"kind": "checkin", "run_id": state["run_id"], "questions": questions})
    action = (decision or {}).get("action") if isinstance(decision, dict) else decision
    next_round = state.get("checkin_round", 0) + 1
    if action == "skip":
        if state.get("facts"):
            return Command(update={"checkin_round": next_round}, goto="discover")
        return Command(
            update={
                "checkin_round": next_round,
                "outcome": "no_post",
                "outcome_reason": "check-in skipped with no usable evidence",
            },
            goto="finish",
        )
    return Command(update={"checkin_round": next_round}, goto="collect")


async def discover(state: PipelineState, runtime: Ctx) -> Command[Literal["finish"]]:
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
    from xbot.intelligence.schemas import FactWithId

    facts = [FactWithId.model_validate(f) for f in facts_json]
    scored, report = score_insights(result.parsed, facts)
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
        return Command(
            update={
                "selected_insight": None,
                "insight_reason": reason,
                "outcome": "no_post",
                "outcome_reason": reason,
            },
            goto="finish",
        )
    # M4a: goto="strategy". Until then the run ends with the chosen insight.
    return Command(
        update={
            "selected_insight": chosen.model_dump(mode="json"),
            "insight_reason": reason,
            "outcome": "insights_ready",
            "outcome_reason": reason,
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
    g.add_node("discover", discover, destinations=("finish",), timeout=600)
    g.add_node("finish", finish)
    g.add_edge(START, "collect")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer, name=f"xbot-{GRAPH_VERSION}")
