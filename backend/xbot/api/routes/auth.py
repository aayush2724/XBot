"""Single-user password login that issues a signed, HTTP-only session cookie."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from xbot.api.deps import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SettingsDep,
    create_session_token,
    require_auth,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class AuthStatus(BaseModel):
    authenticated: bool


@router.post("/login", response_model=AuthStatus)
async def login(body: LoginIn, response: Response, settings: SettingsDep) -> AuthStatus:
    if not secrets.compare_digest(body.password.encode(), settings.dashboard_password.encode()):
        await asyncio.sleep(0.5)  # blunt brute-force throttle for a single-user app
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="wrong password")
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(settings),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=settings.public_url.startswith("https://"),
        path="/",
    )
    return AuthStatus(authenticated=True)


@router.post("/logout", response_model=AuthStatus)
async def logout(response: Response) -> AuthStatus:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return AuthStatus(authenticated=False)


@router.get("/me", response_model=AuthStatus, dependencies=[Depends(require_auth)])
async def me() -> AuthStatus:
    return AuthStatus(authenticated=True)
