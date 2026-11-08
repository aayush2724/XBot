"""OpenRouter implementation (OpenAI-compatible chat completions).

Structured outputs use `response_format: json_schema` with a strict schema derived from the
stage's Pydantic model, and `provider.require_parameters` so OpenRouter never routes to a
backend that would ignore the schema. Pydantic validates every reply. If the backend rejects
strict schemas, the provider falls back once to JSON mode with the schema in the prompt and
stays in that mode for the rest of the process."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from pydantic import ValidationError

from xbot.llm.base import (
    Effort,
    LLMError,
    LLMRefusal,
    LLMResult,
    LLMTruncated,
    LLMUnavailable,
    T,
    Usage,
)
from xbot.llm.json_schema import strict_json_schema
from xbot.llm.pricing import estimate_cost
from xbot.logging import get_logger

log = get_logger("xbot.llm.openrouter")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high"}
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504, 529}


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        model: str,
        api_key: str | None,
        *,
        http: httpx.AsyncClient | None = None,
        retry_delays: tuple[float, ...] = (2.0,),
        app_url: str = "http://127.0.0.1:8000",
    ) -> None:
        if "/" not in model:
            raise ValueError(
                f"OpenRouter model ids look like 'vendor/model'; got {model!r}. "
                "Set XBOT_LLM_MODEL, e.g. google/gemini-3.5-flash-lite"
            )
        self.model = model
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        self._retry_delays = retry_delays
        self._app_url = app_url
        self._strict = True  # flips to False once if the backend rejects strict schemas

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
        if not self._api_key:
            raise LLMUnavailable("OPENROUTER_API_KEY is not configured")
        json_schema = strict_json_schema(schema)
        body = self._body(
            system_shared, system_stage, user, schema, json_schema, effort, max_tokens
        )
        data = await self._post(body)
        return self._parse(data, schema, stage, max_tokens)

    # -- request -----------------------------------------------------------------------------

    def _body(
        self,
        system_shared: str,
        system_stage: str,
        user: str,
        schema: type[T],
        json_schema: dict[str, Any],
        effort: Effort,
        max_tokens: int,
    ) -> dict[str, Any]:
        system = f"{system_shared}\n\n{system_stage}"
        if self._strict:
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": json_schema},
            }
        else:
            system += (
                "\n\nReply with a single JSON object and nothing else. It must match this JSON "
                f"schema exactly:\n{json.dumps(json_schema, sort_keys=True)}"
            )
            response_format = {"type": "json_object"}
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": response_format,
            "max_tokens": max_tokens,
            "reasoning": {"effort": _EFFORT[effort], "exclude": True},
            "provider": {"require_parameters": True},
            "usage": {"include": True},
        }

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": self._app_url,
            "X-Title": "XBot",
        }
        attempts = len(self._retry_delays) + 1
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self._http.post(API_URL, json=body, headers=headers)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                last = LLMUnavailable(f"could not reach OpenRouter: {e}")
            else:
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    if "error" in data and not data.get("choices"):
                        raise _map_error(int(data["error"].get("code") or 500), data["error"])
                    return data
                err = _error_body(resp)
                if resp.status_code == 400 and self._strict and _is_schema_rejection(err):
                    log.warning(
                        "strict schema rejected; switching to JSON mode", detail=str(err)[:200]
                    )
                    self._strict = False
                    return await self._post(_to_json_mode(body))
                if resp.status_code in TRANSIENT_STATUS:
                    last = _map_error(resp.status_code, err)
                else:
                    raise _map_error(resp.status_code, err)
            if i < attempts - 1:
                await asyncio.sleep(self._retry_delays[i])
        assert last is not None
        raise last

    # -- response ----------------------------------------------------------------------------

    def _parse(
        self, data: dict[str, Any], schema: type[T], stage: str, max_tokens: int
    ) -> LLMResult[T]:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("OpenRouter returned no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason") or choice.get("native_finish_reason") or "stop"
        if message.get("refusal"):
            raise LLMRefusal(str(message["refusal"])[:60])
        if finish == "content_filter":
            raise LLMRefusal("content_filter")
        content = message.get("content") or ""
        if isinstance(content, list):  # some backends return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if finish == "length":
            raise LLMTruncated(f"hit max_tokens={max_tokens} in {stage}")
        text = _FENCE.sub("", content.strip())
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as e:
            # call_stage retries LLMTruncated once at lower effort
            raise LLMTruncated(f"reply did not match {schema.__name__}: {str(e)[:300]}") from e

        u = data.get("usage") or {}
        prompt = int(u.get("prompt_tokens") or 0)
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage = Usage(
            input_tokens=max(0, prompt - cached),
            output_tokens=int(u.get("completion_tokens") or 0),
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )
        reported = u.get("cost")
        cost = (
            float(reported)
            if isinstance(reported, int | float)
            else estimate_cost(self.model, usage)
        )
        result = LLMResult[T](
            parsed=parsed,
            model=str(data.get("model") or self.model),
            usage=usage,
            cost_usd=round(cost, 6),
            request_id=str(data.get("id")) if data.get("id") else None,
            stop_reason=str(finish),
        )
        log.info(
            "llm call",
            stage=stage,
            model=result.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
            cost_usd=result.cost_usd,
            strict=self._strict,
        )
        return result


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {"message": resp.text[:300]}
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {"message": str(body)[:300]}


def _is_schema_rejection(err: dict[str, Any]) -> bool:
    text = json.dumps(err).lower()
    return any(k in text for k in ("response_format", "json_schema", "schema", "structured"))


def _to_json_mode(body: dict[str, Any]) -> dict[str, Any]:
    schema = body["response_format"]["json_schema"]["schema"]
    messages = [dict(m) for m in body["messages"]]
    messages[0]["content"] += (
        "\n\nReply with a single JSON object and nothing else. It must match this JSON schema "
        f"exactly:\n{json.dumps(schema, sort_keys=True)}"
    )
    return {**body, "messages": messages, "response_format": {"type": "json_object"}}


def _map_error(status: int, err: dict[str, Any]) -> LLMError:
    msg = str(err.get("message") or err)[:300]
    if status == 402:
        return LLMUnavailable(f"OpenRouter account is out of credits (402): {msg}")
    if status in (401, 403):
        return LLMUnavailable(f"OpenRouter rejected the API key ({status}): {msg}")
    if status in TRANSIENT_STATUS:
        return LLMUnavailable(f"OpenRouter temporarily unavailable ({status}): {msg}")
    return LLMError(f"OpenRouter error ({status}): {msg}")
