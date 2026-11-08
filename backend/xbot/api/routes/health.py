"""Liveness and configuration summary. Unauthenticated, but reveals no secrets."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from xbot import __version__
from xbot.api.deps import DbDep, SettingsDep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str
    env: str
    db: str
    timezone: str
    llm_provider: str
    llm_model: str
    pipeline: str
    publisher: str
    mock: bool
    llm_configured: bool
    x_app_configured: bool


@router.get("/health", response_model=Health)
async def health(request: Request, settings: SettingsDep, db: DbDep) -> Health:
    db_ok = await db.ping()
    return Health(
        status="ok" if db_ok else "degraded",
        version=__version__,
        env=settings.env,
        db="ok" if db_ok else "unreachable",
        timezone=settings.timezone,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        pipeline="ready"
        if getattr(request.app.state, "runner", None) is not None
        else "unavailable",
        publisher=settings.publisher,
        mock=settings.is_mock,
        llm_configured=bool(settings.anthropic_api_key),
        x_app_configured=bool(settings.x_client_id and settings.x_client_secret),
    )
