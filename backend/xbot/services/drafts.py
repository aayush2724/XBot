"""Draft listing and owner edits. Text edits are allowed only while the run waits for review;
ratings and feedback are allowed at any time because they feed future generations."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.db.models import DraftRecord, DraftStatus, PipelineRun, RunStatus


class DraftNotFound(Exception):
    pass


class DraftNotEditable(Exception):
    pass


async def list_for_day(session: AsyncSession, local_date: date) -> list[DraftRecord]:
    stmt = (
        select(DraftRecord)
        .where(DraftRecord.local_date == local_date)
        .order_by(DraftRecord.run_id.desc(), DraftRecord.attempt.desc(), DraftRecord.label)
    )
    return list((await session.execute(stmt)).scalars().all())


async def patch_draft(
    session: AsyncSession,
    draft_id: int,
    *,
    edited_text: str | None = None,
    rating: int | None = None,
    feedback: str | None = None,
) -> DraftRecord:
    draft = await session.get(DraftRecord, draft_id)
    if draft is None:
        raise DraftNotFound(f"draft {draft_id} not found")
    if edited_text is not None:
        run = await session.get(PipelineRun, draft.run_id)
        editable = (
            run is not None
            and run.status == RunStatus.waiting_review
            and draft.status in (DraftStatus.candidate, DraftStatus.accepted)
        )
        if not editable:
            raise DraftNotEditable(
                "text can only be edited while the run is waiting for review and the draft is "
                "still a candidate"
            )
        cleaned = edited_text.strip()
        draft.edited_text = cleaned if cleaned and cleaned != draft.text.strip() else None
    if rating is not None:
        draft.rating = rating
    if feedback is not None:
        draft.feedback = feedback.strip() or None
    await session.commit()
    await session.refresh(draft)
    return draft
