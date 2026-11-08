from __future__ import annotations

from xbot.config import Settings
from xbot.db.base import Database
from xbot.publishing.base import Publisher
from xbot.publishing.mock_publisher import MockPublisher
from xbot.publishing.x_publisher import XPublisher


def build_publisher(db: Database, settings: Settings) -> Publisher:
    if settings.publisher == "mock":
        return MockPublisher()
    return XPublisher(db, settings)
