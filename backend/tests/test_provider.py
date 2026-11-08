from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from xbot.llm.anthropic_provider import AnthropicProvider
from xbot.llm.base import Effort, LLMError, LLMRefusal, LLMTruncated, Usage
from xbot.llm.pricing import estimate_cost


class Out(BaseModel):
    summary: str
    score: int


class FakeMessages:
    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.kwargs: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


class FakeClient:
    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.messages = FakeMessages(response, exc)


def _response(**overrides: Any) -> SimpleNamespace:
    base = {
        "stop_reason": "end_turn",
        "stop_details": None,
        "parsed_output": Out(summary="ok", score=3),
        "usage": SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=5000,
            cache_creation_input_tokens=600,
        ),
        "model": "claude-opus-5",
        "_request_id": "req_123",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _call(client: FakeClient, effort: Effort = "high") -> Any:
    provider = AnthropicProvider(model="claude-opus-5", client=client)
    return await provider.complete_structured(
        system_shared="SHARED" * 100,
        system_stage="STAGE",
        user="hello",
        schema=Out,
        effort=effort,
        stage="stage1",
    )


def test_pricing_separates_cached_and_uncached_tokens() -> None:
    usage = Usage(
        input_tokens=1000, output_tokens=200, cache_read_tokens=5000, cache_write_tokens=600
    )
    # 1000*5 + 200*25 + 5000*0.5 + 600*6.25 = 5000 + 5000 + 2500 + 3750 = 16250 / 1e6
    assert estimate_cost("claude-opus-5", usage) == pytest.approx(0.01625)
    assert estimate_cost("claude-sonnet-5", Usage(input_tokens=1_000_000)) == pytest.approx(2.0)
    assert estimate_cost("unknown-model", Usage(output_tokens=1_000_000)) == pytest.approx(25.0)


async def test_request_payload_has_cache_breakpoint_thinking_effort_and_schema() -> None:
    client = FakeClient(_response())
    result = await _call(client, effort="medium")
    kw = client.messages.kwargs
    assert kw["model"] == "claude-opus-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["output_format"] is Out
    assert kw["max_tokens"] == 16000
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in kw["system"][1]
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    assert result.parsed == Out(summary="ok", score=3)
    assert result.usage == Usage(
        input_tokens=1000, output_tokens=200, cache_read_tokens=5000, cache_write_tokens=600
    )
    assert result.cost_usd == pytest.approx(0.01625)
    assert result.request_id == "req_123"


async def test_refusal_is_typed() -> None:
    client = FakeClient(
        _response(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    )
    with pytest.raises(LLMRefusal) as exc:
        await _call(client)
    assert exc.value.category == "cyber"


async def test_max_tokens_is_truncation() -> None:
    client = FakeClient(_response(stop_reason="max_tokens", parsed_output=None))
    with pytest.raises(LLMTruncated):
        await _call(client)


async def test_unparseable_json_is_truncation() -> None:
    client = FakeClient(exc=ValueError("Expecting value"))
    with pytest.raises(LLMTruncated):
        await _call(client)


async def test_missing_parsed_output_is_error() -> None:
    client = FakeClient(_response(parsed_output=None))
    with pytest.raises(LLMError):
        await _call(client)
