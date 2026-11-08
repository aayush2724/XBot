from __future__ import annotations

from xbot.config import Settings
from xbot.llm.anthropic_provider import AnthropicProvider
from xbot.llm.base import LLMProvider
from xbot.llm.mock_provider import MockLLMProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    return AnthropicProvider(model=settings.llm_model, api_key=settings.anthropic_api_key)
