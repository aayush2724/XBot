"""X OAuth 2.0 Authorization Code with PKCE for a confidential client, encrypted token storage,
and refresh under a row lock (refresh tokens are single-use)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.config import Settings
from xbot.db.base import Database
from xbot.db.models import OAuthState, XCredential
from xbot.logging import get_logger
from xbot.publishing.base import NotConnected, PublishError

log = get_logger("xbot.x_oauth")

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"  # noqa: S105 (a URL, not a secret)
ME_URL = "https://api.x.com/2/users/me"
SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")
STATE_TTL = timedelta(minutes=10)
REFRESH_MARGIN = timedelta(minutes=5)


class XAuthTransient(PublishError):
    """Network or 5xx while refreshing; nothing changed, caller may back off and retry."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class TokenBox:
    def __init__(self, settings: Settings) -> None:
        self._fernet = Fernet(settings.fernet_key)

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as e:
            raise NotConnected("stored X tokens cannot be decrypted; reconnect X") from e


def _require_app(settings: Settings) -> tuple[str, str]:
    if not settings.x_client_id or not settings.x_client_secret:
        raise NotConnected("X_CLIENT_ID and X_CLIENT_SECRET are not configured")
    return settings.x_client_id, settings.x_client_secret


async def begin_connect(session: AsyncSession, settings: Settings) -> str:
    """Create a PKCE challenge + state, store the verifier, return the authorize URL."""
    client_id, _ = _require_app(settings)
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(32))
    await session.execute(
        delete(OAuthState).where(OAuthState.created_at < datetime.now(UTC) - STATE_TTL)
    )
    session.add(OAuthState(state=state, code_verifier=verifier))
    await session.commit()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": settings.x_redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def complete_connect(
    session: AsyncSession, settings: Settings, http: httpx.AsyncClient, *, code: str, state: str
) -> XCredential:
    """Exchange the code for tokens, fetch the user, store encrypted credentials."""
    client_id, client_secret = _require_app(settings)
    row = await session.get(OAuthState, state)
    if row is None or row.created_at < datetime.now(UTC) - STATE_TTL:
        raise PublishError("unknown or expired OAuth state; start the connection again")
    verifier = row.code_verifier
    await session.delete(row)
    await session.commit()

    resp = await http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.x_redirect_uri,
            "code_verifier": verifier,
            "client_id": client_id,
        },
        auth=(client_id, client_secret),
    )
    if resp.status_code != 200:
        raise PublishError(f"X token exchange failed ({resp.status_code}): {resp.text[:200]}")
    tokens = resp.json()
    me = await http.get(ME_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    if me.status_code != 200:
        raise PublishError(f"X users/me failed ({me.status_code}): {me.text[:200]}")
    user = me.json().get("data", {})

    box = TokenBox(settings)
    creds = await session.get(XCredential, 1)
    if creds is None:
        creds = XCredential(id=1)
        session.add(creds)
    creds.x_user_id = str(user.get("id", ""))
    creds.username = str(user.get("username", ""))
    creds.access_token_enc = box.seal(tokens["access_token"])
    creds.refresh_token_enc = (
        box.seal(tokens.get("refresh_token", "")) if tokens.get("refresh_token") else None
    )
    creds.expires_at = datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in", 7200)))
    creds.scopes = str(tokens.get("scope", " ".join(SCOPES)))
    creds.connected_at = datetime.now(UTC)
    creds.needs_reconnect = False
    await session.commit()
    await session.refresh(creds)
    log.info("x connected", username=creds.username)
    return creds


async def disconnect(session: AsyncSession) -> None:
    await session.execute(delete(XCredential))
    await session.commit()


async def get_access_token(db: Database, settings: Settings, http: httpx.AsyncClient) -> str:
    """Return a valid access token, refreshing under `SELECT ... FOR UPDATE` when it is within
    REFRESH_MARGIN of expiry. The new refresh token is committed before the access token is
    used, and a `needs_reconnect` flag is committed before the caller sees NotConnected."""
    box = TokenBox(settings)
    reconnect_error: NotConnected | None = None
    access: str | None = None
    async with db.session_factory() as session, session.begin():
        creds = (
            await session.execute(select(XCredential).where(XCredential.id == 1).with_for_update())
        ).scalar_one_or_none()
        if creds is None:
            raise NotConnected("no X account connected")
        if creds.needs_reconnect:
            raise NotConnected("X connection needs re-authorisation")
        now = datetime.now(UTC)
        if creds.expires_at - now > REFRESH_MARGIN:
            return box.open(creds.access_token_enc)
        if not creds.refresh_token_enc:
            creds.needs_reconnect = True
            reconnect_error = NotConnected("X access token expired and no refresh token is stored")
        else:
            client_id, client_secret = _require_app(settings)
            try:
                resp = await http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": box.open(creds.refresh_token_enc),
                        "client_id": client_id,
                    },
                    auth=(client_id, client_secret),
                )
            except httpx.TransportError as e:
                raise XAuthTransient(f"network error refreshing X token: {e}") from e
            if resp.status_code >= 500:
                raise XAuthTransient(f"X token endpoint returned {resp.status_code}")
            if resp.status_code != 200:
                body: dict[str, Any] = {}
                with contextlib.suppress(ValueError):
                    body = resp.json()
                if body.get("error") == "invalid_grant" or resp.status_code in (400, 401):
                    creds.needs_reconnect = True
                    reconnect_error = NotConnected(
                        "X refresh token rejected (invalid_grant); reconnect X"
                    )
                else:
                    raise PublishError(
                        f"X token refresh failed ({resp.status_code}): {resp.text[:200]}"
                    )
            else:
                tokens = resp.json()
                creds.access_token_enc = box.seal(tokens["access_token"])
                if tokens.get("refresh_token"):
                    creds.refresh_token_enc = box.seal(tokens["refresh_token"])
                creds.expires_at = now + timedelta(seconds=int(tokens.get("expires_in", 7200)))
                access = str(tokens["access_token"])
    # The transaction has committed here: new tokens or the reconnect flag are durable.
    if reconnect_error is not None:
        raise reconnect_error
    assert access is not None
    return access
