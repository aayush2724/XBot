from __future__ import annotations

import httpx
import pytest

from tests.conftest import make_test_settings
from xbot.main import create_app


async def test_health_reports_db_and_mock_flags(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "ok"
    assert body["status"] == "ok"
    assert body["mock"] is True
    assert body["llm_provider"] == "mock"
    assert body["timezone"] == "Asia/Kolkata"
    assert "secret" not in r.text.lower()


async def test_login_wrong_password_is_401(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert "xbot_session" not in r.cookies


async def test_login_sets_httponly_cookie_and_me_works(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "correct-horse"})
    assert r.status_code == 200
    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie
    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json() == {"authenticated": True}


async def test_me_without_cookie_is_401(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_tampered_cookie_is_rejected(client: httpx.AsyncClient) -> None:
    client.cookies.set("xbot_session", "ok.forged.signature")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_mutating_request_without_csrf_header_is_403(client: httpx.AsyncClient) -> None:
    await client.post("/api/auth/login", json={"password": "correct-horse"})
    r = await client.post("/api/auth/logout")
    assert r.status_code == 403
    r = await client.post("/api/auth/logout", headers={"x-requested-with": "xbot"})
    assert r.status_code == 200


def test_app_refuses_example_secret_outside_tests() -> None:
    bad = make_test_settings(env="dev", secret_key="change-me-generate-with-secrets-token-urlsafe")
    with pytest.raises(RuntimeError, match="XBOT_SECRET_KEY"):
        create_app(bad)


def test_app_refuses_mock_providers_in_prod() -> None:
    bad = make_test_settings(
        env="prod", secret_key="x" * 40, dashboard_password="strong-enough", llm_provider="mock"
    )
    with pytest.raises(RuntimeError, match="mock"):
        create_app(bad)
