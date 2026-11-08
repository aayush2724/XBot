"""Memory inputs that personalise prompts. Published-post history joins in M5."""

from __future__ import annotations

from pydantic import BaseModel, Field

from xbot.preferences import Preferences


class RecentPost(BaseModel):
    id: str
    local_date: str
    category: str
    text: str


class MemoryInputs(BaseModel):
    voice_profile: str
    topics_to_explore: list[str]
    phrases_to_avoid: list[str]
    recent_posts: list[RecentPost] = Field(default_factory=list)
    category_counts_14d: dict[str, int] = Field(default_factory=dict)


def build_memory_inputs(prefs: Preferences) -> MemoryInputs:
    return MemoryInputs(
        voice_profile=prefs.voice_profile.strip()
        or "No explicit voice profile yet. Write plainly, like a developer's own notes.",
        topics_to_explore=prefs.topics_to_explore,
        phrases_to_avoid=prefs.phrases_to_avoid,
    )
