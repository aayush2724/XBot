from __future__ import annotations

import httpx
from sqlalchemy import select

from xbot.db.models import Checkin
from xbot.services import days as svc

D = "2026-11-08"


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/days/today")).status_code == 401


async def test_today_view_is_empty_until_written(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.get("/api/days/today")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is False
    assert body["decision"] == "pending"
    assert body["quests"] == [] and body["entries"] == [] and body["checkins"] == []


async def test_quests_limit_status_and_preservation(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.put(f"/api/days/{D}/quests", json={"quests": ["a", "b", "c", "d"]})
    assert r.status_code == 422

    r = await auth_client.put(f"/api/days/{D}/quests", json={"quests": [" Ship M2 ", "Read PKCE"]})
    assert r.status_code == 200, r.text
    quests = r.json()["quests"]
    assert [q["position"] for q in quests] == [1, 2]
    assert [q["text"] for q in quests] == ["Ship M2", "Read PKCE"]
    assert all(q["status"] == "planned" and q["completed_at"] is None for q in quests)

    r = await auth_client.patch(f"/api/quests/{quests[0]['id']}", json={"status": "done"})
    assert r.status_code == 200
    assert r.json()["status"] == "done" and r.json()["completed_at"] is not None

    # Re-submitting the list keeps the done status for the unchanged quest text.
    r = await auth_client.put(
        f"/api/days/{D}/quests", json={"quests": ["Read PKCE", "ship m2", "DSA"]}
    )
    assert r.status_code == 200
    by_text = {q["text"]: q for q in r.json()["quests"]}
    assert by_text["ship m2"]["status"] == "done"
    assert by_text["Read PKCE"]["status"] == "planned"
    assert by_text["DSA"]["position"] == 3

    r = await auth_client.patch("/api/quests/999999", json={"status": "done"})
    assert r.status_code == 404


async def test_quest_text_too_long_is_422(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.put(f"/api/days/{D}/quests", json={"quests": ["x" * 201]})
    assert r.status_code == 422


async def test_journal_entry_lifecycle(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.post(f"/api/days/{D}/entries", json={"kind": "nope", "content": "x"})
    assert r.status_code == 422
    r = await auth_client.post(f"/api/days/{D}/entries", json={"kind": "learned", "content": "  "})
    assert r.status_code == 422

    r = await auth_client.post(
        f"/api/days/{D}/entries",
        json={
            "kind": "learned",
            "content": "  PKCE verifier is per-authorisation  ",
            "source": "cli",
        },
    )
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["content"] == "PKCE verifier is per-authorisation"
    assert entry["source"] == "cli"

    day = (await auth_client.get(f"/api/days/{D}")).json()
    assert day["exists"] is True
    assert [e["id"] for e in day["entries"]] == [entry["id"]]

    assert (await auth_client.delete(f"/api/entries/{entry['id']}")).status_code == 204
    assert (await auth_client.delete(f"/api/entries/{entry['id']}")).status_code == 404
    assert (await auth_client.get(f"/api/days/{D}")).json()["entries"] == []


async def test_checkins_are_listed_and_answered(app, auth_client: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    async with app.state.db.session_factory() as session:
        rows = await svc.create_checkins(
            session, date.fromisoformat(D), ["What did you actually finish?", "  "]
        )
    assert len(rows) == 1

    r = await auth_client.get(f"/api/days/{D}/checkins")
    assert r.status_code == 200
    [c] = r.json()
    assert c["answer"] is None and c["answered_at"] is None

    r = await auth_client.post(f"/api/checkins/{c['id']}/answer", json={"answer": "The sync job."})
    assert r.status_code == 200
    assert r.json()["answer"] == "The sync job." and r.json()["answered_at"] is not None

    async with app.state.db.session_factory() as session:
        stored = (await session.execute(select(Checkin))).scalar_one()
        assert stored.answer == "The sync job."

    assert (
        await auth_client.post("/api/checkins/4242/answer", json={"answer": "x"})
    ).status_code == 404


async def test_no_post_decision(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.post(f"/api/days/{D}/no-post", json={"reason": "Nothing worth sharing"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "no_post"
    assert body["no_post_reason"] == "Nothing worth sharing"
    assert body["decided_at"] is not None
