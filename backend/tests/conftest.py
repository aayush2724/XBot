"""Shared fixtures: a migrated Postgres test database, an app with test settings, HTTP clients."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from xbot.config import Settings
from xbot.db.base import Base, Database
from xbot.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get(
    "XBOT_TEST_DATABASE_URL", "postgresql+psycopg://xbot:xbot@127.0.0.1:5433/xbot_test"
)


def make_test_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "secret_key": "test-secret-key-that-is-long-enough-for-tests",
        "dashboard_password": "correct-horse",
        "database_url": TEST_DB_URL,
        "timezone": "Asia/Kolkata",
        "llm_provider": "mock",
        "publisher": "mock",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Reset the test database schema and apply every Alembic migration once per session."""
    import psycopg

    sync_url = TEST_DB_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    env = {**os.environ, "XBOT_DATABASE_URL": TEST_DB_URL, "XBOT_ENV": "test"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def settings() -> Settings:
    return make_test_settings()


@pytest.fixture
async def app(settings: Settings):  # type: ignore[no-untyped-def]
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application
        db: Database = application.state.db
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        async with db.engine.begin() as conn:
            if tables:
                await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            # LangGraph's checkpoint tables are outside Base.metadata; clear them too so a
            # reused run id can never resume a thread left over from an earlier test.
            await conn.execute(
                text(
                    "DO $$ BEGIN IF to_regclass('public.checkpoints') IS NOT NULL THEN "
                    "EXECUTE 'TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes'; "
                    "END IF; END $$;"
                )
            )


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    r = await client.post("/api/auth/login", json={"password": "correct-horse"})
    assert r.status_code == 200, r.text
    client.headers["x-requested-with"] = "xbot"
    return client
