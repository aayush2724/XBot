"""Pydantic read/write schemas for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from xbot.db.models import (
    DayDecision,
    DraftStatus,
    EntryKind,
    EntrySource,
    QuestStatus,
    RunStatus,
    RunTrigger,
)


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


class CommitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sha: str
    repo_full_name: str
    authored_at: datetime
    message: str
    additions: int
    deletions: int
    html_url: str
    files: list[dict[str, Any]]
    diff: str | None = Field(default=None, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_diff(self) -> bool:
        return self.diff is not None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    local_date: date
    trigger: RunTrigger
    status: RunStatus
    outcome_reason: str | None
    recover_attempts: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cost_usd: float
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class StageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    stage: int
    attempt: int
    status: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    duration_ms: int
    request_id: str | None
    error: str | None
    output: dict[str, Any] | None


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    attempt: int
    label: str
    category: str | None
    text: str
    edited_text: str | None
    style_note: str
    claims: list[dict[str, Any]]
    evaluation: dict[str, Any] | None
    status: DraftStatus
    rating: int | None
    feedback: str | None


class RunDetail(RunOut):
    next_nodes: list[str] = Field(default_factory=list)
    waiting_for: dict[str, Any] | None = None
    stages: list[StageOut] = Field(default_factory=list)
    drafts: list[DraftOut] = Field(default_factory=list)


class DayView(BaseModel):
    local_date: date
    exists: bool
    decision: DayDecision
    no_post_reason: str | None
    decided_at: datetime | None
    quests: list[QuestOut]
    entries: list[EntryOut]
    checkins: list[CheckinOut]
    commits: list[CommitOut] = Field(default_factory=list)
    latest_run: RunOut | None = None


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
