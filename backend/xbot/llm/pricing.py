"""USD per million tokens. Anthropic first-party rates, cached 2026-06. Update when prices move."""

from __future__ import annotations

from xbot.llm.base import Usage

# model -> (uncached input, output, cache read, cache write)
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}
FALLBACK = PRICES_PER_MTOK["claude-opus-5"]  # conservative when a model id is unknown


def estimate_cost(model: str, usage: Usage) -> float:
    key = next((k for k in PRICES_PER_MTOK if model.startswith(k)), None)
    inp, out, cread, cwrite = PRICES_PER_MTOK[key] if key else FALLBACK
    cost = (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * cread
        + usage.cache_write_tokens * cwrite
    ) / 1_000_000
    return round(cost, 6)
