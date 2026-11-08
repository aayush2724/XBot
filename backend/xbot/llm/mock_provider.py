"""MOCK provider for development and tests. Every default output is prefixed with [MOCK] so it can
never be mistaken for real analysis. Tests script exact outputs or exceptions per stage."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from xbot.llm.base import Effort, LLMError, LLMResult, T, Usage


class MockLLMProvider:
    name = "mock"
    model = "mock-llm"

    def __init__(self) -> None:
        self._scripts: dict[str, list[BaseModel | Exception]] = {}
        self.calls: list[dict[str, Any]] = []

    def script(self, stage: str, *outputs: BaseModel | Exception) -> None:
        """Queue outputs (or exceptions to raise) for a stage, consumed in order."""
        self._scripts.setdefault(stage, []).extend(outputs)

    def calls_for(self, stage: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["stage"] == stage]

    async def complete_structured(
        self,
        *,
        system_shared: str,
        system_stage: str,
        user: str,
        schema: type[T],
        effort: Effort = "high",
        max_tokens: int = 16000,
        stage: str,
    ) -> LLMResult[T]:
        self.calls.append(
            {"stage": stage, "effort": effort, "schema": schema.__name__, "user": user}
        )
        queue = self._scripts.get(stage)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            parsed: BaseModel = item
        else:
            parsed = _default_output(schema, user)
        if not isinstance(parsed, schema):
            raise LLMError(f"mock output {type(parsed).__name__} does not match {schema.__name__}")
        usage = Usage(input_tokens=max(1, len(user) // 4), output_tokens=200)
        return LLMResult[T](parsed=parsed, model=self.model, usage=usage, cost_usd=0.0)


def _default_output(schema: type[BaseModel], user: str) -> BaseModel:
    """Plausible, clearly labelled defaults so the app runs end to end without an API key."""
    from xbot.intelligence import schemas as s

    body = user.split("\n", 1)[-1] if "\n" in user else user
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if schema is s.ActivityUnderstanding:
        items = [i for i in payload.get("items", []) if isinstance(i, dict) and i.get("ref_id")]
        facts = [
            s.Fact(
                kind="activity",
                text=f"[MOCK] {str(i.get('content', ''))[:120]}",
                confidence="confirmed" if i.get("status") != "planned" else "inferred",
                source_refs=[i["ref_id"]],
            )
            for i in items
        ]
        return s.ActivityUnderstanding(
            facts=facts,
            summary="[MOCK] summary of the day",
            sufficiency="enough" if facts else "insufficient",
            checkin_questions=[] if facts else ["[MOCK] What did you work on today?"],
        )
    if schema is s.InsightSet:
        ids = [f["id"] for f in payload.get("facts", []) if isinstance(f, dict) and f.get("id")]
        if not ids:
            return s.InsightSet(insights=[], nothing_worth_sharing=True, reason="[MOCK] no facts")
        return s.InsightSet(
            insights=[
                s.Insight(
                    title="[MOCK] Something you did today",
                    what_happened="[MOCK] what happened",
                    why_interesting="[MOCK] why it matters",
                    fact_ids=ids[:2],
                    scores=s.InsightScores(
                        technical_value=4, novelty=3, relatability=4, authenticity=5, relevance=4
                    ),
                    similar_post_id=None,
                )
            ],
            nothing_worth_sharing=False,
            reason="[MOCK] one plausible insight",
        )
    raise LLMError(f"mock provider has no default for schema {schema.__name__}")
