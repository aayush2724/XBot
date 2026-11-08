"""M4a: strategy → generate → evaluate → review ⟂ / publish, through the API and the graph."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select

from xbot.db.models import (
    DraftRecord,
    DraftStatus,
    PipelineRun,
    PipelineStageRun,
    RunStatus,
    RunTrigger,
)
from xbot.intelligence import schemas as S
from xbot.intelligence.graph import PipelineContext, build_graph
from xbot.preferences import AUTONOMOUS_CONFIRMATION, load_preferences
from xbot.services.days import add_entry, get_or_create_day

D = "2026-11-08"


async def _wait(app: Any, run_id: int) -> dict[str, Any]:
    await app.state.runner.wait(run_id, 60)
    async with app.state.db.session_factory() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return {"status": run.status, "reason": run.outcome_reason, "error": run.error}


async def _start(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.post(f"/api/days/{D}/pipeline/run")
    assert r.status_code == 202, r.text
    body: dict[str, Any] = r.json()
    return body


async def _seed(client: httpx.AsyncClient) -> None:
    r = await client.post(
        f"/api/days/{D}/entries",
        json={
            "kind": "worked_on",
            "content": "Built the LangGraph runner with a Postgres checkpointer",
        },
    )
    assert r.status_code == 201


def _draft(label: str, text: str, claims: list[S.Claim]) -> S.Draft:
    return S.Draft(label=label, text=text, style_note="t", claims=claims)


def _reject_eval(hint: str = "be more specific") -> S.Evaluation:
    return S.Evaluation(
        grounding=[],
        worth_sharing=2,
        sounds_human=3,
        technically_correct=True,
        repetitive=False,
        reveals_sensitive=False,
        issues=["too vague"],
        verdict="revise",
        revision_hint=hint,
    )


async def test_full_run_ends_waiting_review_with_drafts(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    await _seed(auth_client)
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.waiting_review, state
    detail = (await auth_client.get(f"/api/pipeline/runs/{run['id']}")).json()
    assert detail["waiting_for"]["kind"] == "review"
    assert detail["next_nodes"] == ["review"]
    assert len(detail["drafts"]) == 2
    assert {d["status"] for d in detail["drafts"]} == {"accepted"}
    assert set(detail["waiting_for"]["draft_ids"]) == {d["id"] for d in detail["drafts"]}
    assert all(d["category"] == "build_in_public" for d in detail["drafts"])
    assert all(d["text"].startswith("[MOCK]") for d in detail["drafts"])
    stages = [(s["stage"], s["status"]) for s in detail["stages"]]
    assert stages == [
        (1, "succeeded"),
        (2, "succeeded"),
        (3, "succeeded"),
        (4, "succeeded"),
        (5, "succeeded"),
        (5, "succeeded"),
    ]
    assert detail["stages"][-1]["output"]["raw"]["verdict"] == "accept"


async def test_code_checks_reject_before_the_model_sees_the_draft(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    await _seed(auth_client)
    uncovered = _draft(
        "A",
        "Shipped the runner. Also rewrote the whole scheduler.",
        [S.Claim(sentence="Shipped the runner.", kind="fact", fact_ids=["F1"], hedged=False)],
    )
    leaky = _draft(
        "B",
        "My key is sk-ant-" + "x" * 30 + " lol.",
        [
            S.Claim(
                sentence="My key is sk-ant-" + "x" * 30 + " lol.",
                kind="opinion",
                fact_ids=[],
                hedged=False,
            )
        ],
    )
    clean_text = "[MOCK] Today: Built the LangGraph runner with a Postgres checkpointer."
    clean = _draft(
        "C", clean_text, [S.Claim(sentence=clean_text, kind="fact", fact_ids=["F1"], hedged=False)]
    )
    llm.script("stage4", S.DraftSet(drafts=[uncovered, leaky, clean]))
    run = await _start(auth_client)
    assert (await _wait(app, run["id"]))["status"] == RunStatus.waiting_review
    assert len(llm.calls_for("stage5")) == 1, "only the clean draft reaches the model"
    detail = (await auth_client.get(f"/api/pipeline/runs/{run['id']}")).json()
    by_label = {d["label"]: d for d in detail["drafts"]}
    assert (
        by_label["A"]["status"] == "rejected"
        and "uncovered sentence" in by_label["A"]["evaluation"]["code_issues"][0]
    )
    assert by_label["B"]["status"] == "rejected" and any(
        "secret" in i for i in by_label["B"]["evaluation"]["code_issues"]
    )
    assert by_label["C"]["status"] == "accepted"
    assert detail["waiting_for"]["draft_ids"] == [by_label["C"]["id"]]


async def test_unhedged_inferred_claim_is_rejected(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    await _seed(auth_client)
    llm.script(
        "stage1",
        S.ActivityUnderstanding(
            facts=[
                S.Fact(
                    kind="activity",
                    text="probably fixed the race",
                    confidence="inferred",
                    source_refs=["journal:1"],
                )
            ],
            summary="s",
            sufficiency="enough",
            checkin_questions=[],
        ),
    )
    # Stage 2 default would cap authenticity; script an insight citing F1 so the run proceeds.
    llm.script(
        "stage2",
        S.InsightSet(
            insights=[
                S.Insight(
                    title="race",
                    what_happened="w",
                    why_interesting="y",
                    fact_ids=["F1"],
                    scores=S.InsightScores(
                        technical_value=4, novelty=4, relatability=4, authenticity=2, relevance=4
                    ),
                    similar_post_id=None,
                )
            ],
            nothing_worth_sharing=False,
            reason="",
        ),
    )
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    # No confirmed fact → select_insight refuses → no_post before drafting.
    assert state["status"] == RunStatus.no_post and "confirmed fact" in (state["reason"] or "")


async def test_two_failed_revisions_end_in_no_post(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    await _seed(auth_client)
    for _ in range(6):  # 3 attempts × 2 drafts
        llm.script("stage5", _reject_eval())
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.no_post and "after 3 attempts" in (state["reason"] or ""), (
        state
    )
    assert len(llm.calls_for("stage4")) == 3 and len(llm.calls_for("stage5")) == 6
    assert "be more specific" in llm.calls_for("stage4")[1]["user"]
    async with app.state.db.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(DraftRecord).order_by(DraftRecord.attempt, DraftRecord.label)
                )
            )
            .scalars()
            .all()
        )
        assert [(r.attempt, r.label, r.status) for r in rows] == [
            (1, "A", DraftStatus.rejected),
            (1, "B", DraftStatus.rejected),
            (2, "A", DraftStatus.rejected),
            (2, "B", DraftStatus.rejected),
            (3, "A", DraftStatus.rejected),
            (3, "B", DraftStatus.rejected),
        ]
        stage_rows = (
            (await session.execute(select(PipelineStageRun).where(PipelineStageRun.stage == 4)))
            .scalars()
            .all()
        )
        assert [s.attempt for s in stage_rows] == [1, 2, 3]


async def test_autonomous_mode_skips_review_and_reaches_publish(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    r = await auth_client.put(
        "/api/settings",
        json={
            "preferences": {"publishing_mode": "autonomous"},
            "confirmation": AUTONOMOUS_CONFIRMATION,
        },
    )
    assert r.status_code == 200
    await _seed(auth_client)
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.published, state
    detail = (await auth_client.get(f"/api/pipeline/runs/{run['id']}")).json()
    assert detail["waiting_for"] is None
    statuses = sorted(d["status"] for d in detail["drafts"])
    assert statuses == ["accepted", "published"]
    assert app.state.publisher.publish_calls == 1


async def test_new_run_discards_previous_unreviewed_drafts(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    await _seed(auth_client)
    first = await _start(auth_client)
    assert (await _wait(app, first["id"]))["status"] == RunStatus.waiting_review
    r = await auth_client.post(f"/api/days/{D}/pipeline/run", params={"supersede": "true"})
    assert r.status_code == 202
    await _wait(app, r.json()["id"])
    async with app.state.db.session_factory() as session:
        old = (
            (await session.execute(select(DraftRecord).where(DraftRecord.run_id == first["id"])))
            .scalars()
            .all()
        )
        assert old and all(d.status == DraftStatus.discarded for d in old)


# ---------------------------------------------------------------- review routing (graph) ----


async def _graph_to_review(app: Any) -> tuple[Any, RunnableConfig, PipelineContext, int]:
    """Drive a graph with an in-memory saver up to the review interrupt."""
    async with app.state.db.session_factory() as session:
        await get_or_create_day(session, date.fromisoformat(D))
        await add_entry(session, date.fromisoformat(D), "worked_on", "Wrote the review node")  # type: ignore[arg-type]
        run = PipelineRun(
            local_date=date.fromisoformat(D),
            thread_id="run:graphtest",
            graph_version="v3",
            trigger=RunTrigger.manual,
            status=RunStatus.running,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        prefs = await load_preferences(session)
    ctx = PipelineContext(
        db=app.state.db, settings=app.state.settings, prefs=prefs, llm=app.state.llm
    )
    graph = build_graph(InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": "t-review"}, "recursion_limit": 50}
    await graph.ainvoke({"run_id": run.id, "local_date": D, "checkin_round": 0}, cfg, context=ctx)
    snap = await graph.aget_state(cfg)
    assert snap.interrupts and snap.interrupts[0].value["kind"] == "review"
    return graph, cfg, ctx, run.id


async def test_review_approve_marks_draft_approved_and_reaches_publish(app: Any) -> None:
    graph, cfg, ctx, run_id = await _graph_to_review(app)
    draft_ids = (await graph.aget_state(cfg)).interrupts[0].value["draft_ids"]
    out = await graph.ainvoke(
        Command(resume={"action": "approve", "draft_id": draft_ids[0]}), cfg, context=ctx
    )
    assert out["outcome"] == "publish_failed" and "no publisher" in out["outcome_reason"]
    async with app.state.db.session_factory() as session:
        row = await session.get(DraftRecord, draft_ids[0])
        assert row is not None and row.status == DraftStatus.approved


async def test_review_reject_is_no_post(app: Any) -> None:
    graph, cfg, ctx, _ = await _graph_to_review(app)
    out = await graph.ainvoke(
        Command(resume={"action": "reject", "reason": "not today"}), cfg, context=ctx
    )
    assert out["outcome"] == "no_post" and "not today" in out["outcome_reason"]


async def test_review_regenerate_loops_with_feedback_then_caps(app: Any) -> None:
    llm = app.state.llm
    graph, cfg, ctx, _ = await _graph_to_review(app)
    assert len(llm.calls_for("stage4")) == 1
    await graph.ainvoke(
        Command(resume={"action": "regenerate", "feedback": "shorter and drier"}), cfg, context=ctx
    )
    snap = await graph.aget_state(cfg)
    assert snap.interrupts and snap.interrupts[0].value["attempt"] == 2
    assert (
        len(llm.calls_for("stage4")) == 2
        and "shorter and drier" in llm.calls_for("stage4")[1]["user"]
    )
    await graph.ainvoke(Command(resume={"action": "regenerate"}), cfg, context=ctx)
    assert (await graph.aget_state(cfg)).interrupts[0].value["attempt"] == 3
    out = await graph.ainvoke(Command(resume={"action": "regenerate"}), cfg, context=ctx)
    assert out["outcome"] == "no_post" and "regeneration limit" in out["outcome_reason"]
    assert len(llm.calls_for("stage4")) == 3


async def test_review_approve_with_wrong_draft_id_fails_safely(app: Any) -> None:
    graph, cfg, ctx, _ = await _graph_to_review(app)
    out = await graph.ainvoke(
        Command(resume={"action": "approve", "draft_id": 999999}), cfg, context=ctx
    )
    assert out["outcome"] == "failed" and "not an accepted draft" in out["outcome_reason"]
