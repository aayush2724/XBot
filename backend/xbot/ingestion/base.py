"""Ingestion contracts. A source turns stored activity into SourceItems; the pipeline sees only
SourceItems, never raw tables, so new sources (calendar, notes) are additive."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

SourceKind = Literal["journal", "quest", "checkin", "commit"]


class SourceItem(BaseModel):
    source: SourceKind
    ref_id: str  # e.g. "journal:42", "quest:7", "commit:ab12cd"
    occurred_at: datetime
    content: str
    status: str | None = None  # quest status, so "planned" is visible to the model as not done
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivitySource(Protocol):
    name: str

    async def collect(self, session: AsyncSession, local_date: date) -> list[SourceItem]: ...


class DayContext(BaseModel):
    local_date: date
    items: list[SourceItem]

    def by_source(self, kind: SourceKind) -> list[SourceItem]:
        return [i for i in self.items if i.source == kind]

    @property
    def ref_ids(self) -> set[str]:
        return {i.ref_id for i in self.items}


async def collect_day_context(
    session: AsyncSession, local_date: date, sources: list[ActivitySource]
) -> DayContext:
    items: list[SourceItem] = []
    for source in sources:
        items.extend(await source.collect(session, local_date))
    items.sort(key=lambda i: i.occurred_at)
    return DayContext(local_date=local_date, items=items)
