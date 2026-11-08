"""Application settings loaded from environment variables and the repo-root .env file."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SECRET = "change-me-generate-with-secrets-token-urlsafe"  # noqa: S105 placeholder
EXAMPLE_PASSWORD = "change-me"  # noqa: S105 placeholder


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="XBOT_",
        env_file=(REPO_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    secret_key: str = EXAMPLE_SECRET
    dashboard_password: str = ""
    database_url: str = "postgresql+psycopg://xbot:xbot@127.0.0.1:5433/xbot"
    timezone: str = "Asia/Kolkata"
    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str = "http://127.0.0.1:8000"
    frontend_dist: Path | None = None

    llm_provider: Literal["anthropic", "mock"] = "anthropic"
    llm_model: str = "claude-opus-5"
    publisher: Literal["x", "mock"] = "x"

    # External credentials are read without the XBOT_ prefix so they match vendor conventions.
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "XBOT_ANTHROPIC_API_KEY")
    )
    github_token: str | None = Field(
        default=None, validation_alias=AliasChoices("GITHUB_TOKEN", "XBOT_GITHUB_TOKEN")
    )
    x_client_id: str | None = Field(
        default=None, validation_alias=AliasChoices("X_CLIENT_ID", "XBOT_X_CLIENT_ID")
    )
    x_client_secret: str | None = Field(
        default=None, validation_alias=AliasChoices("X_CLIENT_SECRET", "XBOT_X_CLIENT_SECRET")
    )
    x_redirect_uri: str = "http://127.0.0.1:8000/api/x/callback"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_mock(self) -> bool:
        return self.llm_provider == "mock" or self.publisher == "mock"

    @property
    def fernet_key(self) -> bytes:
        """Derive a 32-byte url-safe base64 key for Fernet from the free-form secret."""
        digest = hashlib.sha256(f"xbot-fernet:{self.secret_key}".encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def runtime_problems(self) -> list[str]:
        """Configuration mistakes that must stop a dev/prod process from starting."""
        problems: list[str] = []
        if self.env == "test":
            return problems
        if self.secret_key == EXAMPLE_SECRET or len(self.secret_key) < 32:
            problems.append("XBOT_SECRET_KEY is the example value or shorter than 32 characters")
        if not self.dashboard_password or self.dashboard_password == EXAMPLE_PASSWORD:
            problems.append("XBOT_DASHBOARD_PASSWORD is empty or the example value")
        if self.env == "prod" and self.is_mock:
            problems.append("mock providers are enabled in prod")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
