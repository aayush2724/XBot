"""Journal source: quests, free-text entries and answered check-ins for one day."""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from xbot.ingestion.base import SourceItem
from xbot.services.days import get_day


class JournalSource:
    name = "journal"

    async def collect(self, session: AsyncSession, local_date: date) -> list[SourceItem]:
        day = await get_day(session, local_date)
        if day is None:
            return []
        items: list[SourceItem] = []
        for q in day.quests:
            items.append(
                SourceItem(
                    source="quest",
                    ref_id=f"quest:{q.id}",
                    occurred_at=q.completed_at or q.created_at,
                    content=q.text,
                    status=q.status.value,
                    metadata={"position": q.position},
                )
            )
        for e in day.entries:
            items.append(
                SourceItem(
                    source="journal",
                    ref_id=f"journal:{e.id}",
                    occurred_at=e.created_at,
                    content=e.content,
                    metadata={"kind": e.kind.value},
                )
            )
        for c in day.checkins:
            if c.answer and c.answered_at:
                items.append(
                    SourceItem(
                        source="checkin",
                        ref_id=f"checkin:{c.id}",
                        occurred_at=c.answered_at,
                        content=f"Q: {c.question}\nA: {c.answer}",
                    )
                )
        return items
