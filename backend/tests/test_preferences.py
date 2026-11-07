from __future__ import annotations

import httpx

from xbot.db.models import Setting
from xbot.preferences import AUTONOMOUS_CONFIRMATION


async def test_defaults(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["publishing_mode"] == "approval"
    assert body["max_post_chars"] == 280
    assert body["allow_links"] is False
    assert body["evening_time"] == "20:00"


async def test_partial_update_persists(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.put(
        "/api/settings",
        json={
            "preferences": {
                "max_post_chars": 4000,
                "evening_time": "21:30",
                "extra_repos": ["owner/repo"],
                "github_author_emails": ["Me@Example.com"],
                "phrases_to_avoid": [" game changer ", ""],
            }
        },
    )
    assert r.status_code == 200, r.text
    body = (await auth_client.get("/api/settings")).json()
    assert body["max_post_chars"] == 4000
    assert body["evening_time"] == "21:30"
    assert body["extra_repos"] == ["owner/repo"]
    assert body["github_author_emails"] == ["me@example.com"]
    assert body["phrases_to_avoid"] == ["game changer"]
    assert body["publishing_mode"] == "approval"  # untouched


async def test_validation_errors(auth_client: httpx.AsyncClient) -> None:
    bad = [
        {"evening_time": "25:99"},
        {"extra_repos": ["not-a-repo"]},
        {"max_post_chars": 10},
        {"unknown_field": 1},
        {"github_author_emails": ["nope"]},
    ]
    for prefs in bad:
        r = await auth_client.put("/api/settings", json={"preferences": prefs})
        assert r.status_code == 422, prefs


async def test_autonomous_mode_requires_confirmation(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.put(
        "/api/settings", json={"preferences": {"publishing_mode": "autonomous"}}
    )
    assert r.status_code == 400
    r = await auth_client.put(
        "/api/settings",
        json={"preferences": {"publishing_mode": "autonomous"}, "confirmation": "yes please"},
    )
    assert r.status_code == 400
    assert (await auth_client.get("/api/settings")).json()["publishing_mode"] == "approval"

    r = await auth_client.put(
        "/api/settings",
        json={
            "preferences": {"publishing_mode": "autonomous"},
            "confirmation": AUTONOMOUS_CONFIRMATION,
        },
    )
    assert r.status_code == 200 and r.json()["publishing_mode"] == "autonomous"

    r = await auth_client.put(
        "/api/settings", json={"preferences": {"publishing_mode": "approval"}}
    )
    assert r.status_code == 200 and r.json()["publishing_mode"] == "approval"


async def test_invalid_stored_value_is_ignored(app, auth_client: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    async with app.state.db.session_factory() as session:
        session.add(Setting(key="evening_time", value="25:99"))
        session.add(Setting(key="max_post_chars", value=500))
        await session.commit()
    body = (await auth_client.get("/api/settings")).json()
    assert body["evening_time"] == "20:00"
    assert body["max_post_chars"] == 500
