from __future__ import annotations

from fastapi import APIRouter

from xbot.api.routes import auth, days, drafts, github, health, history, pipeline, settings, x_auth

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(days.router)
api_router.include_router(settings.router)
api_router.include_router(github.router)
api_router.include_router(pipeline.router)
api_router.include_router(drafts.router)
api_router.include_router(x_auth.router)
api_router.include_router(history.router)
