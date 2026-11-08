"""Structured outputs for the pipeline stages. Keep these JSON-schema friendly: no numeric
constraints, no datetimes. Ranges are enforced in guards.py."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Confidence = Literal["confirmed", "inferred", "unknown"]
FactKind = Literal[
    "activity", "project", "technology", "concept", "problem", "discovery", "unfinished"
]
Sufficiency = Literal["enough", "thin", "insufficient"]


class Fact(BaseModel):
    kind: FactKind
    text: str
    confidence: Confidence
    source_refs: list[str]


class ActivityUnderstanding(BaseModel):
    facts: list[Fact]
    summary: str
    sufficiency: Sufficiency
    checkin_questions: list[str]


class FactWithId(Fact):
    id: str


class InsightScores(BaseModel):
    technical_value: int
    novelty: int
    relatability: int
    authenticity: int
    relevance: int


class Insight(BaseModel):
    title: str
    what_happened: str
    why_interesting: str
    fact_ids: list[str]
    scores: InsightScores
    similar_post_id: str | None


class InsightSet(BaseModel):
    insights: list[Insight]
    nothing_worth_sharing: bool
    reason: str


class ScoredInsight(BaseModel):
    insight: Insight
    weighted_score: float
    eligible: bool
    ineligible_reason: str | None = None


# ---- Stages 3–5 ------------------------------------------------------------------------------

Category = Literal[
    "daily_learning",
    "build_in_public",
    "ai_ml_discovery",
    "engineering_lesson",
    "dsa_journey",
    "daily_quest",
    "project_update",
    "personal_reflection",
    "leadership",
    "weekly_summary",
]
Style = Literal[
    "plain_note", "mini_story", "question_led", "before_after", "code_mention", "reflection"
]


class Strategy(BaseModel):
    category: Category
    angle: str
    style: Style
    target_chars: int
    avoid: list[str]
    reasoning: str


class Claim(BaseModel):
    sentence: str
    kind: Literal["fact", "opinion"]
    fact_ids: list[str]
    hedged: bool


class Draft(BaseModel):
    label: str
    text: str
    style_note: str
    claims: list[Claim]


class DraftSet(BaseModel):
    drafts: list[Draft]


class GroundingCheck(BaseModel):
    claim: str
    supported: bool
    note: str


class Evaluation(BaseModel):
    grounding: list[GroundingCheck]
    worth_sharing: int
    sounds_human: int
    technically_correct: bool
    repetitive: bool
    reveals_sensitive: bool
    issues: list[str]
    verdict: Literal["accept", "revise", "reject"]
    revision_hint: str
