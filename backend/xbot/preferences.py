"""User preferences stored in the `settings` table as one JSON value per key."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.db.models import Setting
from xbot.logging import get_logger

log = get_logger("xbot.preferences")

AUTONOMOUS_CONFIRMATION = "I UNDERSTAND XBOT WILL POST WITHOUT ASKING"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PublishingMode(StrEnum):
    approval = "approval"
    autonomous = "autonomous"


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publishing_mode: PublishingMode = PublishingMode.approval
    max_post_chars: int = Field(default=280, ge=50, le=25000)
    allow_links: bool = False
    evening_time: str = "20:00"
    github_username: str | None = Field(default=None, max_length=64)
    github_author_emails: list[str] = Field(default_factory=list, max_length=10)
    extra_repos: list[str] = Field(default_factory=list, max_length=50)
    min_insight_score: float = Field(default=3.0, ge=1.0, le=5.0)
    category_cooldown_days: int = Field(default=3, ge=0, le=30)
    voice_profile: str = Field(default="", max_length=4000)
    topics_to_explore: list[str] = Field(default_factory=list, max_length=50)
    phrases_to_avoid: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evening_time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("must be HH:MM in 24-hour time")
        return v

    @field_validator("extra_repos")
    @classmethod
    def _repos(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if not _REPO.match(r)]
        if bad:
            raise ValueError(f"repos must look like owner/name: {bad}")
        return v

    @field_validator("github_author_emails")
    @classmethod
    def _emails(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if not _EMAIL.match(e)]
        if bad:
            raise ValueError(f"invalid email addresses: {bad}")
        return [e.lower() for e in v]

    @field_validator("topics_to_explore", "phrases_to_avoid")
    @classmethod
    def _short_strings(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s.strip()]
        if any(len(s) > 200 for s in cleaned):
            raise ValueError("items must be 200 characters or fewer")
        return cleaned


class PreferencesUpdate(BaseModel):
    """Partial update: only the provided fields change."""

    model_config = ConfigDict(extra="forbid")

    publishing_mode: PublishingMode | None = None
    max_post_chars: int | None = None
    allow_links: bool | None = None
    evening_time: str | None = None
    github_username: str | None = None
    github_author_emails: list[str] | None = None
    extra_repos: list[str] | None = None
    min_insight_score: float | None = None
    category_cooldown_days: int | None = None
    voice_profile: str | None = None
    topics_to_explore: list[str] | None = None
    phrases_to_avoid: list[str] | None = None


class ConfirmationRequired(Exception):
    """Raised when switching to autonomous publishing without the confirmation phrase."""


async def load_preferences(session: AsyncSession) -> Preferences:
    rows = (await session.execute(select(Setting))).scalars().all()
    data: dict[str, Any] = {}
    for row in rows:
        if row.key in Preferences.model_fields:
            data[row.key] = row.value
    try:
        return Preferences.model_validate(data)
    except ValueError:
        # A stored value no longer fits the schema (e.g. after a code change). Drop the bad keys
        # one at a time rather than failing every request.
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            try:
                Preferences.model_validate({**cleaned, key: value})
                cleaned[key] = value
            except ValueError:
                log.warning("ignoring invalid stored preference", key=key)
        return Preferences.model_validate(cleaned)


async def save_preferences(
    session: AsyncSession, update: PreferencesUpdate, *, confirmation: str | None = None
) -> Preferences:
    current = await load_preferences(session)
    changes = update.model_dump(exclude_none=True)
    if (
        changes.get("publishing_mode") == PublishingMode.autonomous
        and current.publishing_mode != PublishingMode.autonomous
        and confirmation != AUTONOMOUS_CONFIRMATION
    ):
        raise ConfirmationRequired(
            f"switching to autonomous publishing requires confirmation={AUTONOMOUS_CONFIRMATION!r}"
        )
    merged = Preferences.model_validate({**current.model_dump(mode="json"), **changes})
    merged_json = merged.model_dump(mode="json")
    for key in changes:
        stmt = insert(Setting).values(key=key, value=merged_json[key])
        stmt = stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value}
        )
        await session.execute(stmt)
    await session.commit()
    if "publishing_mode" in changes:
        log.warning("publishing mode changed", mode=merged.publishing_mode.value)
    return merged
