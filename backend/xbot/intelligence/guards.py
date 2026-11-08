"""Deterministic checks between stages. The model proposes; code enforces."""

from __future__ import annotations

from pydantic import BaseModel

from xbot.ingestion.base import DayContext
from xbot.intelligence.schemas import Fact, FactWithId, Insight, InsightSet, ScoredInsight

AUTHENTICITY_CAP_FOR_UNCONFIRMED = 2
SCORE_WEIGHTS = {
    "technical_value": 1.0,
    "novelty": 1.0,
    "relatability": 1.0,
    "authenticity": 1.5,
    "relevance": 1.0,
}


class GuardReport(BaseModel):
    dropped: list[str]
    adjusted: list[str]


def apply_fact_guards(facts: list[Fact], ctx: DayContext) -> tuple[list[FactWithId], GuardReport]:
    valid_refs = ctx.ref_ids
    planned_refs = {i.ref_id for i in ctx.items if i.source == "quest" and i.status == "planned"}
    kept: list[FactWithId] = []
    dropped: list[str] = []
    adjusted: list[str] = []
    for fact in facts:
        refs = [r for r in fact.source_refs if r in valid_refs]
        if not refs:
            dropped.append(f"no valid source_refs: {fact.text[:80]}")
            continue
        confidence = fact.confidence
        if all(r in planned_refs for r in refs) and confidence != "unknown":
            confidence = "unknown"
            adjusted.append(f"planned-quest-only fact downgraded to unknown: {fact.text[:80]}")
        kept.append(
            FactWithId(
                id=f"F{len(kept) + 1}",
                kind=fact.kind,
                text=fact.text.strip(),
                confidence=confidence,
                source_refs=refs,
            )
        )
    return kept, GuardReport(dropped=dropped, adjusted=adjusted)


def _clamp(v: int) -> int:
    return max(1, min(5, v))


def score_insights(
    insight_set: InsightSet, facts: list[FactWithId]
) -> tuple[list[ScoredInsight], GuardReport]:
    by_id = {f.id: f for f in facts}
    scored: list[ScoredInsight] = []
    dropped: list[str] = []
    adjusted: list[str] = []
    for ins in insight_set.insights:
        ids = [i for i in ins.fact_ids if i in by_id]
        if not ids:
            dropped.append(f"insight cites no existing facts: {ins.title[:80]}")
            continue
        scores = ins.scores.model_copy(
            update={k: _clamp(getattr(ins.scores, k)) for k in SCORE_WEIGHTS}
        )
        cited = [by_id[i] for i in ids]
        has_confirmed = any(f.confidence == "confirmed" for f in cited)
        ineligible: str | None = None
        if not has_confirmed:
            if scores.authenticity > AUTHENTICITY_CAP_FOR_UNCONFIRMED:
                scores = scores.model_copy(
                    update={"authenticity": AUTHENTICITY_CAP_FOR_UNCONFIRMED}
                )
                adjusted.append(f"authenticity capped (no confirmed facts): {ins.title[:80]}")
            ineligible = "rests only on inferred or unknown facts"
        total = sum(getattr(scores, k) * w for k, w in SCORE_WEIGHTS.items())
        weighted = round(total / sum(SCORE_WEIGHTS.values()), 3)
        scored.append(
            ScoredInsight(
                insight=ins.model_copy(update={"fact_ids": ids, "scores": scores}),
                weighted_score=weighted,
                eligible=ineligible is None,
                ineligible_reason=ineligible,
            )
        )
    scored.sort(key=lambda s: s.weighted_score, reverse=True)
    return scored, GuardReport(dropped=dropped, adjusted=adjusted)


def select_insight(
    insight_set: InsightSet, scored: list[ScoredInsight], min_score: float
) -> tuple[Insight | None, str]:
    """Return the chosen insight or (None, reason)."""
    if insight_set.nothing_worth_sharing:
        return None, f"model judged nothing worth sharing: {insight_set.reason}"
    eligible = [s for s in scored if s.eligible]
    if not eligible:
        return None, "no insight rests on a confirmed fact"
    best = eligible[0]
    if best.weighted_score < min_score:
        return None, (
            f"best insight scored {best.weighted_score} below threshold {min_score}: "
            f"{best.insight.title}"
        )
    return best.insight, f"selected '{best.insight.title}' (score {best.weighted_score})"
