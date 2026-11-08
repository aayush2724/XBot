"""Memory inputs that personalise prompts: preferences, rated drafts, and (from M5) published
posts and category counts."""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.db.models import DraftRecord, PublishedPost, PublishStatus
from xbot.preferences import Preferences

MAX_GOOD_EXAMPLES = 5
MAX_BAD_EXAMPLES = 3


class RecentPost(BaseModel):
    id: str
    local_date: str
    category: str
    text: str


class RatedExample(BaseModel):
    text: str
    rating: int
    feedback: str | None = None


class MemoryInputs(BaseModel):
    voice_profile: str
    topics_to_explore: list[str]
    phrases_to_avoid: list[str]
    recent_posts: list[RecentPost] = Field(default_factory=list)
    category_counts_14d: dict[str, int] = Field(default_factory=dict)
    liked_examples: list[RatedExample] = Field(default_factory=list)
    disliked_examples: list[RatedExample] = Field(default_factory=list)
    last_categories: list[str] = Field(default_factory=list)  # within the cooldown window


def build_memory_inputs(prefs: Preferences) -> MemoryInputs:
    return MemoryInputs(
        voice_profile=prefs.voice_profile.strip()
        or "No explicit voice profile yet. Write plainly, like a developer's own notes.",
        topics_to_explore=prefs.topics_to_explore,
        phrases_to_avoid=prefs.phrases_to_avoid,
    )


async def load_memory_inputs(session: AsyncSession, prefs: Preferences) -> MemoryInputs:
    """Preferences plus the owner's best- and worst-rated past drafts as style examples."""
    memory = build_memory_inputs(prefs)
    rated = (
        (
            await session.execute(
                select(DraftRecord)
                .where(DraftRecord.rating.is_not(None))
                .order_by(DraftRecord.rating.desc(), DraftRecord.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    good = [r for r in rated if (r.rating or 0) >= 4][:MAX_GOOD_EXAMPLES]
    bad = sorted((r for r in rated if (r.rating or 0) <= 2), key=lambda r: r.rating or 0)[
        :MAX_BAD_EXAMPLES
    ]
    memory.liked_examples = [
        RatedExample(text=r.edited_text or r.text, rating=r.rating or 0, feedback=r.feedback)
        for r in good
    ]
    memory.disliked_examples = [
        RatedExample(text=r.edited_text or r.text, rating=r.rating or 0, feedback=r.feedback)
        for r in bad
    ]
    posts = (
        (
            await session.execute(
                select(PublishedPost)
                .where(PublishedPost.status == PublishStatus.published)
                .order_by(PublishedPost.local_date.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    memory.recent_posts = [
        RecentPost(
            id=str(p.id),
            local_date=p.local_date.isoformat(),
            category=p.category or "unknown",
            text=p.final_text,
        )
        for p in posts
    ]
    today = date.today()
    counts: dict[str, int] = {}
    for p in posts:
        if p.local_date >= today - timedelta(days=14) and p.category:
            counts[p.category] = counts.get(p.category, 0) + 1
    memory.category_counts_14d = counts
    cooldown_start = today - timedelta(days=prefs.category_cooldown_days)
    memory.last_categories = sorted(
        {p.category for p in posts if p.category and p.local_date >= cooldown_start}
    )
    return memory
