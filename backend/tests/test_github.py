from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import select

from xbot.db.models import GithubCommit, GithubSync, RepoSetting
from xbot.ingestion import github as gh
from xbot.ingestion.github import (
    GitHubClient,
    GitHubSource,
    build_diff,
    day_bounds_utc,
    is_owner_commit,
    sync_github,
)
from xbot.preferences import Preferences

TZ = ZoneInfo("Asia/Kolkata")
D = date(2026, 11, 8)
USER = "aayush2724"
EMAIL = "aayush2615@gmail.com"
API = "https://api.github.com"
SHA_A, SHA_B, SHA_C, SHA_D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def _repo(full_name: str, private: bool, pushed_at: str) -> dict[str, Any]:
    return {"full_name": full_name, "private": private, "pushed_at": pushed_at}


def _commit(sha: str, email: str, login: str | None, when: str, msg: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "html_url": f"https://github.com/{USER}/Vetra/commit/{sha}",
        "commit": {"author": {"email": email, "date": when, "name": "x"}, "message": msg},
        "author": {"login": login} if login else None,
    }


def _detail(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "stats": {"additions": 12, "deletions": 3},
            "files": [
                {
                    "filename": "xbot/sync.py",
                    "status": "modified",
                    "additions": 10,
                    "deletions": 3,
                    "patch": "+" * 5000,
                },
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 2,
                    "deletions": 0,
                    "patch": "+docs",
                },
            ],
        },
    )


def _mock_github(mock: respx.MockRouter) -> dict[str, respx.Route]:
    routes = {
        "repos": mock.get(f"{API}/users/{USER}/repos").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _repo(f"{USER}/Vetra", False, "2026-11-08T10:00:00Z"),
                    _repo(f"{USER}/secret-thing", True, "2026-11-08T10:00:00Z"),
                    _repo(f"{USER}/old", False, "2026-09-01T00:00:00Z"),
                ],
            )
        ),
        "vetra_commits": mock.get(f"{API}/repos/{USER}/Vetra/commits").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _commit(SHA_A, EMAIL, None, "2026-11-08T05:00:00Z", "Add retry loop to sync"),
                    _commit(SHA_B, "other@example.com", USER, "2026-11-08T12:00:00Z", "Fix typo"),
                    _commit(SHA_C, "stranger@example.com", "someone", "2026-11-08T13:00:00Z", "PR"),
                    _commit(SHA_D, EMAIL, None, "2026-11-07T17:00:00Z", "Yesterday IST"),
                ],
            )
        ),
        "secret_commits": mock.get(url__regex=rf"{API}/repos/{USER}/secret-thing/commits.*").mock(
            return_value=httpx.Response(200, json=[])
        ),
        "old_commits": mock.get(url__regex=rf"{API}/repos/{USER}/old/commits.*").mock(
            return_value=httpx.Response(200, json=[])
        ),
        "detail": mock.get(url__regex=rf"{API}/repos/{USER}/Vetra/commits/[a-f0-9]{{40}}$").mock(
            side_effect=_detail
        ),
    }
    return routes


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "RETRY_BASE_DELAY", 0.0)


def _prefs(**kw: Any) -> Preferences:
    return Preferences(github_username=USER, github_author_emails=[EMAIL], **kw)


def test_day_bounds_are_converted_from_ist_to_utc() -> None:
    since, until = day_bounds_utc(D, TZ)
    assert since == datetime(2026, 11, 7, 18, 30, tzinfo=UTC)
    assert until == datetime(2026, 11, 8, 18, 30, tzinfo=UTC)


def test_owner_match_by_email_or_login() -> None:
    emails = {EMAIL}
    assert is_owner_commit(_commit("x", EMAIL.upper(), None, "", ""), USER, emails)
    assert is_owner_commit(_commit("x", "other@x.com", USER.upper(), "", ""), USER, emails)
    assert not is_owner_commit(_commit("x", "other@x.com", "someone", "", ""), USER, emails)
    assert not is_owner_commit(_commit("x", "other@x.com", None, "", ""), USER, emails)


def test_build_diff_truncates_per_file_and_total() -> None:
    files = [{"filename": f"f{i}.py", "patch": "x" * 5000} for i in range(10)]
    diff = build_diff(files)
    assert diff is not None
    assert "[truncated 1000 chars]" in diff
    assert "remaining files omitted" in diff
    assert len(diff) < gh.MAX_PATCH_PER_COMMIT + 500
    assert build_diff([{"filename": "bin", "patch": None}]) is None


async def test_sync_reads_public_repos_only_and_filters_author(app) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=False) as mock:
        routes = _mock_github(mock)
        async with app.state.db.session_factory() as session, GitHubClient(None) as client:
            result = await sync_github(session, client, _prefs(), D, TZ)

        assert result.status == "ok", result
        assert result.repos_checked == 1
        assert result.commits_found == 2
        assert routes["secret_commits"].call_count == 0, "private repo must never be queried"
        assert routes["old_commits"].call_count == 0, "repo pushed long ago is skipped"
        params = routes["vetra_commits"].calls.last.request.url.params
        assert params["since"] == "2026-11-07T18:30:00Z"
        assert params["until"] == "2026-11-08T18:30:00Z"
        assert "author" not in params

    async with app.state.db.session_factory() as session:
        rows = (
            (await session.execute(select(GithubCommit).order_by(GithubCommit.authored_at)))
            .scalars()
            .all()
        )
        assert [r.sha for r in rows] == [SHA_A, SHA_B]
        assert rows[0].additions == 12 and rows[0].deletions == 3
        assert rows[0].files[0]["filename"] == "xbot/sync.py"
        assert all(r.diff is None for r in rows), "diffs are opt-in"
        assert rows[1].author_login == USER
        sync_row = (await session.execute(select(GithubSync))).scalar_one()
        assert sync_row.status == "ok" and sync_row.commits_found == 2


