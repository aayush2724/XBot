from __future__ import annotations

from fastapi import APIRouter

from xbot.api.routes import auth, days, github, health, pipeline, settings

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(days.router)
api_router.include_router(settings.router)
api_router.include_router(github.router)
api_router.include_router(pipeline.router)
