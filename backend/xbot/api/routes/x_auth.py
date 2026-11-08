"""Connect / disconnect the owner's X account (OAuth 2.0 PKCE, confidential client)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from xbot.api.deps import SessionDep, SettingsDep, require_auth
from xbot.api.schemas import XStatus
from xbot.db.models import XCredential
from xbot.publishing import x_oauth
from xbot.publishing.base import NotConnected, PublishError

router = APIRouter(prefix="/x", tags=["x"])


class ConnectOut(BaseModel):
    authorize_url: str


@router.get("/connect", response_model=ConnectOut, dependencies=[Depends(require_auth)])
async def connect(session: SessionDep, settings: SettingsDep) -> ConnectOut:
    try:
        return ConnectOut(authorize_url=await x_oauth.begin_connect(session, settings))
    except NotConnected as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/callback")
async def callback(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """X redirects the browser here. No session cookie is required because the `state` value,
    stored server-side at connect time, is the proof that this flow was started by the owner."""
    base = settings.public_url.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(
            f"{base}/?x=error&reason={error or 'missing_code'}", status_code=303
        )
    http: httpx.AsyncClient | None = getattr(request.app.state, "x_http", None)
    own_client = http is None
    http = http or httpx.AsyncClient(timeout=20.0)
    try:
        await x_oauth.complete_connect(session, settings, http, code=code, state=state)
    except PublishError as e:
        return RedirectResponse(f"{base}/?x=error&reason={str(e)[:120]}", status_code=303)
    finally:
        if own_client:
            await http.aclose()
    return RedirectResponse(f"{base}/?x=connected", status_code=303)


@router.get("/status", response_model=XStatus, dependencies=[Depends(require_auth)])
async def status(session: SessionDep, settings: SettingsDep) -> XStatus:
    creds = (
        await session.execute(select(XCredential).where(XCredential.id == 1))
    ).scalar_one_or_none()
    configured = bool(settings.x_client_id and settings.x_client_secret)
    if creds is None:
        return XStatus(app_configured=configured, connected=False)
    return XStatus(
        app_configured=configured,
        connected=not creds.needs_reconnect,
        username=creds.username,
        x_user_id=creds.x_user_id,
        expires_at=creds.expires_at,
        needs_reconnect=creds.needs_reconnect,
        scopes=creds.scopes,
    )


@router.delete("/connection", response_model=XStatus, dependencies=[Depends(require_auth)])
async def disconnect(session: SessionDep, settings: SettingsDep) -> XStatus:
    await x_oauth.disconnect(session)
    return XStatus(
        app_configured=bool(settings.x_client_id and settings.x_client_secret), connected=False
    )
