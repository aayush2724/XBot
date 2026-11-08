"""Drafts: list a day's drafts, edit text while waiting for review, rate and give feedback."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from xbot.api.deps import SessionDep, require_auth
from xbot.api.schemas import DraftOut, DraftPatch
from xbot.services import drafts as svc

router = APIRouter(tags=["drafts"], dependencies=[Depends(require_auth)])


@router.get("/days/{local_date}/drafts", response_model=list[DraftOut])
async def list_drafts(local_date: date, session: SessionDep) -> list[DraftOut]:
    return [DraftOut.model_validate(d) for d in await svc.list_for_day(session, local_date)]


@router.patch("/drafts/{draft_id}", response_model=DraftOut)
async def patch_draft(draft_id: int, body: DraftPatch, session: SessionDep) -> DraftOut:
    if body.model_dump(exclude_none=True) == {}:
        raise HTTPException(status_code=422, detail="nothing to change")
    try:
        draft = await svc.patch_draft(
            session,
            draft_id,
            edited_text=body.edited_text,
            rating=body.rating,
            feedback=body.feedback,
        )
    except svc.DraftNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except svc.DraftNotEditable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return DraftOut.model_validate(draft)
