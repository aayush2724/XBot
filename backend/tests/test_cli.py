from __future__ import annotations

import httpx
import pytest

from xbot.cli import run


async def test_cli_round_trip(
    auth_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(["quests", "Ship M2", "Read PKCE"], auth_client) == 0
    assert "1. Ship M2" in capsys.readouterr().out

    assert await run(["log", "Fixed the sync retry loop", "--kind", "worked_on"], auth_client) == 0
    assert "logged (worked_on)" in capsys.readouterr().out

    assert await run(["done", "1"], auth_client) == 0
    assert "done: Ship M2" in capsys.readouterr().out

    assert await run(["today"], auth_client) == 0
    out = capsys.readouterr().out
    assert "[x] 1. Ship M2" in out
    assert "[ ] 2. Read PKCE" in out
    assert "(worked_on) Fixed the sync retry loop" in out


async def test_cli_done_with_bad_position(
    auth_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(["done", "3"], auth_client) == 2
    assert "no quest at position 3" in capsys.readouterr().err


async def test_cli_too_many_quests(
    auth_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(["quests", "a", "b", "c", "d"], auth_client) == 2
    assert "error" in capsys.readouterr().err


async def test_cli_keygen(
    auth_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(["keygen"], auth_client) == 0
    assert len(capsys.readouterr().out.strip()) >= 60
