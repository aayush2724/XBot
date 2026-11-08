"""Publisher contract and the failure classes the service reasons about.

The split matters: a `PublishNotSent` failure means X provably never created a post, so a retry
is safe; a `PublishMaybeSent` failure means X may have created it, so the service must look the
post up before it ever re-posts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field


class PostPayload(BaseModel):
    segments: list[str] = Field(min_length=1)  # one segment in MVP; threads later

    @property
    def text(self) -> str:
        return self.segments[0]


class PublishResult(BaseModel):
    external_id: str
    url: str | None = None


class PublishError(Exception):
    pass


class PublishNotSent(PublishError):
    """The request never reached X (connection refused, 429 before creation). Retry is safe."""


class PublishMaybeSent(PublishError):
    """X may have created the post (5xx, timeout, duplicate-content 403, unknown). Verify first."""


class PublishRejected(PublishError):
    """X refused definitively (4xx other than 429); do not retry automatically."""


class NotConnected(PublishError):
    """No X account is connected, or the connection needs re-authorisation."""


class Publisher(Protocol):
    name: str

    async def publish(self, payload: PostPayload) -> PublishResult: ...

    async def find_recent(self, text: str, since: datetime) -> PublishResult | None: ...
