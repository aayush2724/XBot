"""M3: graph core (collect → understand → ask_checkin ⟂ / discover → finish) and the runner."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from tests.conftest import BACKEND_DIR, TEST_DB_URL
from xbot.db.models import Checkin, PipelineRun, PipelineStageRun, RunStatus, RunTrigger
from xbot.ingestion.base import DayContext, SourceItem
from xbot.intelligence import schemas as S
from xbot.intelligence.guards import apply_fact_guards, score_insights, select_insight
from xbot.intelligence.runner import psycopg_dsn
from xbot.llm.base import LLMUnavailable

D = "2026-11-08"


# ---------------------------------------------------------------- guards (pure) ------------


def _ctx(*items: SourceItem) -> DayContext:
    return DayContext(local_date=date.fromisoformat(D), items=list(items))


def _item(ref: str, source: str = "journal", status: str | None = None) -> SourceItem:
    return SourceItem(
        source=source,
        ref_id=ref,
        occurred_at="2026-11-08T10:00:00Z",
        content="c",
        status=status,
    )


def test_fact_guards_assign_ids_drop_unsourced_and_downgrade_planned() -> None:
    ctx = _ctx(
        _item("journal:1"), _item("quest:7", "quest", "planned"), _item("quest:8", "quest", "done")
    )
    facts = [
        S.Fact(kind="activity", text="built x", confidence="confirmed", source_refs=["journal:1"]),
        S.Fact(kind="activity", text="hallucinated", confidence="confirmed", source_refs=[]),
        S.Fact(
            kind="activity", text="bad ref", confidence="confirmed", source_refs=["journal:999"]
        ),
        S.Fact(
            kind="activity", text="planned only", confidence="confirmed", source_refs=["quest:7"]
        ),
        S.Fact(
            kind="activity",
            text="done quest",
            confidence="confirmed",
            source_refs=["quest:8", "journal:999"],
        ),
    ]
    kept, report = apply_fact_guards(facts, ctx)
    assert [f.id for f in kept] == ["F1", "F2", "F3"]
    assert kept[1].confidence == "unknown" and "planned" in report.adjusted[0]
    assert kept[2].source_refs == ["quest:8"]
    assert len(report.dropped) == 2


def _insight(title: str, ids: list[str], auth: int = 5, score: int = 4) -> S.Insight:
    return S.Insight(
        title=title,
        what_happened="w",
        why_interesting="y",
        fact_ids=ids,
        scores=S.InsightScores(
            technical_value=score,
            novelty=score,
            relatability=score,
            authenticity=auth,
            relevance=score,
        ),
        similar_post_id=None,
    )


def test_insight_guards_cap_authenticity_and_select_threshold() -> None:
    facts = [
        S.FactWithId(
            id="F1", kind="activity", text="a", confidence="confirmed", source_refs=["j:1"]
        ),
        S.FactWithId(id="F2", kind="concept", text="b", confidence="inferred", source_refs=["j:1"]),
    ]
    iset = S.InsightSet(
        insights=[
            _insight("inferred only", ["F2"], auth=5),
            _insight("ghost", ["F9"]),
            _insight("solid", ["F1", "F2"], score=4),
        ],
        nothing_worth_sharing=False,
        reason="",
    )
    scored, report = score_insights(iset, facts)
    assert len(report.dropped) == 1
    by_title = {s.insight.title: s for s in scored}
    assert by_title["inferred only"].eligible is False
    assert by_title["inferred only"].insight.scores.authenticity == 2
    chosen, reason = select_insight(iset, scored, min_score=3.0)
    assert chosen is not None and chosen.title == "solid"
    chosen2, reason2 = select_insight(iset, scored, min_score=4.9)
    assert chosen2 is None and "below threshold" in reason2
    nothing = S.InsightSet(insights=[], nothing_worth_sharing=True, reason="boring day")
    assert select_insight(nothing, [], 3.0)[0] is None


def test_psycopg_dsn_conversion() -> None:
    assert psycopg_dsn("postgresql+psycopg://u:p@h:5433/db") == "postgresql://u:p@h:5433/db"
    assert psycopg_dsn("postgresql://u@h/db") == "postgresql://u@h/db"
    with pytest.raises(ValueError):
        psycopg_dsn("sqlite:///x.db")


# ---------------------------------------------------------------- helpers -------------------


async def _wait(app: Any, run_id: int) -> dict[str, Any]:
    await app.state.runner.wait(run_id)
    async with app.state.db.session_factory() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        view: dict[str, Any] = {
            "status": run.status,
            "reason": run.outcome_reason,
            "error": run.error,
            "cost": run.total_cost_usd,
            "tokens": run.total_input_tokens,
        }
        return view


async def _start(client: httpx.AsyncClient, supersede: bool = False) -> dict[str, Any]:
    r = await client.post(
        f"/api/days/{D}/pipeline/run", params={"supersede": str(supersede).lower()}
    )
    assert r.status_code == 202, r.text
    body: dict[str, Any] = r.json()
    return body


async def _add_entry(client: httpx.AsyncClient, content: str) -> None:
    r = await client.post(f"/api/days/{D}/entries", json={"kind": "worked_on", "content": content})
    assert r.status_code == 201


def _insufficient(*questions: str) -> S.ActivityUnderstanding:
    return S.ActivityUnderstanding(
        facts=[], summary="nothing", sufficiency="insufficient", checkin_questions=list(questions)
    )


# ---------------------------------------------------------------- graph via API -------------


async def test_empty_day_asks_checkins_then_resumes_without_duplicates(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.waiting_checkin
    assert llm.calls_for("stage1") == []  # no evidence: no model call was wasted

    detail = (await auth_client.get(f"/api/pipeline/runs/{run['id']}")).json()
    assert detail["status"] == "waiting_checkin"
    assert detail["waiting_for"]["kind"] == "checkin"
    assert detail["next_nodes"] == ["ask_checkin"]
    checkins = (await auth_client.get(f"/api/days/{D}/checkins")).json()
    assert len(checkins) == 2

    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/submit")
    assert r.status_code == 409 and "unanswered" in r.json()["detail"]

    for c in checkins:
        r = await auth_client.post(
            f"/api/checkins/{c['id']}/answer",
            json={"answer": "Built the sync job and learned PKCE."},
        )
        assert r.status_code == 200
    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/submit")
    assert r.status_code == 202, r.text
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.waiting_review, state
    assert len(llm.calls_for("stage1")) == 1 and len(llm.calls_for("stage2")) == 1

    async with app.state.db.session_factory() as session:
        rows = (
            (await session.execute(select(Checkin).where(Checkin.run_id == run["id"])))
            .scalars()
            .all()
        )
        assert len(rows) == 2, "ask_checkin re-executed on resume but must not duplicate rows"
        stages = (
            (await session.execute(select(PipelineStageRun).order_by(PipelineStageRun.stage)))
            .scalars()
            .all()
        )
        assert [(s.stage, s.status) for s in stages][:2] == [(1, "succeeded"), (2, "succeeded")]
        assert stages[0].output and stages[0].output["facts"][0]["id"] == "F1"
        assert stages[1].output and stages[1].output["selected"]["title"].startswith("[MOCK]")
    assert state["tokens"] > 0

    day = (await auth_client.get(f"/api/days/{D}")).json()
    assert day["latest_run"]["id"] == run["id"] and day["latest_run"]["status"] == "waiting_review"


async def test_checkin_round_cap_ends_in_no_post(app: Any, auth_client: httpx.AsyncClient) -> None:
    llm = app.state.llm
    await _add_entry(auth_client, "vague note")
    llm.script(
        "stage1", _insufficient("Q1?", "Q2?"), _insufficient("Q1?", "Q3?"), _insufficient("Q1?")
    )
    run = await _start(auth_client)
    assert (await _wait(app, run["id"]))["status"] == RunStatus.waiting_checkin
    for round_no in (1, 2):
        checkins = (await auth_client.get(f"/api/days/{D}/checkins")).json()
        for c in checkins:
            if c["answer"] is None:
                await auth_client.post(
                    f"/api/checkins/{c['id']}/answer", json={"answer": "still vague"}
                )
        r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/submit")
        assert r.status_code == 202, (round_no, r.text)
        state = await _wait(app, run["id"])
        if round_no == 1:
            assert state["status"] == RunStatus.waiting_checkin
    assert state["status"] == RunStatus.no_post
    assert "check-in rounds" in (state["reason"] or "")
    assert len(llm.calls_for("stage1")) == 3
    checkins = (await auth_client.get(f"/api/days/{D}/checkins")).json()
    assert sorted(c["question"] for c in checkins) == ["Q1?", "Q2?", "Q3?"], "upsert by question"


async def test_skip_with_no_facts_is_no_post(app: Any, auth_client: httpx.AsyncClient) -> None:
    run = await _start(auth_client)
    assert (await _wait(app, run["id"]))["status"] == RunStatus.waiting_checkin
    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/skip")
    assert r.status_code == 202
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.no_post and "skipped" in (state["reason"] or "")


async def test_nothing_worth_sharing_is_no_post(app: Any, auth_client: httpx.AsyncClient) -> None:
    await _add_entry(auth_client, "Fixed a flaky test")
    app.state.llm.script(
        "stage2", S.InsightSet(insights=[], nothing_worth_sharing=True, reason="routine")
    )
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.no_post and "routine" in (state["reason"] or "")


async def test_second_start_conflicts_and_supersede_replaces(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    first = await _start(auth_client)
    assert (await _wait(app, first["id"]))["status"] == RunStatus.waiting_checkin
    r = await auth_client.post(f"/api/days/{D}/pipeline/run")
    assert r.status_code == 409
    second = await _start(auth_client, supersede=True)
    await _wait(app, second["id"])
    runs = (await auth_client.get(f"/api/days/{D}/runs")).json()
    by_id = {r["id"]: r["status"] for r in runs}
    assert by_id[first["id"]] == "superseded" and by_id[second["id"]] == "waiting_checkin"


async def test_double_resume_only_one_wins(app: Any, auth_client: httpx.AsyncClient) -> None:
    run = await _start(auth_client)
    await _wait(app, run["id"])
    for c in (await auth_client.get(f"/api/days/{D}/checkins")).json():
        await auth_client.post(f"/api/checkins/{c['id']}/answer", json={"answer": "did things"})
    r1, r2 = await asyncio.gather(
        auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/submit"),
        auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/submit"),
    )
    assert sorted([r1.status_code, r2.status_code]) == [202, 409]
    assert (await _wait(app, run["id"]))["status"] == RunStatus.waiting_review


async def test_failure_keeps_checkpoint_and_retry_resumes_only_failed_node(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    await _add_entry(auth_client, "Wrote the runner")
    llm.script("stage1", LLMUnavailable("Anthropic rate limit after SDK retries"))
    run = await _start(auth_client)
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.failed and "LLMUnavailable" in (state["error"] or "")
    async with app.state.db.session_factory() as session:
        failed_rows = (await session.execute(select(PipelineStageRun))).scalars().all()
        assert [(r.stage, r.status) for r in failed_rows] == [(1, "failed")]

    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/retry")
    assert r.status_code == 202
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.waiting_review, state
    assert len(llm.calls_for("stage1")) == 2 and len(llm.calls_for("stage2")) == 1
    async with app.state.db.session_factory() as session:
        rows = (
            (await session.execute(select(PipelineStageRun).order_by(PipelineStageRun.id)))
            .scalars()
            .all()
        )
        assert [(r.stage, r.attempt, r.status) for r in rows][:3] == [
            (1, 1, "failed"),
            (1, 2, "succeeded"),
            (2, 1, "succeeded"),
        ]
    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/retry")
    assert r.status_code == 409


async def test_recover_resumes_transient_failure_once_and_marks_others_failed(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    await _add_entry(auth_client, "Recovery test")
    llm.script("stage1", LLMUnavailable("could not reach the Anthropic API"))
    run = await _start(auth_client)
    assert (await _wait(app, run["id"]))["status"] == RunStatus.failed
    # Simulate a crash before the runner could record the failure: status is still 'running'.
    async with app.state.db.session_factory() as session:
        await session.execute(
            text("UPDATE pipeline_runs SET status='running', error=NULL WHERE id=:id"),
            {"id": run["id"]},
        )
        await session.commit()
    await app.state.runner.recover()
    state = await _wait(app, run["id"])
    assert state["status"] == RunStatus.waiting_review, state
    async with app.state.db.session_factory() as session:
        db_run = await session.get(PipelineRun, run["id"])
        assert db_run is not None and db_run.recover_attempts == 1

    # A live row whose thread has no checkpoint is marked failed, not resumed.
    from xbot.services.days import get_or_create_day

    async with app.state.db.session_factory() as session:
        await get_or_create_day(session, date(2026, 11, 7))
        session.add(
            PipelineRun(
                local_date=date(2026, 11, 7),
                thread_id="run:ghost",
                graph_version="v3",
                trigger=RunTrigger.manual,
                status=RunStatus.running,
            )
        )
        await session.commit()
    await app.state.runner.recover()
    async with app.state.db.session_factory() as session:
        ghost = (
            await session.execute(select(PipelineRun).where(PipelineRun.thread_id == "run:ghost"))
        ).scalar_one()
        assert ghost.status == RunStatus.failed and "no checkpoint" in (ghost.outcome_reason or "")


async def test_cancel_waiting_run(app: Any, auth_client: httpx.AsyncClient) -> None:
    run = await _start(auth_client)
    await _wait(app, run["id"])
    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await auth_client.post(f"/api/pipeline/runs/{run['id']}/checkins/skip")
    assert r.status_code == 409
    second = await _start(auth_client)  # the day is free again
    await _wait(app, second["id"])


async def test_state_is_json_only_in_every_checkpoint(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    await _add_entry(auth_client, "JSON check")
    run = await _start(auth_client)
    await _wait(app, run["id"])
    runner = app.state.runner
    async with app.state.db.session_factory() as session:
        db_run = await session.get(PipelineRun, run["id"])
        assert db_run is not None
    cfg = runner._config(db_run.thread_id)
    count = 0
    async for snap in runner.graph.aget_state_history(cfg):
        json.dumps(snap.values)  # raises on any non-JSON value
        count += 1
    assert count >= 4


def test_setup_is_idempotent_and_alembic_sees_no_drift() -> None:
    """LangGraph's checkpoint tables exist in the test DB (created by the app's lifespan in
    earlier tests); Alembic must ignore them and report no pending changes."""
    env = {**os.environ, "XBOT_DATABASE_URL": TEST_DB_URL, "XBOT_ENV": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout + result.stderr


async def test_setup_twice_is_safe(app: Any) -> None:
    await app.state.runner.checkpointer.setup()
    await app.state.runner.checkpointer.setup()
