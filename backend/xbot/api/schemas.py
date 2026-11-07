"""Pydantic read/write schemas for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xbot.db.models import DayDecision, EntryKind, EntrySource, QuestStatus


class QuestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    text: str
    position: int
    status: QuestStatus
    completed_at: datetime | None


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: EntryKind
    content: str
    source: EntrySource
    created_at: datetime


class CheckinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    question: str
    answer: str | None
    asked_at: datetime
    answered_at: datetime | None


class DayView(BaseModel):
    local_date: date
    exists: bool
    decision: DayDecision
    no_post_reason: str | None
    decided_at: datetime | None
    quests: list[QuestOut]
    entries: list[EntryOut]
    checkins: list[CheckinOut]


class QuestsIn(BaseModel):
    quests: list[str] = Field(max_length=3)

    def cleaned(self) -> list[str]:
        out = [q.strip() for q in self.quests if q.strip()]
        if any(len(q) > 200 for q in out):
            raise ValueError("each quest must be 200 characters or fewer")
        return out


class QuestStatusIn(BaseModel):
    status: QuestStatus


class EntryIn(BaseModel):
    kind: EntryKind = EntryKind.update
    content: str = Field(min_length=1, max_length=4000)
    source: EntrySource = EntrySource.dashboard

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be blank")
        return v.strip()


class CheckinAnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)

    @field_validator("answer")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("answer must not be blank")
        return v.strip()


class NoPostIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
