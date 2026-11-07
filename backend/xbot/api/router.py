from __future__ import annotations

from fastapi import APIRouter

from xbot.api.routes import auth, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
