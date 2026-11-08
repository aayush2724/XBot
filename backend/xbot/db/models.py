"""ORM models. Tables are added milestone by milestone; Alembic migrations live in ../alembic."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false, func

from xbot.db.base import Base, TimestampMixin


class DayDecision(StrEnum):
    pending = "pending"
    post = "post"
    no_post = "no_post"


class QuestStatus(StrEnum):
    planned = "planned"
    done = "done"
    dropped = "dropped"


class EntryKind(StrEnum):
    update = "update"
    worked_on = "worked_on"
    learned = "learned"
    challenge = "challenge"
    plan = "plan"


class EntrySource(StrEnum):
    dashboard = "dashboard"
    cli = "cli"
    api = "api"


class RunStatus(StrEnum):
    running = "running"
    waiting_checkin = "waiting_checkin"
    waiting_review = "waiting_review"
    published = "published"
    no_post = "no_post"
    publish_failed = "publish_failed"
    failed = "failed"
    superseded = "superseded"
    cancelled = "cancelled"


LIVE_RUN_STATUSES = (RunStatus.running, RunStatus.waiting_checkin, RunStatus.waiting_review)


class RunTrigger(StrEnum):
    scheduled = "scheduled"
    manual = "manual"


class PublishStatus(StrEnum):
    publishing = "publishing"
    published = "published"
    failed = "failed"


class DraftStatus(StrEnum):
    candidate = "candidate"
    rejected = "rejected"
    accepted = "accepted"
    approved = "approved"
    published = "published"
    discarded = "discarded"


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """String-backed enum with a CHECK constraint; avoids Postgres enum types in migrations."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=24,
        values_callable=lambda cls: [m.value for m in cls],
    )


class Day(TimestampMixin, Base):
    __tablename__ = "days"

    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    decision: Mapped[DayDecision] = mapped_column(
        _enum(DayDecision, "day_decision"), default=DayDecision.pending, nullable=False
    )
    no_post_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    quests: Mapped[list[Quest]] = relationship(
        back_populates="day", cascade="all, delete-orphan", order_by="Quest.position"
    )
    entries: Mapped[list[JournalEntry]] = relationship(
        back_populates="day", cascade="all, delete-orphan", order_by="JournalEntry.created_at"
    )
    checkins: Mapped[list[Checkin]] = relationship(
        back_populates="day", cascade="all, delete-orphan", order_by="Checkin.asked_at"
    )


class Quest(TimestampMixin, Base):
    __tablename__ = "quests"
    __table_args__ = (
        UniqueConstraint("local_date", "position", name="uq_quests_day_position"),
        CheckConstraint("position BETWEEN 1 AND 3", name="ck_quests_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_date: Mapped[date] = mapped_column(
        ForeignKey("days.local_date", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[QuestStatus] = mapped_column(
        _enum(QuestStatus, "quest_status"), default=QuestStatus.planned, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    day: Mapped[Day] = relationship(back_populates="quests")


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_date: Mapped[date] = mapped_column(
        ForeignKey("days.local_date", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[EntryKind] = mapped_column(_enum(EntryKind, "entry_kind"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[EntrySource] = mapped_column(
        _enum(EntrySource, "entry_source"), default=EntrySource.dashboard, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    day: Mapped[Day] = relationship(back_populates="entries")


class Checkin(Base):
    __tablename__ = "checkins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_date: Mapped[date] = mapped_column(
        ForeignKey("days.local_date", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"), index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    day: Mapped[Day] = relationship(back_populates="checkins")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index(
            "uq_pipeline_runs_live_per_day",
            "local_date",
            unique=True,
            postgresql_where=text("status IN ('running', 'waiting_checkin', 'waiting_review')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_date: Mapped[date] = mapped_column(
        ForeignKey("days.local_date", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    graph_version: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[RunTrigger] = mapped_column(_enum(RunTrigger, "run_trigger"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.running, nullable=False
    )
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    recover_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class PipelineStageRun(Base):
    __tablename__ = "pipeline_stage_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DraftRecord(TimestampMixin, Base):
    """A generated post draft. Authoritative for text, edits and ratings; the graph state only
    holds draft ids."""

    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", "label", name="uq_drafts_run_attempt_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    local_date: Mapped[date] = mapped_column(
        ForeignKey("days.local_date", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    edited_text: Mapped[str | None] = mapped_column(Text)
    style_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[DraftStatus] = mapped_column(
        _enum(DraftStatus, "draft_status"), default=DraftStatus.candidate, nullable=False
    )
    rating: Mapped[int | None] = mapped_column(Integer)
    feedback: Mapped[str | None] = mapped_column(Text)


class PublishedPost(Base):
    """One row per calendar day. The unique local_date is the duplicate guard: a second
    concurrent claim fails here, before any network call."""

    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_date: Mapped[date] = mapped_column(
        ForeignKey("days.local_date", ondelete="CASCADE"), nullable=False, unique=True
    )
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False
    )
    final_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    x_post_id: Mapped[str | None] = mapped_column(String(64))
    x_url: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[PublishStatus] = mapped_column(
        _enum(PublishStatus, "publish_status"), default=PublishStatus.publishing, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class XCredential(Base):
    """Single-row table (id = 1). Tokens are Fernet-encrypted with XBOT_SECRET_KEY."""

    __tablename__ = "x_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    x_user_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    scopes: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    needs_reconnect: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    code_verifier: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RepoSetting(TimestampMixin, Base):
    __tablename__ = "repo_settings"

    repo_full_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    include_diffs: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    excluded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )


class GithubCommit(Base):
    __tablename__ = "github_commits"

    sha: Mapped[str] = mapped_column(String(40), primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    local_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    authored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    author_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    author_login: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    html_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    diff: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GithubSync(Base):
    __tablename__ = "github_syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    repos_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    commits_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "Base",
    "Checkin",
    "Day",
    "DayDecision",
    "DraftRecord",
    "DraftStatus",
    "EntryKind",
    "EntrySource",
    "GithubCommit",
    "GithubSync",
    "JournalEntry",
    "LIVE_RUN_STATUSES",
    "OAuthState",
    "PipelineRun",
    "PipelineStageRun",
    "PublishStatus",
    "PublishedPost",
    "Quest",
    "QuestStatus",
    "RepoSetting",
    "RunStatus",
    "RunTrigger",
    "Setting",
    "TimestampMixin",
    "XCredential",
]
