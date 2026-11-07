"""FastAPI dependencies: settings, database sessions, session-cookie auth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.config import Settings
from xbot.db.base import Database

SESSION_COOKIE = "xbot_session"
SESSION_MAX_AGE = 30 * 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "xbot"


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[Database, Depends(get_db)]


async def get_session(db: DbDep) -> AsyncIterator[AsyncSession]:
    async with db.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _signer(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.secret_key, salt="xbot-session")


def create_session_token(settings: Settings) -> str:
    return _signer(settings).sign(b"ok").decode()


def verify_session_token(settings: Settings, token: str | None) -> bool:
    if not token:
        return False
    try:
        _signer(settings).unsign(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return False
    return True


async def require_auth(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not verify_session_token(settings, request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
