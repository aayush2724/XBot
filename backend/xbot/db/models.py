"""ORM models. Tables are added milestone by milestone; Alembic migrations live in ../alembic."""

from __future__ import annotations

from xbot.db.base import Base, TimestampMixin

__all__ = ["Base", "TimestampMixin"]
