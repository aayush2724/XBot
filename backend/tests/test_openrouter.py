"""OpenRouter provider: strict schema translation, request payload, response parsing, cost
accounting and error mapping (HTTP mocked with respx; no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import make_test_settings
from xbot.intelligence import schemas as S
from xbot.llm.base import LLMError, LLMRefusal, LLMTruncated, LLMUnavailable
from xbot.llm.factory import build_provider
from xbot.llm.json_schema import strict_json_schema
from xbot.llm.openrouter_provider import API_URL, OpenRouterProvider

MODEL = "google/gemini-3.5-flash-lite"
STAGE_SCHEMAS = [S.ActivityUnderstanding, S.InsightSet, S.Strategy, S.DraftSet, S.Evaluation]


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("schema", STAGE_SCHEMAS, ids=lambda s: s.__name__)
def test_strict_schema_is_self_contained_and_closed(schema: Any) -> None:
    js = strict_json_schema(schema)
    text = json.dumps(js)
    assert "$ref" not in text and "$defs" not in text
    for node in _walk(js):
        assert "title" not in node and "default" not in node
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])


def test_strict_schema_keeps_enums_and_nullable() -> None:
    js = strict_json_schema(S.InsightSet)
    insight = js["properties"]["insights"]["items"]
    assert insight["properties"]["similar_post_id"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    strat = strict_json_schema(S.Strategy)
    assert "build_in_public" in strat["properties"]["category"]["enum"]


def _ok(content: Any, **usage: Any) -> httpx.Response:
    body = {
        "id": "gen-123",
        "model": MODEL,
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 600},
            **usage,
        },
    }
    return httpx.Response(200, json=body)


STRATEGY = {
    "category": "build_in_public",
    "angle": "a",
    "style": "plain_note",
    "target_chars": 200,
    "avoid": [],
    "reasoning": "r",
}


async def _call(provider: OpenRouterProvider, effort: Any = "high") -> Any:
    return await provider.complete_structured(
        system_shared="SHARED",
        system_stage="STAGE",
        user="evidence",
        schema=S.Strategy,
        effort=effort,
        stage="stage3",
    )


async def test_request_payload_and_parsed_result() -> None:
    with respx.mock() as mock:
        route = mock.post(API_URL).mock(return_value=_ok(json.dumps(STRATEGY), cost=0.00042))
        async with httpx.AsyncClient() as http:
            provider = OpenRouterProvider(MODEL, "sk-or-test", http=http, retry_delays=())
            result = await _call(provider, effort="xhigh")
    req = route.calls.last.request
    body = json.loads(req.content)
    assert req.headers["authorization"] == "Bearer sk-or-test"
    assert body["model"] == MODEL
    assert body["messages"][0] == {"role": "system", "content": "SHARED\n\nSTAGE"}
    assert body["messages"][1] == {"role": "user", "content": "evidence"}
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "Strategy"
    assert rf["json_schema"]["schema"] == strict_json_schema(S.Strategy)
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"effort": "high", "exclude": True}  # xhigh → high
    assert result.parsed == S.Strategy.model_validate(STRATEGY)
    assert result.usage.input_tokens == 400 and result.usage.cache_read_tokens == 600
    assert result.cost_usd == pytest.approx(0.00042)  # OpenRouter-reported cost wins
    assert result.request_id == "gen-123"


async def test_cost_falls_back_to_pricing_table_and_strips_fences() -> None:
    fenced = "```json\n" + json.dumps(STRATEGY) + "\n```"
    with respx.mock() as mock:
        mock.post(API_URL).mock(return_value=_ok(fenced))
        async with httpx.AsyncClient() as http:
            result = await _call(OpenRouterProvider(MODEL, "k", http=http, retry_delays=()))
    # 400*0.30 + 200*2.50 + 600*0.03 = 120 + 500 + 18 = 638 / 1e6
    assert result.cost_usd == pytest.approx(0.000638)


@pytest.mark.parametrize(
    ("status", "exc", "fragment"),
    [
        (402, LLMUnavailable, "out of credits"),
        (401, LLMUnavailable, "API key"),
        (429, LLMUnavailable, "temporarily"),
        (503, LLMUnavailable, "temporarily"),
        (400, LLMError, "OpenRouter error"),
    ],
)
async def test_http_errors_map_to_typed_errors(
    status: int, exc: type[Exception], fragment: str
) -> None:
    with respx.mock() as mock:
        mock.post(API_URL).mock(
            return_value=httpx.Response(status, json={"error": {"code": status, "message": "nope"}})
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(exc, match=fragment):
                await _call(OpenRouterProvider(MODEL, "k", http=http, retry_delays=()))


async def test_transient_error_is_retried_once() -> None:
    with respx.mock() as mock:
        route = mock.post(API_URL).mock(
            side_effect=[
                httpx.Response(503, json={"error": {"message": "busy"}}),
                _ok(json.dumps(STRATEGY)),
            ]
        )
        async with httpx.AsyncClient() as http:
            result = await _call(OpenRouterProvider(MODEL, "k", http=http, retry_delays=(0.0,)))
    assert route.call_count == 2 and result.parsed.category == "build_in_public"


async def test_schema_rejection_falls_back_to_json_mode_and_stays_there() -> None:
    with respx.mock() as mock:
        route = mock.post(API_URL).mock(
            side_effect=[
                httpx.Response(
                    400,
                    json={"error": {"code": 400, "message": "Invalid response_format json_schema"}},
                ),
                _ok(json.dumps(STRATEGY)),
                _ok(json.dumps(STRATEGY)),
            ]
        )
        async with httpx.AsyncClient() as http:
            provider = OpenRouterProvider(MODEL, "k", http=http, retry_delays=())
            await _call(provider)
            await _call(provider)
    second, third = (json.loads(c.request.content) for c in route.calls[1:])
    assert second["response_format"] == {"type": "json_object"}
    assert '"category"' in second["messages"][0]["content"]  # schema moved into the prompt
    assert third["response_format"] == {"type": "json_object"}


async def test_invalid_json_length_and_refusal() -> None:
    async with httpx.AsyncClient() as http:
        provider = OpenRouterProvider(MODEL, "k", http=http, retry_delays=())
        with respx.mock() as mock:
            mock.post(API_URL).mock(return_value=_ok('{"category": "not-a-category"}'))
            with pytest.raises(LLMTruncated, match="did not match Strategy"):
                await _call(provider)
        with respx.mock() as mock:
            resp = {
                "choices": [{"finish_reason": "length", "message": {"content": '{"cat'}}],
                "usage": {},
            }
            mock.post(API_URL).mock(return_value=httpx.Response(200, json=resp))
            with pytest.raises(LLMTruncated, match="max_tokens"):
                await _call(provider)
        with respx.mock() as mock:
            resp = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": None, "refusal": "I can't help"},
                    }
                ]
            }
            mock.post(API_URL).mock(return_value=httpx.Response(200, json=resp))
            with pytest.raises(LLMRefusal):
                await _call(provider)


async def test_missing_key_is_unavailable_and_bad_model_id_is_rejected() -> None:
    with pytest.raises(LLMUnavailable, match="OPENROUTER_API_KEY"):
        await _call(OpenRouterProvider(MODEL, None, retry_delays=()))
    with pytest.raises(ValueError, match="vendor/model"):
        OpenRouterProvider("claude-opus-5", "k")


def test_settings_and_factory() -> None:
    s = make_test_settings(llm_provider="openrouter", llm_model=MODEL, openrouter_api_key="k")
    assert s.llm_configured and isinstance(build_provider(s), OpenRouterProvider)
    assert make_test_settings(llm_provider="openrouter", llm_model=MODEL).llm_configured is False
    bad = make_test_settings(
        env="dev",
        secret_key="x" * 40,
        dashboard_password="pw-strong",
        llm_provider="openrouter",
        llm_model="claude-opus-5",
        publisher="x",
    )
    assert any("vendor/model" in p or "google/gemini" in p for p in bad.runtime_problems())
