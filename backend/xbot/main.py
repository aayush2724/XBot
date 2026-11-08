"""FastAPI application factory. Run with: uvicorn xbot.main:create_app --factory"""

from __future__ import annotations

import utils.session
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from xbot.api.deps import CSRF_HEADER, CSRF_VALUE
from xbot.api.router import api_router
from xbot.config import Settings, get_settings
from xbot.db.base import Database
from xbot.intelligence.runner import PipelineRunner
from xbot.llm.factory import build_provider
from xbot.logging import configure_logging, get_logger
from xbot.publishing.factory import build_publisher

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_EXEMPT = frozenset({"/api/auth/login"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("xbot.main")

    problems = settings.runtime_problems()
    if problems:
        raise RuntimeError("refusing to start: " + "; ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url, echo=False)
        app.state.db = db
        if await db.ping():
            log.info("database reachable")
        else:
            log.warning("database unreachable at startup; health will report degraded")
        if settings.is_mock:
            log.warning("MOCK providers active", llm=settings.llm_provider, pub=settings.publisher)
        else:
            log.info("llm provider", provider=settings.llm_provider, model=settings.llm_model)
        app.state.llm = build_provider(settings)
        app.state.publisher = build_publisher(db, settings)
        runner = PipelineRunner(db, settings, app.state.llm, publisher=app.state.publisher)
        await runner.startup()
        await runner.recover()
        app.state.runner = runner
        try:
            yield
        finally:
            await runner.shutdown()
            await db.dispose()

    app = FastAPI(title="XBot", version="0.1.0", lifespan=lifespan, docs_url="/api/docs")
    app.state.settings = settings

    @app.middleware("http")
    async def csrf_header_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if (
            path.startswith("/api/")
            and request.method not in SAFE_METHODS
            and path not in CSRF_EXEMPT
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE
        ):
            return JSONResponse(
                {"detail": f"missing {CSRF_HEADER}: {CSRF_VALUE} header"}, status_code=403
            )
        return await call_next(request)

    app.include_router(api_router, prefix="/api")

    dist = settings.frontend_dist
    if dist and dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app