async def test_sync_diff_opt_in_and_resync_is_idempotent(app) -> None:  # type: ignore[no-untyped-def]
    async with app.state.db.session_factory() as session:
        session.add(RepoSetting(repo_full_name=f"{USER}/Vetra", include_diffs=True))
        await session.commit()
    with respx.mock(assert_all_called=False) as mock:
        routes = _mock_github(mock)
        async with app.state.db.session_factory() as session, GitHubClient(None) as client:
            first = await sync_github(session, client, _prefs(), D, TZ)
            second = await sync_github(session, client, _prefs(), D, TZ)
        assert first.commits_found == second.commits_found == 2
        assert routes["detail"].call_count == 2, "details fetched once per commit, not per sync"
    async with app.state.db.session_factory() as session:
        rows = (await session.execute(select(GithubCommit))).scalars().all()
        assert len(rows) == 2
        assert all(r.diff and "[truncated 1000 chars]" in r.diff for r in rows)


async def test_sync_skips_excluded_repo(app) -> None:  # type: ignore[no-untyped-def]
    async with app.state.db.session_factory() as session:
        session.add(RepoSetting(repo_full_name=f"{USER}/Vetra", excluded=True))
        await session.commit()
    with respx.mock(assert_all_called=False) as mock:
        routes = _mock_github(mock)
        async with app.state.db.session_factory() as session, GitHubClient(None) as client:
            result = await sync_github(session, client, _prefs(), D, TZ)
        assert result.status == "ok" and result.repos_checked == 0 and result.commits_found == 0
        assert routes["vetra_commits"].call_count == 0


async def test_sync_records_rate_limit_as_error(app) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/users/{USER}/repos").mock(
            return_value=httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}
            )
        )
        async with app.state.db.session_factory() as session, GitHubClient(None) as client:
            result = await sync_github(session, client, _prefs(), D, TZ)
        assert result.status == "error"
        assert result.error and "rate limit" in result.error
    async with app.state.db.session_factory() as session:
        sync_row = (await session.execute(select(GithubSync))).scalar_one()
        assert sync_row.status == "error" and sync_row.error == result.error


async def test_sync_retries_transient_5xx(app) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=False) as mock:
        routes = _mock_github(mock)
        routes["repos"].side_effect = [
            httpx.Response(502),
            httpx.Response(200, json=[_repo(f"{USER}/Vetra", False, "2026-11-08T10:00:00Z")]),
        ]
        async with app.state.db.session_factory() as session, GitHubClient(None) as client:
            result = await sync_github(session, client, _prefs(), D, TZ)
        assert result.status == "ok" and result.commits_found == 2
        assert routes["repos"].call_count == 2


async def test_sync_without_username_is_recorded_error(app) -> None:  # type: ignore[no-untyped-def]
    async with app.state.db.session_factory() as session, GitHubClient(None) as client:
        result = await sync_github(session, client, Preferences(), D, TZ)
    assert result.status == "error" and "github_username" in (result.error or "")


async def test_github_source_renders_commits_as_items(app) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=False) as mock:
        _mock_github(mock)
        async with app.state.db.session_factory() as session, GitHubClient(None) as client:
            await sync_github(session, client, _prefs(), D, TZ)
    async with app.state.db.session_factory() as session:
        items = await GitHubSource().collect(session, D)
    assert [i.ref_id for i in items] == [f"commit:{SHA_A[:12]}", f"commit:{SHA_B[:12]}"]
    assert items[0].source == "commit"
    assert f"[{USER}/Vetra] Add retry loop to sync" in items[0].content
    assert "xbot/sync.py" in items[0].content
    assert items[0].metadata["url"].endswith(SHA_A)


async def test_sync_route_day_view_and_repo_settings(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.post(f"/api/days/{D}/github/sync")
    assert r.status_code == 400  # username not configured yet

    r = await auth_client.put(
        "/api/settings",
        json={"preferences": {"github_username": USER, "github_author_emails": [EMAIL]}},
    )
    assert r.status_code == 200

    with respx.mock(assert_all_called=False) as mock:
        _mock_github(mock)
        r = await auth_client.post(f"/api/days/{D}/github/sync")
    assert r.status_code == 200, r.text
    assert r.json()["commits_found"] == 2

    day = (await auth_client.get(f"/api/days/{D}")).json()
    assert [c["sha"] for c in day["commits"]] == [SHA_A, SHA_B]
    assert day["commits"][0]["has_diff"] is False
    assert "diff" not in day["commits"][0]

    repos = (await auth_client.get("/api/github/repos")).json()
    assert repos == [{"repo_full_name": f"{USER}/Vetra", "include_diffs": False, "excluded": False}]

    r = await auth_client.patch(f"/api/github/repos/{USER}/Vetra", json={"include_diffs": True})
    assert r.status_code == 200 and r.json()["include_diffs"] is True
    repos = (await auth_client.get("/api/github/repos")).json()
    assert repos[0]["include_diffs"] is True and repos[0]["excluded"] is False
