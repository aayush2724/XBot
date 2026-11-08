"""Days, quests, journal entries, check-ins and day decisions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.api.deps import SessionDep, SettingsDep, require_auth
from xbot.api.schemas import (
    CheckinAnswerIn,
    CheckinOut,
    CommitOut,
    DayView,
    EntryIn,
    EntryOut,
    NoPostIn,
    PublishedOut,
    QuestOut,
    QuestsIn,
    QuestStatusIn,
    RunOut,
)
from xbot.db.models import Day, DayDecision, GithubCommit, PipelineRun, PublishedPost
from xbot.ingestion.github import list_commits_for_day
from xbot.services import days as svc

router = APIRouter(tags=["days"], dependencies=[Depends(require_auth)])


async def _latest_run(session: AsyncSession, local_date: date) -> RunOut | None:
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.local_date == local_date)
        .order_by(PipelineRun.id.desc())
        .limit(1)
    )
    run = (await session.execute(stmt)).scalar_one_or_none()
    return RunOut.model_validate(run) if run else None


async def _published(session: AsyncSession, local_date: date) -> PublishedOut | None:
    row = (
        await session.execute(select(PublishedPost).where(PublishedPost.local_date == local_date))
    ).scalar_one_or_none()
    return PublishedOut.model_validate(row) if row else None


async def _full_view(session: AsyncSession, local_date: date) -> DayView:
    view = _view(
        local_date,
        await svc.get_day(session, local_date),
        await list_commits_for_day(session, local_date),
        await _latest_run(session, local_date),
    )
    view.published = await _published(session, local_date)
    return view


def _view(
    local_date: date,
    day: Day | None,
    commits: list[GithubCommit] | None = None,
    latest_run: RunOut | None = None,
) -> DayView:
    commit_views = [CommitOut.model_validate(c) for c in commits or []]
    if day is None:
        return DayView(
            local_date=local_date,
            exists=False,
            decision=DayDecision.pending,
            no_post_reason=None,
            decided_at=None,
            quests=[],
            entries=[],
            checkins=[],
            commits=commit_views,
            latest_run=latest_run,
        )
    return DayView(
        local_date=day.local_date,
        exists=True,
        decision=day.decision,
        no_post_reason=day.no_post_reason,
        decided_at=day.decided_at,
        quests=[QuestOut.model_validate(q) for q in day.quests],
        entries=[EntryOut.model_validate(e) for e in day.entries],
        checkins=[CheckinOut.model_validate(c) for c in day.checkins],
        commits=commit_views,
        latest_run=latest_run,
    )


@router.get("/days/today", response_model=DayView)
async def today(session: SessionDep, settings: SettingsDep) -> DayView:
    return await _full_view(session, svc.local_today(settings))


@router.get("/days/{local_date}", response_model=DayView)
async def day(local_date: date, session: SessionDep) -> DayView:
    return await _full_view(session, local_date)


@router.put("/days/{local_date}/quests", response_model=DayView)
async def put_quests(local_date: date, body: QuestsIn, session: SessionDep) -> DayView:
    try:
        texts = body.cleaned()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _view(local_date, await svc.set_quests(session, local_date, texts))


@router.patch("/quests/{quest_id}", response_model=QuestOut)
async def patch_quest(quest_id: int, body: QuestStatusIn, session: SessionDep) -> QuestOut:
    try:
        quest = await svc.set_quest_status(session, quest_id, body.status)
    except svc.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return QuestOut.model_validate(quest)


@router.post("/days/{local_date}/entries", response_model=EntryOut, status_code=201)
async def post_entry(local_date: date, body: EntryIn, session: SessionDep) -> EntryOut:
    entry = await svc.add_entry(session, local_date, body.kind, body.content, body.source)
    return EntryOut.model_validate(entry)


@router.delete("/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(entry_id: int, session: SessionDep) -> None:
    try:
        await svc.delete_entry(session, entry_id)
    except svc.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/days/{local_date}/checkins", response_model=list[CheckinOut])
async def checkins(local_date: date, session: SessionDep) -> list[CheckinOut]:
    day_row = await svc.get_day(session, local_date)
    return [CheckinOut.model_validate(c) for c in (day_row.checkins if day_row else [])]


@router.post("/checkins/{checkin_id}/answer", response_model=CheckinOut)
async def answer(checkin_id: int, body: CheckinAnswerIn, session: SessionDep) -> CheckinOut:
    try:
        row = await svc.answer_checkin(session, checkin_id, body.answer)
    except svc.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return CheckinOut.model_validate(row)


@router.post("/days/{local_date}/no-post", response_model=DayView)
async def no_post(local_date: date, body: NoPostIn, session: SessionDep) -> DayView:
    await svc.mark_no_post(session, local_date, body.reason)
    return await _full_view(session, local_date)
