"""ORM models. Tables are added milestone by milestone; Alembic migrations live in ../alembic."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

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
    run_id: Mapped[int | None] = mapped_column(Integer)  # FK to pipeline_runs added in M3
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    day: Mapped[Day] = relationship(back_populates="checkins")


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
    "EntryKind",
    "EntrySource",
    "JournalEntry",
    "Quest",
    "QuestStatus",
    "Setting",
    "TimestampMixin",
]
