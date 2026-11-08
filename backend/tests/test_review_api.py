"""M4b: drafts endpoints, review endpoint with compare-and-set, CLI review commands."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from xbot.cli import run as cli_run
from xbot.db.models import DraftRecord, DraftStatus, PipelineRun

D = "2026-11-08"


async def _wait(app: Any, run_id: int) -> str:
    await app.state.runner.wait(run_id, 60)
    async with app.state.db.session_factory() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return str(run.status.value)


async def _to_review(app: Any, client: httpx.AsyncClient) -> tuple[int, list[dict[str, Any]]]:
    r = await client.post(
        f"/api/days/{D}/entries", json={"kind": "worked_on", "content": "Built the review endpoint"}
    )
    assert r.status_code == 201
    r = await client.post(f"/api/days/{D}/pipeline/run")
    assert r.status_code == 202
    run_id = int(r.json()["id"])
    assert await _wait(app, run_id) == "waiting_review"
    drafts = (await client.get(f"/api/days/{D}/drafts")).json()
    return run_id, drafts


async def test_list_edit_and_rate_drafts(app: Any, auth_client: httpx.AsyncClient) -> None:
    run_id, drafts = await _to_review(app, auth_client)
    assert len(drafts) == 2 and all(d["run_id"] == run_id for d in drafts)
    target = drafts[0]

    r = await auth_client.patch(f"/api/drafts/{target['id']}", json={})
    assert r.status_code == 422
    r = await auth_client.patch(f"/api/drafts/{target['id']}", json={"rating": 9})
    assert r.status_code == 422
    r = await auth_client.patch(
        f"/api/drafts/{target['id']}",
        json={
            "edited_text": "  Shorter, in my own words.  ",
            "rating": 4,
            "feedback": "good angle",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["edited_text"] == "Shorter, in my own words."
        and body["rating"] == 4
        and body["feedback"] == "good angle"
    )
    assert body["text"].startswith("[MOCK]")  # the original is kept

    # Reverting to the original text clears the edit instead of storing a duplicate.
    r = await auth_client.patch(f"/api/drafts/{target['id']}", json={"edited_text": target["text"]})
    assert r.status_code == 200 and r.json()["edited_text"] is None
    assert (await auth_client.patch("/api/drafts/999999", json={"rating": 3})).status_code == 404


async def test_approve_resumes_once_and_second_approve_conflicts(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    run_id, drafts = await _to_review(app, auth_client)
    chosen = drafts[1]["id"]
    r1, r2 = await asyncio.gather(
        auth_client.post(
            f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": chosen}
        ),
        auth_client.post(
            f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": chosen}
        ),
    )
    assert sorted([r1.status_code, r2.status_code]) == [202, 409]
    assert await _wait(app, run_id) == "published"
    async with app.state.db.session_factory() as session:
        row = await session.get(DraftRecord, chosen)
        assert row is not None and row.status == DraftStatus.published
    assert app.state.publisher.publish_calls == 1
    # Editing after the run left waiting_review is refused; rating still works.
    r = await auth_client.patch(f"/api/drafts/{chosen}", json={"edited_text": "too late"})
    assert r.status_code == 409
    r = await auth_client.patch(f"/api/drafts/{chosen}", json={"rating": 5})
    assert r.status_code == 200
    r = await auth_client.post(f"/api/pipeline/runs/{run_id}/review", json={"action": "reject"})
    assert r.status_code == 409


async def test_approve_validates_draft_id_and_edited_text(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    run_id, drafts = await _to_review(app, auth_client)
    r = await auth_client.post(f"/api/pipeline/runs/{run_id}/review", json={"action": "approve"})
    assert r.status_code == 422
    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": 999999}
    )
    assert r.status_code == 422 and "accepted drafts" in r.json()["detail"]

    chosen = drafts[0]["id"]
    r = await auth_client.patch(f"/api/drafts/{chosen}", json={"edited_text": "x" * 300})
    assert r.status_code == 200
    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": chosen}
    )
    assert r.status_code == 422 and "too long" in r.json()["detail"]
    r = await auth_client.patch(
        f"/api/drafts/{chosen}", json={"edited_text": "Read this https://example.com"}
    )
    assert r.status_code == 200
    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": chosen}
    )
    assert r.status_code == 422 and "link" in r.json()["detail"]
    # The run is still waiting; a valid approval now succeeds.
    r = await auth_client.patch(
        f"/api/drafts/{chosen}", json={"edited_text": "Fixed the review flow today. Felt good."}
    )
    assert r.status_code == 200
    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review", json={"action": "approve", "draft_id": chosen}
    )
    assert r.status_code == 202
    assert await _wait(app, run_id) == "published"
    assert app.state.publisher.sent[-1][1] == "Fixed the review flow today. Felt good."


async def test_regenerate_with_feedback_then_reject(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    llm = app.state.llm
    run_id, drafts = await _to_review(app, auth_client)
    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review",
        json={"action": "regenerate", "feedback": "drier, no exclamation"},
    )
    assert r.status_code == 202
    assert await _wait(app, run_id) == "waiting_review"
    assert (
        len(llm.calls_for("stage4")) == 2
        and "drier, no exclamation" in llm.calls_for("stage4")[1]["user"]
    )
    all_drafts = (await auth_client.get(f"/api/days/{D}/drafts")).json()
    by_attempt: dict[int, list[str]] = {}
    for d in all_drafts:
        by_attempt.setdefault(d["attempt"], []).append(d["status"])
    assert by_attempt == {1: ["discarded", "discarded"], 2: ["accepted", "accepted"]}
    detail = (await auth_client.get(f"/api/pipeline/runs/{run_id}")).json()
    assert detail["waiting_for"]["attempt"] == 2

    r = await auth_client.post(
        f"/api/pipeline/runs/{run_id}/review", json={"action": "reject", "reason": "not today"}
    )
    assert r.status_code == 202
    assert await _wait(app, run_id) == "no_post"
    detail = (await auth_client.get(f"/api/pipeline/runs/{run_id}")).json()
    assert "not today" in detail["outcome_reason"]


async def test_review_on_run_not_waiting_is_409(app: Any, auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.post(f"/api/days/{D}/pipeline/run")  # empty day → waiting_checkin
    run_id = int(r.json()["id"])
    assert await _wait(app, run_id) == "waiting_checkin"
    r = await auth_client.post(f"/api/pipeline/runs/{run_id}/review", json={"action": "reject"})
    assert r.status_code == 409 and "waiting_review" in r.json()["detail"]


async def test_cli_drafts_rate_edit_and_review(
    app: Any, auth_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id, drafts = await _to_review(app, auth_client)
    assert await cli_run(["drafts"], auth_client) == 0
    out = capsys.readouterr().out
    assert f"draft {drafts[0]['id']}" in out and "accepted" in out

    assert await cli_run(["rate", str(drafts[0]["id"]), "2", "too generic"], auth_client) == 0
    assert (
        await cli_run(
            ["edit", str(drafts[1]["id"]), "My own words about the review endpoint."], auth_client
        )
        == 0
    )
    capsys.readouterr()
    assert await cli_run(["review", "approve"], auth_client) == 2  # needs a draft id
    assert "needs a draft id" in capsys.readouterr().err
    assert await cli_run(["review", "approve", str(drafts[1]["id"])], auth_client) == 0
    assert "approve accepted" in capsys.readouterr().out
    assert await _wait(app, run_id) == "published"
    async with app.state.db.session_factory() as session:
        row = await session.get(DraftRecord, drafts[1]["id"])
        assert row is not None and row.edited_text == "My own words about the review endpoint."
        rated = await session.get(DraftRecord, drafts[0]["id"])
        assert rated is not None and rated.rating == 2 and rated.feedback == "too generic"
