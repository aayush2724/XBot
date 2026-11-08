"""Approve → publish, designed so no code path can create two X posts for one day.

1. Claim the day in its own committed transaction (INSERT, or a conditional UPDATE of a failed
   or stale claim). The unique index on local_date makes a concurrent second claim fail before
   any network call.
2. Call the publisher outside any transaction.
3. Classify failures: NotSent → bounded automatic retry; MaybeSent → look the post up with
   find_recent and record it if found; Rejected / NotConnected → fail, manual retry later.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from xbot.db.base import Database
from xbot.db.models import (
    Day,
    DayDecision,
    DraftRecord,
    DraftStatus,
    PublishedPost,
    PublishStatus,
)
from xbot.logging import get_logger
from xbot.publishing.base import (
    NotConnected,
    PostPayload,
    Publisher,
    PublishError,
    PublishMaybeSent,
    PublishNotSent,
    PublishRejected,
    PublishResult,
)

log = get_logger("xbot.publishing")

STALE_CLAIM = timedelta(minutes=5)
NOT_SENT_RETRIES = 2
RETRY_DELAYS = (1.0, 2.0)
LOOKBACK = timedelta(hours=1)


class AlreadyPublished(PublishError):
    def __init__(self, post: PublishedPost) -> None:
        self.post = post
        super().__init__(f"a post for {post.local_date} is already published ({post.x_post_id})")


class PublishInProgress(PublishError):
    pass


async def _claim(db: Database, draft: DraftRecord, final_text: str) -> PublishedPost:
    async with db.session_factory() as session:
        row = PublishedPost(
            local_date=draft.local_date,
            draft_id=draft.id,
            final_text=final_text,
            category=draft.category,
            status=PublishStatus.publishing,
            attempts=1,
        )
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
            return row
        except IntegrityError:
            await session.rollback()
    async with db.session_factory() as session:
        existing = (
            await session.execute(
                select(PublishedPost).where(PublishedPost.local_date == draft.local_date)
            )
        ).scalar_one()
        if existing.status == PublishStatus.published:
            raise AlreadyPublished(existing)
        stale_before = datetime.now(UTC) - STALE_CLAIM
        stmt = (
            update(PublishedPost)
            .where(
                PublishedPost.id == existing.id,
                (PublishedPost.status == PublishStatus.failed)
                | (
                    (PublishedPost.status == PublishStatus.publishing)
                    & (PublishedPost.updated_at < stale_before)
                ),
            )
            .values(
                status=PublishStatus.publishing,
                draft_id=draft.id,
                final_text=final_text,
                category=draft.category,
                attempts=PublishedPost.attempts + 1,
                last_error=None,
                updated_at=datetime.now(UTC),
            )
            .returning(PublishedPost.id)
        )
        claimed = (await session.execute(stmt)).first()
        await session.commit()
        if claimed is None:
            raise PublishInProgress(
                f"a publish attempt for {draft.local_date} is already in progress"
            )
        return (
            await session.execute(select(PublishedPost).where(PublishedPost.id == existing.id))
        ).scalar_one()


async def _finish(
    db: Database, post_id: int, *, result: PublishResult | None, error: str | None
) -> PublishedPost:
    async with db.session_factory() as session:
        post = await session.get(PublishedPost, post_id)
        assert post is not None
        if result is not None:
            post.status = PublishStatus.published
            post.x_post_id = result.external_id
            post.x_url = result.url
            post.last_error = None
            post.published_at = datetime.now(UTC)
            draft = await session.get(DraftRecord, post.draft_id)
            if draft is not None:
                draft.status = DraftStatus.published
            day = await session.get(Day, post.local_date)
            if day is not None:
                day.decision = DayDecision.post
                day.decided_at = datetime.now(UTC)
                day.no_post_reason = None
        else:
            post.status = PublishStatus.failed
            post.last_error = (error or "unknown error")[:2000]
        post.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(post)
        return post


async def publish_draft(
    db: Database,
    publisher: Publisher,
    draft_id: int,
    *,
    retry_delays: tuple[float, ...] = RETRY_DELAYS,
) -> PublishedPost:
    async with db.session_factory() as session:
        draft = await session.get(DraftRecord, draft_id)
        if draft is None:
            raise PublishError(f"draft {draft_id} not found")
        final_text = (draft.edited_text or draft.text).strip()
    claim = await _claim(db, draft, final_text)
    started = datetime.now(UTC)
    payload = PostPayload(segments=[final_text])

    attempt = 0
    while True:
        try:
            result = await publisher.publish(payload)
        except PublishNotSent as e:
            attempt += 1
            if attempt <= NOT_SENT_RETRIES:
                log.warning("publish not sent, retrying", error=str(e), attempt=attempt)
                await asyncio.sleep(retry_delays[min(attempt - 1, len(retry_delays) - 1)])
                continue
            return await _finish(db, claim.id, result=None, error=f"not sent after retries: {e}")
        except PublishMaybeSent as e:
            log.warning("publish uncertain, verifying", error=str(e))
            found = await publisher.find_recent(final_text, since=started - LOOKBACK)
            if found is not None:
                log.info("publish verified via timeline", post_id=found.external_id)
                return await _finish(db, claim.id, result=found, error=None)
            return await _finish(
                db, claim.id, result=None, error=f"uncertain and not found on timeline: {e}"
            )
        except (PublishRejected, NotConnected) as e:
            return await _finish(db, claim.id, result=None, error=str(e))
        return await _finish(db, claim.id, result=result, error=None)


async def retry_published(db: Database, publisher: Publisher, post_id: int) -> PublishedPost:
    async with db.session_factory() as session:
        post = await session.get(PublishedPost, post_id)
        if post is None:
            raise PublishError(f"published post {post_id} not found")
        if post.status == PublishStatus.published:
            raise AlreadyPublished(post)
        draft_id = post.draft_id
    return await publish_draft(db, publisher, draft_id)
