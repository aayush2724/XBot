"""Anthropic SDK implementation. Structured outputs via messages.parse, adaptive thinking, and one
cache breakpoint on the shared system block so every stage of the day reuses the prefix.

Only this module knows the Anthropic SDK; nodes see the LLMProvider protocol."""

from __future__ import annotations

from typing import Any

import anthropic

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
from xbot.llm.pricing import estimate_cost
from xbot.logging import get_logger

log = get_logger("xbot.llm.anthropic")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, client: Any | None = None) -> None:
        self.model = model
        # A bare AsyncAnthropic() resolves ANTHROPIC_API_KEY or an `ant auth login` profile.
        if client is not None:
            self._client = client
        elif api_key:
            self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
        else:
            self._client = anthropic.AsyncAnthropic(max_retries=2)

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
        system = [
            {"type": "text", "text": system_shared, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system_stage},
        ]
        try:
            response = await self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
            )
        except anthropic.AuthenticationError as e:
            raise LLMUnavailable("Anthropic rejected the API key") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMUnavailable("Anthropic API key lacks permission") from e
        except anthropic.RateLimitError as e:
            raise LLMUnavailable("Anthropic rate limit after SDK retries") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMUnavailable(f"Anthropic server error {e.status_code}") from e
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMUnavailable("could not reach the Anthropic API") from e
        except anthropic.AnthropicError as e:
            raise LLMError(f"Anthropic SDK error: {e}") from e
        except ValueError as e:
            # The SDK could not parse the structured JSON; most often a max_tokens cut.
            raise LLMTruncated(f"unparseable structured output: {e}") from e

        stop_reason = getattr(response, "stop_reason", None) or "end_turn"
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefusal(getattr(details, "category", None))
        if stop_reason == "max_tokens":
            raise LLMTruncated(f"hit max_tokens={max_tokens} in {stage}")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError("response contained no parsed structured output")
        if not isinstance(parsed, schema):
            parsed = schema.model_validate(parsed)

        u = response.usage
        usage = Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        result = LLMResult[T](
            parsed=parsed,
            model=str(getattr(response, "model", self.model)),
            usage=usage,
            cost_usd=estimate_cost(self.model, usage),
            request_id=getattr(response, "_request_id", None) or getattr(response, "id", None),
            stop_reason=stop_reason,
        )
        log.info(
            "llm call",
            stage=stage,
            model=result.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
            cost_usd=result.cost_usd,
        )
        return result
