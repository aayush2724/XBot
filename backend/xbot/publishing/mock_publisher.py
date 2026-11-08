"""MOCK publisher for development and tests. Never touches the network. Supports failure
injection so the service's idempotency paths can be exercised."""

from __future__ import annotations

from datetime import UTC, datetime

from xbot.publishing.base import PostPayload, PublishResult


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


class MockPublisher:
    name = "mock"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, datetime]] = []  # (id, text, when)
        self.publish_calls = 0
        self._failures: list[Exception] = []
        self._record_before_failing: list[bool] = []

    def fail_next(self, exc: Exception, *, record_as_sent: bool = False) -> None:
        """Make the next publish raise `exc`. With record_as_sent=True the post is recorded as
        created first, simulating a crash or timeout after X accepted it."""
        self._failures.append(exc)
        self._record_before_failing.append(record_as_sent)

    async def publish(self, payload: PostPayload) -> PublishResult:
        self.publish_calls += 1
        if self._failures:
            exc = self._failures.pop(0)
            record = self._record_before_failing.pop(0)
            if record:
                self.sent.append((f"mock-{len(self.sent) + 1}", payload.text, datetime.now(UTC)))
            raise exc
        post_id = f"mock-{len(self.sent) + 1}"
        self.sent.append((post_id, payload.text, datetime.now(UTC)))
        return PublishResult(external_id=post_id, url=f"https://x.com/mock/status/{post_id}")

    async def find_recent(self, text: str, since: datetime) -> PublishResult | None:
        target = _norm(text)
        for post_id, sent_text, when in reversed(self.sent):
            if when >= since and _norm(sent_text) == target:
                return PublishResult(
                    external_id=post_id, url=f"https://x.com/mock/status/{post_id}"
                )
        return None
