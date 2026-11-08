"""Provider-neutral LLM contract: one structured-completion call, typed result, typed errors."""

from __future__ import annotations

from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
Effort = Literal["low", "medium", "high", "xhigh"]


class Usage(BaseModel):
    input_tokens: int = 0  # uncached input tokens
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMResult[R: BaseModel](BaseModel):
    parsed: R
    model: str
    usage: Usage
    cost_usd: float
    request_id: str | None = None
    stop_reason: str = "end_turn"


class LLMError(Exception):
    """Base class for provider failures the pipeline reports."""


class LLMUnavailable(LLMError):
    """Auth, network or rate-limit failure after the SDK's own retries (transient class)."""


class LLMTruncated(LLMError):
    """The model hit max_tokens before finishing; retry once at lower effort."""


class LLMRefusal(LLMError):
    def __init__(self, category: str | None) -> None:
        self.category = category
        super().__init__(f"model refused (category={category or 'unknown'})")


TRANSIENT_ERROR_MARKERS = ("LLMUnavailable", "TimeoutError", "ConnectError", "NodeTimeoutError")


class LLMProvider(Protocol):
    name: str
    model: str

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
    ) -> LLMResult[T]: ...
