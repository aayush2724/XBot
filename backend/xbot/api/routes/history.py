"""Publishing history, manual retry, and consistency statistics."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from xbot.api.deps import DbDep, SessionDep, SettingsDep, require_auth
from xbot.api.schemas import ConsistencyStats, HistoryDay, PublishedOut
from xbot.db.models import Day, DayDecision, PipelineRun, PublishedPost, PublishStatus
from xbot.publishing.base import PublishError
from xbot.publishing.service import AlreadyPublished, PublishInProgress, retry_published
from xbot.services.days import local_today

router = APIRouter(tags=["history"], dependencies=[Depends(require_auth)])


@router.get("/history", response_model=list[HistoryDay])
async def history(
    session: SessionDep,
    settings: SettingsDep,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> list[HistoryDay]:
    today = local_today(settings)
    end = to_date or today
    start = from_date or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if (end - start).days > 366:
        raise HTTPException(status_code=422, detail="range must be a year or less")
    days = {
        d.local_date: d
        for d in (
            await session.execute(select(Day).where(Day.local_date.between(start, end)))
        ).scalars()
    }
    posts = {
        p.local_date: p
        for p in (
            await session.execute(
                select(PublishedPost).where(PublishedPost.local_date.between(start, end))
            )
        ).scalars()
    }
    runs: dict[date, PipelineRun] = {}
    for run in (
        await session.execute(
            select(PipelineRun)
            .where(PipelineRun.local_date.between(start, end))
            .order_by(PipelineRun.id)
        )
    ).scalars():
        runs[run.local_date] = run  # last one wins = latest run
    out: list[HistoryDay] = []
    for d in days.values():
        post = posts.get(d.local_date)
        latest = runs.get(d.local_date)
        out.append(
            HistoryDay(
                local_date=d.local_date,
                decision=d.decision,
                no_post_reason=d.no_post_reason,
                run_status=latest.status if latest else None,
                published=PublishedOut.model_validate(post) if post else None,
            )
        )
    out.sort(key=lambda h: h.local_date, reverse=True)
    return out


@router.get("/stats/consistency", response_model=ConsistencyStats)
async def consistency(
    session: SessionDep, settings: SettingsDep, days: int = 30
) -> ConsistencyStats:
    today = local_today(settings)
    window = max(1, min(days, 366))
    start = today - timedelta(days=window - 1)
    published_dates = {
        p.local_date
        for p in (
            await session.execute(
                select(PublishedPost).where(PublishedPost.status == PublishStatus.published)
            )
        ).scalars()
    }
    decided_dates = {
        d.local_date
        for d in (
            await session.execute(select(Day).where(Day.decision != DayDecision.pending))
        ).scalars()
    } | published_dates

    def streak(dates: set[date]) -> int:
        n, cursor = 0, today
        if cursor not in dates:  # today may still be open; start from yesterday
            cursor -= timedelta(days=1)
        while cursor in dates:
            n += 1
            cursor -= timedelta(days=1)
        return n

    longest = 0
    run_len = 0
    for d in sorted(published_dates):
        run_len = run_len + 1 if (d - timedelta(days=1)) in published_dates else 1
        longest = max(longest, run_len)
    return ConsistencyStats(
        days_window=window,
        posts_published=sum(1 for d in published_dates if start <= d <= today),
        days_decided=sum(1 for d in decided_dates if start <= d <= today),
        current_post_streak=streak(published_dates),
        longest_post_streak=longest,
        current_decision_streak=streak(decided_dates),
    )


@router.post("/published/{post_id}/retry", response_model=PublishedOut)
async def retry(post_id: int, request: Request, db: DbDep) -> PublishedOut:
    publisher = getattr(request.app.state, "publisher", None)
    if publisher is None:
        raise HTTPException(status_code=503, detail="no publisher configured")
    try:
        post = await retry_published(db, publisher, post_id)
    except AlreadyPublished as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PublishInProgress as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PublishError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return PublishedOut.model_validate(post)
