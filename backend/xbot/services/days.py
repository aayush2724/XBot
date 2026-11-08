"""Day-scoped operations: quests, journal entries, check-ins, day decisions."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from xbot.config import Settings
from xbot.db.models import (
    Checkin,
    Day,
    DayDecision,
    EntryKind,
    EntrySource,
    JournalEntry,
    Quest,
    QuestStatus,
)

MAX_QUESTS = 3


class NotFound(Exception):
    pass


def local_today(settings: Settings) -> date:
    return datetime.now(settings.tz).date()


def _now() -> datetime:
    return datetime.now(UTC)


async def get_day(session: AsyncSession, local_date: date) -> Day | None:
    stmt = (
        select(Day)
        .where(Day.local_date == local_date)
        .options(selectinload(Day.quests), selectinload(Day.entries), selectinload(Day.checkins))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_day(session: AsyncSession, local_date: date) -> Day:
    day = await get_day(session, local_date)
    if day is None:
        day = Day(local_date=local_date, quests=[], entries=[], checkins=[])
        session.add(day)
        await session.flush()
    return day


async def set_quests(session: AsyncSession, local_date: date, texts: list[str]) -> Day:
    if len(texts) > MAX_QUESTS:
        raise ValueError(f"at most {MAX_QUESTS} quests per day")
    day = await get_or_create_day(session, local_date)
    previous = {q.text.strip().lower(): q for q in day.quests}
    new_quests: list[Quest] = []
    for position, text in enumerate(texts, start=1):
        cleaned = text.strip()
        old = previous.get(cleaned.lower())
        new_quests.append(
            Quest(
                local_date=local_date,
                text=cleaned,
                position=position,
                status=old.status if old else QuestStatus.planned,
                completed_at=old.completed_at if old else None,
            )
        )
    day.quests.clear()
    await session.flush()  # delete old rows first so (local_date, position) stays unique
    day.quests.extend(new_quests)
    await session.commit()
    return await get_or_create_day(session, local_date)


async def set_quest_status(session: AsyncSession, quest_id: int, status: QuestStatus) -> Quest:
    quest = await session.get(Quest, quest_id)
    if quest is None:
        raise NotFound("quest not found")
    quest.status = status
    quest.completed_at = _now() if status == QuestStatus.done else None
    await session.commit()
    return quest


async def add_entry(
    session: AsyncSession,
    local_date: date,
    kind: EntryKind,
    content: str,
    source: EntrySource = EntrySource.dashboard,
) -> JournalEntry:
    await get_or_create_day(session, local_date)
    entry = JournalEntry(local_date=local_date, kind=kind, content=content.strip(), source=source)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def delete_entry(session: AsyncSession, entry_id: int) -> None:
    entry = await session.get(JournalEntry, entry_id)
    if entry is None:
        raise NotFound("entry not found")
    await session.delete(entry)
    await session.commit()


async def create_checkins(
    session: AsyncSession, local_date: date, questions: list[str], run_id: int | None = None
) -> list[Checkin]:
    await get_or_create_day(session, local_date)
    rows = [
        Checkin(local_date=local_date, question=q.strip(), run_id=run_id)
        for q in questions
        if q.strip()
    ]
    session.add_all(rows)
    await session.commit()
    for row in rows:
        await session.refresh(row)
    return rows


async def ensure_checkins(
    session: AsyncSession, local_date: date, run_id: int, questions: list[str]
) -> list[Checkin]:
    """Idempotent: creates only the questions that do not already exist for this run."""
    await get_or_create_day(session, local_date)
    existing = (
        (await session.execute(select(Checkin).where(Checkin.run_id == run_id))).scalars().all()
    )
    have = {c.question.strip() for c in existing}
    new_rows = [
        Checkin(local_date=local_date, question=q.strip(), run_id=run_id)
        for q in questions
        if q.strip() and q.strip() not in have
    ]
    if new_rows:
        session.add_all(new_rows)
        await session.commit()
    return [*existing, *new_rows]


async def answer_checkin(session: AsyncSession, checkin_id: int, answer: str) -> Checkin:
    checkin = await session.get(Checkin, checkin_id)
    if checkin is None:
        raise NotFound("check-in not found")
    checkin.answer = answer.strip()
    checkin.answered_at = _now()
    await session.commit()
    return checkin


async def mark_no_post(session: AsyncSession, local_date: date, reason: str) -> Day:
    day = await get_or_create_day(session, local_date)
    day.decision = DayDecision.no_post
    day.no_post_reason = reason.strip()
    day.decided_at = _now()
    await session.commit()
    return day
