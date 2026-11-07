"""User preferences (voice profile, publishing mode, thresholds)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from xbot.api.deps import SessionDep, require_auth
from xbot.preferences import (
    ConfirmationRequired,
    Preferences,
    PreferencesUpdate,
    load_preferences,
    save_preferences,
)

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_auth)])


class SettingsIn(BaseModel):
    preferences: PreferencesUpdate
    confirmation: str | None = None


@router.get("", response_model=Preferences)
async def get_settings(session: SessionDep) -> Preferences:
    return await load_preferences(session)


@router.put("", response_model=Preferences)
async def put_settings(body: SettingsIn, session: SessionDep) -> Preferences:
    try:
        return await save_preferences(session, body.preferences, confirmation=body.confirmation)
    except ConfirmationRequired as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValidationError as e:
        raise HTTPException(
            status_code=422, detail=e.errors(include_url=False, include_context=False)
        ) from e
