"""M5: publishing service idempotency, mock publisher, graph publish node, history and stats."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx
from sqlalchemy import select, text

from xbot.db.models import (
    Day,
    DayDecision,
    DraftRecord,
    DraftStatus,
    PipelineRun,
    PublishedPost,
    PublishStatus,
    RunStatus,
    RunTrigger,
)
from xbot.publishing.base import NotConnected, PublishMaybeSent, PublishNotSent, PublishRejected
from xbot.publishing.mock_publisher import MockPublisher
from xbot.publishing.service import AlreadyPublished, publish_draft, retry_published
from xbot.services.days import get_or_create_day

D = date(2026, 11, 8)


async def _draft(
    app: Any, local_date: date = D, text_: str = "Shipped the publisher today."
) -> int:
    """Create (or reuse) a finished run for the day and add an accepted draft to it."""
    async with app.state.db.session_factory() as session:
        await get_or_create_day(session, local_date)
        run = (
            (await session.execute(select(PipelineRun).where(PipelineRun.local_date == local_date)))
            .scalars()
            .first()
        )
        if run is None:
            run = PipelineRun(
                local_date=local_date,
                thread_id=f"run:pub-{local_date}",
                graph_version="v3",
                trigger=RunTrigger.manual,
                status=RunStatus.no_post,
            )
            session.add(run)
            await session.flush()
        existing = (
            (await session.execute(select(DraftRecord).where(DraftRecord.run_id == run.id)))
            .scalars()
            .all()
        )
        label = "ABCDEFGH"[len(existing)]
        draft = DraftRecord(
            run_id=run.id,
            local_date=local_date,
            attempt=1,
            label=label,
            category="build_in_public",
            text=text_,
            style_note="",
            claims=[],
            status=DraftStatus.accepted,
        )
        session.add(draft)
        await session.commit()
        return draft.id


async def test_publish_success_marks_post_draft_and_day(app: Any) -> None:
    pub = MockPublisher()
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert (
        post.status == PublishStatus.published and post.x_post_id == "mock-1" and post.attempts == 1
    )
    assert pub.publish_calls == 1
    async with app.state.db.session_factory() as session:
        draft = await session.get(DraftRecord, draft_id)
        day = await session.get(Day, D)
        assert draft is not None and draft.status == DraftStatus.published
        assert day is not None and day.decision == DayDecision.post


async def test_second_publish_for_same_day_is_blocked_before_network(app: Any) -> None:
    pub = MockPublisher()
    first = await _draft(app, text_="First post.")
    second = await _draft(app, text_="Second post.")
    await publish_draft(app.state.db, pub, first, retry_delays=(0, 0))
    try:
        await publish_draft(app.state.db, pub, second, retry_delays=(0, 0))
    except AlreadyPublished as e:
        assert e.post.x_post_id == "mock-1"
    else:
        raise AssertionError("second publish must be refused")
    assert pub.publish_calls == 1


async def test_concurrent_publish_only_one_reaches_the_publisher(app: Any) -> None:
    pub = MockPublisher()
    draft_id = await _draft(app)
    results = await asyncio.gather(
        publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0)),
        publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0)),
        return_exceptions=True,
    )
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds in (
        ["AlreadyPublished", "PublishedPost"],
        ["PublishInProgress", "PublishedPost"],
    ), kinds
    assert pub.publish_calls == 1


async def test_not_sent_is_retried_then_succeeds(app: Any) -> None:
    pub = MockPublisher()
    pub.fail_next(PublishNotSent("429"))
    pub.fail_next(PublishNotSent("connection refused"))
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.published and pub.publish_calls == 3 and len(pub.sent) == 1


async def test_not_sent_exhausted_fails_and_manual_retry_works(app: Any) -> None:
    pub = MockPublisher()
    for _ in range(3):
        pub.fail_next(PublishNotSent("429"))
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.failed and "not sent after retries" in (
        post.last_error or ""
    )
    assert pub.publish_calls == 3 and pub.sent == []
    retried = await retry_published(app.state.db, pub, post.id)
    assert (
        retried.id == post.id
        and retried.status == PublishStatus.published
        and retried.attempts == 2
    )


async def test_maybe_sent_after_x_created_it_is_verified_not_reposted(app: Any) -> None:
    """Crash/timeout after POST: X created the post. find_recent must find it; no second POST."""
    pub = MockPublisher()
    pub.fail_next(PublishMaybeSent("timeout"), record_as_sent=True)
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.published and post.x_post_id == "mock-1"
    assert pub.publish_calls == 1 and len(pub.sent) == 1


async def test_maybe_sent_without_a_post_fails_without_reposting(app: Any) -> None:
    pub = MockPublisher()
    pub.fail_next(PublishMaybeSent("503"))
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.failed and "not found on timeline" in (
        post.last_error or ""
    )
    assert pub.publish_calls == 1 and pub.sent == []


async def test_rejected_and_not_connected_fail_immediately(app: Any) -> None:
    pub = MockPublisher()
    pub.fail_next(PublishRejected("403 forbidden"))
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.failed and pub.publish_calls == 1
    pub.fail_next(NotConnected("reconnect"))
    post = await retry_published(app.state.db, pub, post.id)
    assert post.status == PublishStatus.failed and "reconnect" in (post.last_error or "")


async def test_stale_publishing_claim_can_be_retaken(app: Any) -> None:
    pub = MockPublisher()
    draft_id = await _draft(app)
    async with app.state.db.session_factory() as session:
        session.add(
            PublishedPost(
                local_date=D,
                draft_id=draft_id,
                final_text="x",
                status=PublishStatus.publishing,
                attempts=1,
            )
        )
        await session.commit()
        await session.execute(
            text("UPDATE published_posts SET updated_at = now() - interval '10 minutes'")
        )
        await session.commit()
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.published and post.attempts == 2


async def test_graph_publish_node_publishes_via_mock_publisher(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    r = await auth_client.post(
        f"/api/days/{D}/entries", json={"kind": "worked_on", "content": "Wired the publisher"}
    )
    assert r.status_code == 201
    r = await auth_client.post(f"/api/days/{D}/pipeline/run")
    run_id = int(r.json()["id"])
    await app.state.runner.wait(run_id, 60)
    drafts = (await auth_client.get(f"/api/days/{D}/drafts")).json()
    chosen = drafts[0]["id"]
    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": chosen}
    )
    assert r.status_code == 202
    await app.state.runner.wait(run_id, 60)
    async with app.state.db.session_factory() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.status == RunStatus.published, (
            run.status,
            run.outcome_reason,
        )
    day = (await auth_client.get(f"/api/days/{D}")).json()
    assert day["decision"] == "post" and day["published"]["status"] == "published"
    assert day["published"]["x_url"].startswith("https://x.com/mock/status/")
    assert app.state.publisher.publish_calls == 1
    hist = (await auth_client.get("/api/history", params={"from": str(D), "to": str(D)})).json()
    assert hist[0]["published"]["x_post_id"] == "mock-1" and hist[0]["decision"] == "post"


async def test_history_and_consistency_stats(app: Any, auth_client: httpx.AsyncClient) -> None:
    from xbot.services.days import local_today

    today = local_today(app.state.settings)
    pub = MockPublisher()
    for offset in (1, 2, 3, 6):
        d = today - timedelta(days=offset)
        await publish_draft(
            app.state.db, pub, await _draft(app, d, text_=f"post {offset}"), retry_delays=(0, 0)
        )
    async with app.state.db.session_factory() as session:
        day = await get_or_create_day(session, today - timedelta(days=4))
        day.decision = DayDecision.no_post
        day.no_post_reason = "nothing to share"
        await session.commit()
    stats = (await auth_client.get("/api/stats/consistency", params={"days": 30})).json()
    assert stats["posts_published"] == 4 and stats["days_decided"] == 5
    assert stats["current_post_streak"] == 3 and stats["longest_post_streak"] == 3
    assert stats["current_decision_streak"] == 4  # yesterday..4 days ago (3 posts + 1 no_post)
    hist = (await auth_client.get("/api/history")).json()
    assert [h["local_date"] for h in hist][:2] == [
        str(today - timedelta(days=1)),
        str(today - timedelta(days=2)),
    ]
    assert any(
        h["decision"] == "no_post" and h["no_post_reason"] == "nothing to share" for h in hist
    )
    r = await auth_client.get(
        "/api/history", params={"from": str(today), "to": str(today - timedelta(days=1))}
    )
    assert r.status_code == 422


async def test_retry_endpoint_and_already_published_conflict(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    pub: MockPublisher = app.state.publisher
    pub.fail_next(PublishRejected("403"))
    draft_id = await _draft(app)
    post = await publish_draft(app.state.db, pub, draft_id, retry_delays=(0, 0))
    assert post.status == PublishStatus.failed
    r = await auth_client.post(f"/api/published/{post.id}/retry")
    assert r.status_code == 200 and r.json()["status"] == "published"
    r = await auth_client.post(f"/api/published/{post.id}/retry")
    assert r.status_code == 409
    assert (await auth_client.post("/api/published/999/retry")).status_code == 400
