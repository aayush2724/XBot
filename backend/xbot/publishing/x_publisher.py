"""X API v2 publisher. Maps HTTP outcomes onto the failure classes the service understands."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from xbot.config import Settings
from xbot.db.base import Database
from xbot.db.models import XCredential
from xbot.logging import get_logger
from xbot.publishing.base import (
    NotConnected,
    PostPayload,
    PublishMaybeSent,
    PublishNotSent,
    PublishRejected,
    PublishResult,
)
from xbot.publishing.x_oauth import get_access_token

log = get_logger("xbot.x_publisher")

API = "https://api.x.com/2"


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


class XPublisher:
    name = "x"

    def __init__(
        self, db: Database, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> None:
        self.db = db
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _identity(self) -> tuple[str, str]:
        async with self.db.session_factory() as session:
            creds = (
                await session.execute(select(XCredential).where(XCredential.id == 1))
            ).scalar_one_or_none()
        if creds is None:
            raise NotConnected("no X account connected")
        return creds.x_user_id, creds.username

    async def publish(self, payload: PostPayload) -> PublishResult:
        token = await get_access_token(self.db, self.settings, self._http)
        _, username = await self._identity()
        try:
            resp = await self._http.post(
                f"{API}/tweets",
                json={"text": payload.text},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.ConnectError as e:
            raise PublishNotSent(f"could not connect to X: {e}") from e
        except httpx.TimeoutException as e:
            raise PublishMaybeSent(f"timeout talking to X: {e}") from e
        except httpx.TransportError as e:
            raise PublishMaybeSent(f"transport error talking to X: {e}") from e

        if resp.status_code == 201:
            post_id = str(resp.json()["data"]["id"])
            return PublishResult(
                external_id=post_id, url=f"https://x.com/{username}/status/{post_id}"
            )
        detail = _detail(resp)
        if resp.status_code == 429:
            raise PublishNotSent(f"X rate limit (429): {detail}")
        if resp.status_code == 403 and "duplicate" in detail.lower():
            raise PublishMaybeSent(f"X reports duplicate content: {detail}")
        if resp.status_code in (401,):
            raise NotConnected(f"X rejected the token (401): {detail}")
        if 400 <= resp.status_code < 500:
            raise PublishRejected(f"X refused the post ({resp.status_code}): {detail}")
        raise PublishMaybeSent(f"X server error ({resp.status_code}): {detail}")

    async def find_recent(self, text: str, since: datetime) -> PublishResult | None:
        token = await get_access_token(self.db, self.settings, self._http)
        user_id, username = await self._identity()
        try:
            resp = await self._http.get(
                f"{API}/users/{user_id}/tweets",
                params={"max_results": 5, "tweet.fields": "created_at"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TransportError as e:
            log.warning("find_recent failed", error=str(e))
            return None
        if resp.status_code != 200:
            log.warning("find_recent non-200", status=resp.status_code)
            return None
        target = _norm(text)
        for item in resp.json().get("data", []) or []:
            created = item.get("created_at")
            try:
                when = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when >= since and _norm(str(item.get("text", ""))) == target:
                post_id = str(item["id"])
                return PublishResult(
                    external_id=post_id, url=f"https://x.com/{username}/status/{post_id}"
                )
        return None


def _detail(resp: httpx.Response) -> str:
    try:
        body: Any = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("title") or body.get("error") or body)[:200]
    return str(body)[:200]
