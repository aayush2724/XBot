"""M5: X OAuth PKCE flow, locked single-use refresh, and the X publisher's HTTP mapping (respx)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.conftest import make_test_settings
from xbot.db.models import OAuthState, XCredential
from xbot.publishing import x_oauth
from xbot.publishing.base import (
    NotConnected,
    PostPayload,
    PublishMaybeSent,
    PublishNotSent,
    PublishRejected,
)
from xbot.publishing.x_publisher import XPublisher

TOKEN = "https://api.x.com/2/oauth2/token"
ME = "https://api.x.com/2/users/me"
TWEETS = "https://api.x.com/2/tweets"


def _settings() -> Any:
    return make_test_settings(x_client_id="client-id", x_client_secret="client-secret")


async def _connected(app: Any, *, expires_in: int = 7200) -> None:
    box = x_oauth.TokenBox(app.state.settings)
    async with app.state.db.session_factory() as session:
        session.add(
            XCredential(
                id=1,
                x_user_id="42",
                username="aayush",
                access_token_enc=box.seal("access-1"),
                refresh_token_enc=box.seal("refresh-1"),
                expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                scopes="tweet.write",
            )
        )
        await session.commit()


async def test_connect_url_has_pkce_and_stores_state(app: Any) -> None:
    settings = _settings()
    async with app.state.db.session_factory() as session:
        url = await x_oauth.begin_connect(session, settings)
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert parsed.netloc == "x.com" and parsed.path == "/i/oauth2/authorize"
    assert q["response_type"] == ["code"] and q["client_id"] == ["client-id"]
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 40
    assert set(q["scope"][0].split()) == {
        "tweet.read",
        "tweet.write",
        "users.read",
        "offline.access",
    }
    async with app.state.db.session_factory() as session:
        row = (await session.execute(select(OAuthState))).scalar_one()
        assert row.state == q["state"][0] and len(row.code_verifier) >= 43


async def test_callback_exchanges_code_and_stores_encrypted_tokens(app: Any) -> None:
    settings = _settings()
    async with app.state.db.session_factory() as session:
        url = await x_oauth.begin_connect(session, settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    with respx.mock(assert_all_called=True) as mock:
        token_route = mock.post(TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "acc",
                    "refresh_token": "ref",
                    "expires_in": 7200,
                    "scope": "tweet.read tweet.write users.read offline.access",
                },
            )
        )
        mock.get(ME).mock(
            return_value=httpx.Response(200, json={"data": {"id": "42", "username": "aayush"}})
        )
        async with app.state.db.session_factory() as session, httpx.AsyncClient() as http:
            creds = await x_oauth.complete_connect(
                session, settings, http, code="the-code", state=state
            )
    req = token_route.calls.last.request
    body = parse_qs(req.content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["code"] == ["the-code"]
    assert body["code_verifier"][0] and body["redirect_uri"] == [settings.x_redirect_uri]
    assert req.headers["authorization"].startswith("Basic ")
    assert creds.username == "aayush" and creds.x_user_id == "42"
    assert creds.access_token_enc != "acc" and creds.refresh_token_enc not in (None, "ref")
    box = x_oauth.TokenBox(settings)
    assert box.open(creds.access_token_enc) == "acc"
    async with app.state.db.session_factory() as session:
        assert (await session.execute(select(OAuthState))).scalar_one_or_none() is None, (
            "state is single-use"
        )
        with pytest.raises(Exception, match="unknown or expired"):
            await x_oauth.complete_connect(
                session, settings, httpx.AsyncClient(), code="again", state=state
            )


async def test_refresh_only_when_near_expiry_and_saves_new_refresh_token(app: Any) -> None:
    settings = _settings()
    await _connected(app, expires_in=7200)
    async with httpx.AsyncClient() as http:
        assert (
            await x_oauth.get_access_token(app.state.db, settings, http) == "access-1"
        )  # no refresh
    async with app.state.db.session_factory() as session:
        await x_oauth.disconnect(session)
    await _connected(app, expires_in=60)  # within the 5-minute margin
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 7200},
            )
        )
        async with httpx.AsyncClient() as http:
            token = await x_oauth.get_access_token(app.state.db, settings, http)
    assert token == "access-2"
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["refresh-1"]
    box = x_oauth.TokenBox(settings)
    async with app.state.db.session_factory() as session:
        creds = await session.get(XCredential, 1)
        assert creds is not None and box.open(creds.refresh_token_enc or "") == "refresh-2"
        assert creds.expires_at > datetime.now(UTC) + timedelta(minutes=60)


async def test_concurrent_refresh_calls_the_token_endpoint_once(app: Any) -> None:
    settings = _settings()
    await _connected(app, expires_in=60)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 7200},
            )
        )
        async with httpx.AsyncClient() as http:
            tokens = await asyncio.gather(
                *(x_oauth.get_access_token(app.state.db, settings, http) for _ in range(3))
            )
    assert tokens == ["access-2", "access-2", "access-2"]
    assert route.call_count == 1, (
        "the row lock must serialise refreshes; refresh tokens are single-use"
    )


async def test_invalid_grant_marks_reconnect_and_5xx_is_transient(app: Any) -> None:
    settings = _settings()
    await _connected(app, expires_in=60)
    with respx.mock() as mock:
        mock.post(TOKEN).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as http:
            with pytest.raises(x_oauth.XAuthTransient):
                await x_oauth.get_access_token(app.state.db, settings, http)
    async with app.state.db.session_factory() as session:
        creds = await session.get(XCredential, 1)
        assert creds is not None and creds.needs_reconnect is False
    with respx.mock() as mock:
        mock.post(TOKEN).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        async with httpx.AsyncClient() as http:
            with pytest.raises(NotConnected):
                await x_oauth.get_access_token(app.state.db, settings, http)
    async with app.state.db.session_factory() as session:
        creds = await session.get(XCredential, 1)
        assert creds is not None and creds.needs_reconnect is True
    async with httpx.AsyncClient() as http:
        with pytest.raises(NotConnected, match="re-authorisation"):
            await x_oauth.get_access_token(app.state.db, settings, http)


async def test_x_publisher_maps_http_outcomes(app: Any) -> None:
    settings = _settings()
    await _connected(app)
    payload = PostPayload(segments=["hello world"])
    with respx.mock() as mock:
        async with httpx.AsyncClient() as http:
            pub = XPublisher(app.state.db, settings, http)
            mock.post(TWEETS).mock(
                return_value=httpx.Response(
                    201, json={"data": {"id": "777", "text": "hello world"}}
                )
            )
            res = await pub.publish(payload)
            assert res.external_id == "777" and res.url == "https://x.com/aayush/status/777"
            assert mock.calls.last.request.headers["authorization"] == "Bearer access-1"

            mock.post(TWEETS).mock(
                return_value=httpx.Response(429, json={"title": "Too Many Requests"})
            )
            with pytest.raises(PublishNotSent):
                await pub.publish(payload)
            mock.post(TWEETS).mock(
                return_value=httpx.Response(
                    403,
                    json={
                        "detail": "You are not allowed to create a Tweet with duplicate content."
                    },
                )
            )
            with pytest.raises(PublishMaybeSent):
                await pub.publish(payload)
            mock.post(TWEETS).mock(return_value=httpx.Response(503))
            with pytest.raises(PublishMaybeSent):
                await pub.publish(payload)
            mock.post(TWEETS).mock(
                return_value=httpx.Response(400, json={"detail": "text too long"})
            )
            with pytest.raises(PublishRejected):
                await pub.publish(payload)
            mock.post(TWEETS).mock(
                return_value=httpx.Response(401, json={"detail": "Unauthorized"})
            )
            with pytest.raises(NotConnected):
                await pub.publish(payload)
            mock.post(TWEETS).mock(side_effect=httpx.ConnectError("refused"))
            with pytest.raises(PublishNotSent):
                await pub.publish(payload)
            mock.post(TWEETS).mock(side_effect=httpx.ReadTimeout("slow"))
            with pytest.raises(PublishMaybeSent):
                await pub.publish(payload)

            since = datetime.now(UTC) - timedelta(hours=1)
            mock.get("https://api.x.com/2/users/42/tweets").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "900",
                                "text": "Hello   world",
                                "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                            },
                            {
                                "id": "800",
                                "text": "older",
                                "created_at": "2020-01-01T00:00:00.000Z",
                            },
                        ]
                    },
                )
            )
            found = await pub.find_recent("hello world", since)
            assert found is not None and found.external_id == "900"
            assert await pub.find_recent("something else", since) is None


async def test_x_routes_status_connect_and_disconnect(
    app: Any, auth_client: httpx.AsyncClient
) -> None:
    r = await auth_client.get("/api/x/status")
    assert r.status_code == 200 and r.json() == {
        "app_configured": False,
        "connected": False,
        "username": None,
        "x_user_id": None,
        "expires_at": None,
        "needs_reconnect": False,
        "scopes": None,
    }
    r = await auth_client.get("/api/x/connect")
    assert r.status_code == 400 and "X_CLIENT_ID" in r.json()["detail"]
    await _connected(app)
    r = await auth_client.get("/api/x/status")
    assert r.json()["connected"] is True and r.json()["username"] == "aayush"
    health = (await auth_client.get("/api/health")).json()
    assert health["x_connected"] is True
    r = await auth_client.delete("/api/x/connection", headers={"x-requested-with": "xbot"})
    assert r.status_code == 200 and r.json()["connected"] is False
    r = await auth_client.get(
        "/api/x/callback", params={"error": "access_denied"}, follow_redirects=False
    )
    assert r.status_code == 303 and "x=error" in r.headers["location"]
