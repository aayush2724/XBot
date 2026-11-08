"""Pipeline runs: start, inspect, answer check-ins, retry, cancel. All execution goes through the
PipelineRunner; this module only translates HTTP to runner calls."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from xbot.api.deps import SessionDep, require_auth
from xbot.api.schemas import DraftOut, RunDetail, RunOut, StageOut
from xbot.db.models import DraftRecord, PipelineRun, PipelineStageRun, RunTrigger
from xbot.intelligence.runner import PipelineRunner, RunConflict, RunInvalidState, RunNotFound

router = APIRouter(tags=["pipeline"], dependencies=[Depends(require_auth)])

RunnerErrors = (RunConflict, RunInvalidState, RunNotFound)


def _runner(request: Request) -> PipelineRunner:
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="pipeline runner not ready")
    return runner  # type: ignore[no-any-return]


def _translate(e: Exception) -> HTTPException:
    if isinstance(e, RunNotFound):
        return HTTPException(status_code=404, detail=str(e))
    return HTTPException(status_code=409, detail=str(e))


@router.post(
    "/days/{local_date}/pipeline/run",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(local_date: date, request: Request, supersede: bool = False) -> RunOut:
    try:
        run = await _runner(request).start(local_date, RunTrigger.manual, supersede=supersede)
    except RunnerErrors as e:
        raise _translate(e) from e
    return RunOut.model_validate(run)


@router.get("/pipeline/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: int, request: Request, session: SessionDep) -> RunDetail:
    try:
        run, next_nodes, waiting = await _runner(request).run_view(run_id)
    except RunnerErrors as e:
        raise _translate(e) from e
    stmt = (
        select(PipelineStageRun)
        .where(PipelineStageRun.run_id == run_id)
        .order_by(PipelineStageRun.stage, PipelineStageRun.attempt)
    )
    stages = (await session.execute(stmt)).scalars().all()
    drafts = (
        (
            await session.execute(
                select(DraftRecord)
                .where(DraftRecord.run_id == run_id)
                .order_by(DraftRecord.attempt, DraftRecord.label)
            )
        )
        .scalars()
        .all()
    )
    return RunDetail(
        **RunOut.model_validate(run).model_dump(),
        next_nodes=next_nodes,
        waiting_for=waiting,
        stages=[StageOut.model_validate(s) for s in stages],
        drafts=[DraftOut.model_validate(d) for d in drafts],
    )


@router.get("/days/{local_date}/runs", response_model=list[RunOut])
async def list_runs(local_date: date, session: SessionDep) -> list[RunOut]:
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.local_date == local_date)
        .order_by(PipelineRun.id.desc())
    )
    return [RunOut.model_validate(r) for r in (await session.execute(stmt)).scalars()]


@router.post(
    "/pipeline/runs/{run_id}/checkins/submit",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_checkins(run_id: int, request: Request) -> RunOut:
    try:
        run = await _runner(request).resume_checkins(run_id, "submit")
    except RunnerErrors as e:
        raise _translate(e) from e
    return RunOut.model_validate(run)


@router.post(
    "/pipeline/runs/{run_id}/checkins/skip",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def skip_checkins(run_id: int, request: Request) -> RunOut:
    try:
        run = await _runner(request).resume_checkins(run_id, "skip")
    except RunnerErrors as e:
        raise _translate(e) from e
    return RunOut.model_validate(run)


@router.post(
    "/pipeline/runs/{run_id}/retry", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED
)
async def retry_run(run_id: int, request: Request) -> RunOut:
    try:
        run = await _runner(request).retry(run_id)
    except RunnerErrors as e:
        raise _translate(e) from e
    return RunOut.model_validate(run)


@router.post("/pipeline/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: int, request: Request) -> RunOut:
    try:
        run = await _runner(request).cancel(run_id)
    except RunnerErrors as e:
        raise _translate(e) from e
    return RunOut.model_validate(run)
