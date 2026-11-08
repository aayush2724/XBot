"""PipelineRunner: the only caller of the LangGraph graph.

Owns the compiled graph, the Postgres checkpointer (psycopg pool), one asyncio lock per run, and
the reconciliation between LangGraph's execution state and the app's `pipeline_runs` ledger.
Status transitions that start execution go through a compare-and-set UPDATE so two requests can
never drive one thread at the same time (design §7)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, date, datetime
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from xbot.config import Settings
from xbot.db.base import Database
from xbot.db.models import (
    LIVE_RUN_STATUSES,
    Checkin,
    DraftRecord,
    DraftStatus,
    PipelineRun,
    RunStatus,
    RunTrigger,
)
from xbot.intelligence.checks import final_text_checks
from xbot.intelligence.graph import GRAPH_VERSION, PipelineContext, build_graph
from xbot.llm.base import TRANSIENT_ERROR_MARKERS, LLMProvider
from xbot.logging import get_logger
from xbot.preferences import load_preferences

log = get_logger("xbot.runner")

RECURSION_LIMIT = 50
MAX_RECOVER_ATTEMPTS = 2
OUTCOME_TO_STATUS = {
    "no_post": RunStatus.no_post,
    "published": RunStatus.published,
    "publish_failed": RunStatus.publish_failed,
}


class RunnerError(Exception):
    pass


class RunNotFound(RunnerError):
    pass


class RunConflict(RunnerError):
    """Another live run exists, or the status changed under us (HTTP 409)."""


class RunInvalidState(RunnerError):
    """The operation does not apply to the run's current status (HTTP 409)."""


class RunValidationError(RunnerError):
    """The owner's input is not acceptable, e.g. an edited draft that breaks a rule (HTTP 422)."""


def psycopg_dsn(url: str) -> str:
    """SQLAlchemy URL → plain psycopg DSN for the LangGraph checkpointer."""
    prefix = "postgresql+psycopg://"
    if url.startswith(prefix):
        return "postgresql://" + url[len(prefix) :]
    if url.startswith("postgresql://"):
        return url
    raise ValueError("XBot requires a postgresql+psycopg:// database URL")


class PipelineRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        llm: LLMProvider,
        publisher: Any | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.llm = llm
        self.publisher = publisher
        self.checkpointer = checkpointer
        self._pool: AsyncConnectionPool[Any] | None = None
        self.graph: Any = None
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    # -- lifecycle --------------------------------------------------------------------------

    async def startup(self) -> None:
        if self.checkpointer is None:
            self._pool = AsyncConnectionPool(
                psycopg_dsn(self.settings.database_url),
                open=False,
                min_size=1,
                max_size=4,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await self._pool.open()
            saver = AsyncPostgresSaver(self._pool)
            await saver.setup()  # idempotent; creates the checkpoint* tables
            self.checkpointer = saver
        self.graph = build_graph(self.checkpointer)
        log.info("pipeline runner ready", graph_version=GRAPH_VERSION)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- operations -------------------------------------------------------------------------

    async def start(
        self, local_date: date, trigger: RunTrigger, *, supersede: bool = False
    ) -> PipelineRun:
        from xbot.services.days import get_or_create_day

        async with self.db.session_factory() as session:
            await get_or_create_day(session, local_date)
            stmt = select(PipelineRun).where(
                PipelineRun.local_date == local_date, PipelineRun.status.in_(LIVE_RUN_STATUSES)
            )
            live = (await session.execute(stmt)).scalars().all()
            if live and not supersede:
                raise RunConflict(
                    f"run {live[0].id} is {live[0].status.value} for {local_date}; "
                    "cancel it or pass supersede=true"
                )
            for old in live:
                old.status = RunStatus.superseded
                old.finished_at = datetime.now(UTC)
                task = self._tasks.get(old.id)
                if task is not None:
                    task.cancel()
            # Earlier runs' unreviewed drafts for this day are no longer candidates.
            await session.execute(
                update(DraftRecord)
                .where(
                    DraftRecord.local_date == local_date,
                    DraftRecord.status.in_([DraftStatus.candidate, DraftStatus.accepted]),
                )
                .values(status=DraftStatus.discarded)
            )
            run = PipelineRun(
                local_date=local_date,
                thread_id=f"run:{uuid.uuid4().hex[:12]}",
                graph_version=GRAPH_VERSION,
                trigger=trigger,
                status=RunStatus.running,
            )
            session.add(run)
            try:
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                raise RunConflict(f"a live run already exists for {local_date}") from e
            await session.refresh(run)
        self._launch(
            run.id,
            run.thread_id,
            {"run_id": run.id, "local_date": local_date.isoformat(), "checkin_round": 0},
        )
        return run

    async def resume_checkins(self, run_id: int, action: str) -> PipelineRun:
        if action not in ("submit", "skip"):
            raise RunInvalidState("action must be 'submit' or 'skip'")
        run = await self._get(run_id)
        if run.status != RunStatus.waiting_checkin:
            raise RunInvalidState(f"run {run_id} is {run.status.value}, not waiting_checkin")
        if action == "submit":
            async with self.db.session_factory() as session:
                stmt = select(func.count()).where(
                    Checkin.run_id == run_id, Checkin.answer.is_(None)
                )
                unanswered = int((await session.execute(stmt)).scalar_one())
            if unanswered:
                raise RunInvalidState(
                    f"{unanswered} check-in question(s) still unanswered; answer or skip"
                )
        await self._expect_interrupt(run.thread_id, "checkin")
        if not await self._cas(run_id, {RunStatus.waiting_checkin}, RunStatus.running):
            raise RunConflict(f"run {run_id} was already resumed")
        self._launch(run_id, run.thread_id, Command(resume={"action": action}))
        return await self._get(run_id)

    async def resume_review(self, run_id: int, decision: dict[str, Any]) -> PipelineRun:
        """Approve / regenerate / reject. Validates the decision against the pending interrupt and
        the draft, then resumes under a compare-and-set so only one caller wins."""
        action = decision.get("action")
        if action not in ("approve", "regenerate", "reject"):
            raise RunValidationError("action must be approve, regenerate or reject")
        run = await self._get(run_id)
        if run.status != RunStatus.waiting_review:
            raise RunInvalidState(f"run {run_id} is {run.status.value}, not waiting_review")
        pending = await self._pending_interrupt(run.thread_id, "review")
        if action == "approve":
            draft_id = decision.get("draft_id")
            allowed = pending.get("draft_ids") or []
            if not isinstance(draft_id, int) or draft_id not in allowed:
                raise RunValidationError(f"draft_id must be one of the accepted drafts {allowed}")
            async with self.db.session_factory() as session:
                draft = await session.get(DraftRecord, draft_id)
                prefs = await load_preferences(session)
            if draft is None:
                raise RunValidationError(f"draft {draft_id} not found")
            final_text = (draft.edited_text or draft.text).strip()
            issues = final_text_checks(
                text=final_text,
                max_chars=prefs.max_post_chars,
                allow_links=prefs.allow_links,
                recent_texts=[],
            )
            if issues:
                raise RunValidationError("final text fails checks: " + "; ".join(issues))
        if not await self._cas(run_id, {RunStatus.waiting_review}, RunStatus.running):
            raise RunConflict(f"run {run_id} was already resumed")
        self._launch(run_id, run.thread_id, Command(resume=dict(decision)))
        return await self._get(run_id)

    async def retry(self, run_id: int) -> PipelineRun:
        run = await self._get(run_id)
        if run.status != RunStatus.failed:
            raise RunInvalidState(f"run {run_id} is {run.status.value}, only failed runs retry")
        if not await self._cas(run_id, {RunStatus.failed}, RunStatus.running):
            raise RunConflict(f"run {run_id} changed state")
        self._launch(run_id, run.thread_id, None)  # None = continue from the last checkpoint
        return await self._get(run_id)

    async def cancel(self, run_id: int) -> PipelineRun:
        run = await self._get(run_id)
        if run.status not in LIVE_RUN_STATUSES:
            raise RunInvalidState(f"run {run_id} is {run.status.value}, nothing to cancel")
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await self._mark(run_id, RunStatus.cancelled, reason="cancelled by owner")
        return await self._get(run_id)

    async def recover(self) -> None:
        """Startup reconciliation. Single-process until M6 adds the leader lock."""
        async with self.db.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineRun).where(PipelineRun.status.in_(LIVE_RUN_STATUSES))
                    )
                )
                .scalars()
                .all()
            )
        for run in rows:
            cfg = self._config(run.thread_id)
            if run.graph_version != GRAPH_VERSION:
                await self._mark(
                    run.id, RunStatus.failed, reason=f"superseded by graph {GRAPH_VERSION}"
                )
                continue
            snap = await self.graph.aget_state(cfg)
            if not snap.values:
                await self._mark(run.id, RunStatus.failed, reason="no checkpoint found at startup")
                continue
            if snap.interrupts:
                await self._reconcile(run.id, cfg)
                continue
            if not snap.next:
                await self._reconcile(run.id, cfg)
                continue
            errors = [str(t.error) for t in snap.tasks if t.error]
            transient = not errors or any(
                marker in err for err in errors for marker in TRANSIENT_ERROR_MARKERS
            )
            if transient and run.recover_attempts < MAX_RECOVER_ATTEMPTS:
                async with self.db.session_factory() as session:
                    db_run = await session.get(PipelineRun, run.id)
                    assert db_run is not None
                    db_run.recover_attempts += 1
                    db_run.status = RunStatus.running
                    await session.commit()
                log.warning("recovering interrupted run", run_id=run.id, errors=errors[:1])
                self._launch(run.id, run.thread_id, None)
            else:
                await self._mark(
                    run.id,
                    RunStatus.failed,
                    reason="interrupted and not recoverable",
                    error="; ".join(errors)[:2000] or None,
                )

    async def wait(self, run_id: int, max_wait: float = 20.0) -> None:
        """Await the background task for a run (tests and the CLI use this)."""
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), max_wait)

    async def run_view(self, run_id: int) -> tuple[PipelineRun, list[str], dict[str, Any] | None]:
        run = await self._get(run_id)
        snap = await self.graph.aget_state(self._config(run.thread_id))
        waiting = snap.interrupts[0].value if snap.interrupts else None
        return run, list(snap.next), waiting if isinstance(waiting, dict) else None

    # -- internals --------------------------------------------------------------------------

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}

    def _launch(self, run_id: int, thread_id: str, payload: Any) -> None:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        task = asyncio.create_task(
            self._execute(run_id, thread_id, payload, lock), name=f"pipeline-run-{run_id}"
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(run_id, None))

    async def _execute(self, run_id: int, thread_id: str, payload: Any, lock: asyncio.Lock) -> None:
        async with lock:
            cfg = self._config(thread_id)
            async with self.db.session_factory() as session:
                prefs = await load_preferences(session)
            ctx = PipelineContext(
                db=self.db,
                settings=self.settings,
                prefs=prefs,
                llm=self.llm,
                publisher=self.publisher,
            )
            try:
                await self.graph.ainvoke(payload, cfg, context=ctx, durability="sync")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the run row is the error channel
                log.exception("pipeline run failed", run_id=run_id)
                await self._mark(
                    run_id,
                    RunStatus.failed,
                    reason=f"{type(e).__name__}: {str(e)[:300]}",
                    error=f"{type(e).__name__}: {e}"[:4000],
                )
                return
            await self._reconcile(run_id, cfg)

    async def _reconcile(self, run_id: int, cfg: dict[str, Any]) -> None:
        snap = await self.graph.aget_state(cfg)
        if snap.interrupts:
            value = snap.interrupts[0].value
            kind = value.get("kind") if isinstance(value, dict) else None
            status = RunStatus.waiting_review if kind == "review" else RunStatus.waiting_checkin
            await self._mark(run_id, status, reason=f"waiting for {kind or 'input'}")
            return
        outcome = snap.values.get("outcome")
        reason = snap.values.get("outcome_reason")
        mapped = OUTCOME_TO_STATUS.get(outcome or "")
        if mapped is None:
            await self._mark(
                run_id,
                RunStatus.failed,
                reason="graph ended without an outcome",
                error=f"outcome={outcome!r}",
            )
            return
        await self._mark(run_id, mapped, reason=reason)

    async def _mark(
        self, run_id: int, status: RunStatus, *, reason: str | None = None, error: str | None = None
    ) -> None:
        async with self.db.session_factory() as session:
            run = await session.get(PipelineRun, run_id)
            if run is None:
                return
            run.status = status
            if reason is not None:
                run.outcome_reason = reason[:2000]
            run.error = error
            run.finished_at = None if status in LIVE_RUN_STATUSES else datetime.now(UTC)
            await session.commit()
        log.info("run status", run_id=run_id, status=status.value, reason=(reason or "")[:120])

    async def _cas(self, run_id: int, expected: set[RunStatus], new: RunStatus) -> bool:
        async with self.db.session_factory() as session:
            stmt = (
                update(PipelineRun)
                .where(PipelineRun.id == run_id, PipelineRun.status.in_(list(expected)))
                .values(status=new, finished_at=None)
                .returning(PipelineRun.id)
            )
            row = (await session.execute(stmt)).first()
            await session.commit()
            return row is not None

    async def _pending_interrupt(self, thread_id: str, kind: str) -> dict[str, Any]:
        snap = await self.graph.aget_state(self._config(thread_id))
        if not snap.interrupts:
            raise RunInvalidState("the run is not waiting for input")
        value = snap.interrupts[0].value
        actual = value.get("kind") if isinstance(value, dict) else None
        if actual != kind:
            raise RunInvalidState(f"the run is waiting for {actual!r}, not {kind!r}")
        return dict(value)

    async def _expect_interrupt(self, thread_id: str, kind: str) -> None:
        await self._pending_interrupt(thread_id, kind)

    async def _get(self, run_id: int) -> PipelineRun:
        async with self.db.session_factory() as session:
            run = await session.get(PipelineRun, run_id)
            if run is None:
                raise RunNotFound(f"run {run_id} not found")
            return run
