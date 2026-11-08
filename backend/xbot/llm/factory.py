from __future__ import annotations

from xbot.config import Settings
from xbot.llm.anthropic_provider import AnthropicProvider
from xbot.llm.base import LLMProvider
from xbot.llm.mock_provider import MockLLMProvider
from xbot.llm.openrouter_provider import OpenRouterProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    if settings.llm_provider == "openrouter":
        return OpenRouterProvider(
            model=settings.llm_model,
            api_key=settings.openrouter_api_key,
            app_url=settings.public_url,
        )
    return AnthropicProvider(model=settings.llm_model, api_key=settings.anthropic_api_key)
