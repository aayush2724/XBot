"""GitHub sync trigger and per-repository settings."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from xbot.api.deps import SessionDep, SettingsDep, require_auth
from xbot.db.models import GithubCommit, RepoSetting
from xbot.ingestion.github import GitHubClient, SyncResult, sync_github
from xbot.preferences import load_preferences

router = APIRouter(tags=["github"], dependencies=[Depends(require_auth)])


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    repo_full_name: str
    include_diffs: bool
    excluded: bool


class RepoPatch(BaseModel):
    include_diffs: bool | None = None
    excluded: bool | None = None


@router.post("/days/{local_date}/github/sync", response_model=SyncResult)
async def sync(local_date: date, session: SessionDep, settings: SettingsDep) -> SyncResult:
    prefs = await load_preferences(session)
    if not prefs.github_username:
        raise HTTPException(status_code=400, detail="set github_username in settings first")
    async with GitHubClient(settings.github_token) as client:
        return await sync_github(session, client, prefs, local_date, settings.tz)


@router.get("/github/repos", response_model=list[RepoOut])
async def repos(session: SessionDep) -> list[RepoOut]:
    rows = {r.repo_full_name: r for r in (await session.execute(select(RepoSetting))).scalars()}
    seen = (await session.execute(select(GithubCommit.repo_full_name).distinct())).scalars()
    out = [RepoOut.model_validate(r) for r in rows.values()]
    out.extend(
        RepoOut(repo_full_name=name, include_diffs=False, excluded=False)
        for name in seen
        if name not in rows
    )
    return sorted(out, key=lambda r: r.repo_full_name.lower())


@router.patch("/github/repos/{owner}/{repo}", response_model=RepoOut)
async def patch_repo(owner: str, repo: str, body: RepoPatch, session: SessionDep) -> RepoOut:
    full_name = f"{owner}/{repo}"
    changes = body.model_dump(exclude_none=True)
    stmt = insert(RepoSetting).values(repo_full_name=full_name, **changes)
    stmt = stmt.on_conflict_do_update(
        index_elements=[RepoSetting.repo_full_name],
        set_=changes or {"repo_full_name": full_name},
    )
    await session.execute(stmt)
    await session.commit()
    row = await session.get(RepoSetting, full_name)
    assert row is not None
    return RepoOut.model_validate(row)
