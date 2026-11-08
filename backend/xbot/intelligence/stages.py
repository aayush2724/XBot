"""One structured model call for a pipeline stage, persisted as pipeline_stage_runs rows.

Rows are append-only: a truncation retry at lower effort is a second attempt and a second row.
No LangGraph RetryPolicy wraps these calls; the SDK retries transient errors itself, so there is
exactly one retry layer."""

from __future__ import annotations

import time
from typing import Any

from xbot.db.base import Database
from xbot.db.models import PipelineRun, PipelineStageRun
from xbot.llm.base import Effort, LLMProvider, LLMResult, LLMTruncated, T
from xbot.logging import get_logger

log = get_logger("xbot.stages")

EFFORT_LADDER: tuple[Effort, ...] = ("high", "medium")


async def call_stage(
    *,
    db: Database,
    llm: LLMProvider,
    run_id: int,
    stage: int,
    schema: type[T],
    system_shared: str,
    user: str,
) -> LLMResult[T]:
    """Call the model for `stage`, persisting every attempt. Raises the provider error if the
    last attempt fails; the runner turns that into a failed run with a retryable checkpoint."""
    from xbot.intelligence.prompting import stage_system

    system_stage = stage_system(stage)
    attempts = await _next_attempt(db, run_id, stage)
    last_error: Exception | None = None
    for i, effort in enumerate(EFFORT_LADDER):
        attempt = attempts + i
        started = time.monotonic()
        row = PipelineStageRun(
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            status="running",
            input={"user": user, "effort": effort, "stage_prompt": f"stage{stage}.md"},
            model=llm.model,
        )
        try:
            result = await llm.complete_structured(
                system_shared=system_shared,
                system_stage=system_stage,
                user=user,
                schema=schema,
                effort=effort,
                stage=f"stage{stage}",
            )
        except LLMTruncated as e:
            last_error = e
            await _save(db, row, started, status="failed", error=f"{type(e).__name__}: {e}")
            log.warning("stage truncated, retrying at lower effort", stage=stage, attempt=attempt)
            continue
        except Exception as e:
            await _save(db, row, started, status="failed", error=f"{type(e).__name__}: {e}")
            raise
        row.output = {"raw": result.parsed.model_dump(mode="json")}
        row.model = result.model
        row.input_tokens = result.usage.input_tokens
        row.output_tokens = result.usage.output_tokens
        row.cache_read_tokens = result.usage.cache_read_tokens
        row.cache_write_tokens = result.usage.cache_write_tokens
        row.cost_usd = result.cost_usd
        row.request_id = result.request_id
        await _save(db, row, started, status="succeeded")
        await _add_totals(db, run_id, result)
        return result
    assert last_error is not None
    raise last_error


async def annotate_stage(db: Database, run_id: int, stage: int, extra: dict[str, Any]) -> None:
    """Attach guard results to the latest succeeded row of a stage."""
    from sqlalchemy import select

    async with db.session_factory() as session:
        stmt = (
            select(PipelineStageRun)
            .where(
                PipelineStageRun.run_id == run_id,
                PipelineStageRun.stage == stage,
                PipelineStageRun.status == "succeeded",
            )
            .order_by(PipelineStageRun.attempt.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            row.output = {**(row.output or {}), **extra}
            await session.commit()


async def _next_attempt(db: Database, run_id: int, stage: int) -> int:
    from sqlalchemy import func, select

    async with db.session_factory() as session:
        stmt = select(func.coalesce(func.max(PipelineStageRun.attempt), 0)).where(
            PipelineStageRun.run_id == run_id, PipelineStageRun.stage == stage
        )
        return int((await session.execute(stmt)).scalar_one()) + 1


async def _save(
    db: Database, row: PipelineStageRun, started: float, *, status: str, error: str | None = None
) -> None:
    row.status = status
    row.error = error
    row.duration_ms = int((time.monotonic() - started) * 1000)
    async with db.session_factory() as session:
        session.add(row)
        await session.commit()


async def _add_totals(db: Database, run_id: int, result: LLMResult[Any]) -> None:
    async with db.session_factory() as session:
        run = await session.get(PipelineRun, run_id)
        if run is None:
            return
        run.total_input_tokens += result.usage.input_tokens
        run.total_output_tokens += result.usage.output_tokens
        run.total_cache_read_tokens += result.usage.cache_read_tokens
        run.total_cost_usd = round(run.total_cost_usd + result.cost_usd, 6)
        await session.commit()
