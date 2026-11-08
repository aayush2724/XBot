"""GitHub source: public-repository commits authored by the owner on a given local day.

Privacy rule (Phase 1, Q3): only repositories with `private == false` are ever queried. The
filter is applied to the repository listing before any commit endpoint is called.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from xbot.db.models import GithubCommit, GithubSync, RepoSetting
from xbot.ingestion.base import SourceItem
from xbot.logging import get_logger
from xbot.preferences import Preferences

log = get_logger("xbot.github")

GITHUB_API = "https://api.github.com"
MAX_PATCH_PER_FILE = 4_000
MAX_PATCH_PER_COMMIT = 20_000
REPO_PAGES = 2
COMMIT_PAGES = 3
RETRY_BASE_DELAY = 1.0


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset_at: datetime | None) -> None:
        self.reset_at = reset_at
        when = reset_at.isoformat() if reset_at else "unknown"
        super().__init__(f"GitHub rate limit hit; resets at {when}")


class GitHubClient:
    """Thin async client with retries on transient failures and rate-limit awareness."""

    def __init__(
        self, token: str | None = None, *, base_url: str = GITHUB_API, timeout: float = 20.0
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "xbot-personal-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        delay = RETRY_BASE_DELAY
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TransportError as e:
                last_error = e
            else:
                if (
                    resp.status_code in (403, 429)
                    and resp.headers.get("x-ratelimit-remaining") == "0"
                ):
                    reset = resp.headers.get("x-ratelimit-reset")
                    reset_at = datetime.fromtimestamp(int(reset), tz=UTC) if reset else None
                    raise RateLimited(reset_at)
                if resp.status_code >= 500:
                    last_error = GitHubError(f"GitHub {resp.status_code} on {path}")
                elif resp.status_code >= 400:
                    raise GitHubError(f"GitHub {resp.status_code} on {path}: {resp.text[:200]}")
                else:
                    return resp.json()
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
        raise GitHubError(str(last_error))

    async def list_user_repos(self, username: str) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        for page in range(1, REPO_PAGES + 1):
            batch = await self._get(
                f"/users/{username}/repos", {"sort": "pushed", "per_page": 50, "page": page}
            )
            repos.extend(batch)
            if len(batch) < 50:
                break
        return repos

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        return await self._get(f"/repos/{full_name}")  # type: ignore[no-any-return]

    async def list_commits(
        self, full_name: str, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        commits: list[dict[str, Any]] = []
        for page in range(1, COMMIT_PAGES + 1):
            batch = await self._get(
                f"/repos/{full_name}/commits",
                {
                    "since": since.isoformat().replace("+00:00", "Z"),
                    "until": until.isoformat().replace("+00:00", "Z"),
                    "per_page": 100,
                    "page": page,
                },
            )
            commits.extend(batch)
            if len(batch) < 100:
                break
        return commits

    async def get_commit(self, full_name: str, sha: str) -> dict[str, Any]:
        return await self._get(f"/repos/{full_name}/commits/{sha}")  # type: ignore[no-any-return]


def day_bounds_utc(local_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(local_date, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def is_owner_commit(commit: dict[str, Any], username: str, emails: set[str]) -> bool:
    email = ((commit.get("commit") or {}).get("author") or {}).get("email") or ""
    login = ((commit.get("author") or {}).get("login")) or ""
    return email.lower() in emails or (bool(login) and login.lower() == username.lower())


def build_diff(files: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    total = 0
    for f in files:
        patch = f.get("patch")
        if not patch:
            continue
        snippet = patch[:MAX_PATCH_PER_FILE]
        if len(patch) > MAX_PATCH_PER_FILE:
            snippet += f"\n... [truncated {len(patch) - MAX_PATCH_PER_FILE} chars]"
        block = f"--- {f.get('filename')}\n{snippet}"
        if total + len(block) > MAX_PATCH_PER_COMMIT:
            parts.append(f"... [remaining files omitted, {len(files)} files total]")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts) if parts else None


class SyncResult(BaseModel):
    status: str
    repos_checked: int
    commits_found: int
    error: str | None = None


async def _repo_settings(session: AsyncSession) -> dict[str, RepoSetting]:
    rows = (await session.execute(select(RepoSetting))).scalars().all()
    return {r.repo_full_name: r for r in rows}


async def sync_github(
    session: AsyncSession,
    client: GitHubClient,
    prefs: Preferences,
    local_date: date,
    tz: ZoneInfo,
) -> SyncResult:
    """Fetch the owner's public commits for `local_date` and upsert them. Never raises for
    GitHub failures; the outcome is recorded in `github_syncs` and returned."""
    if not prefs.github_username:
        result = SyncResult(
            status="error",
            repos_checked=0,
            commits_found=0,
            error="github_username is not configured in settings",
        )
        session.add(
            GithubSync(
                local_date=local_date,
                status="error",
                repos_checked=0,
                commits_found=0,
                error=result.error,
            )
        )
        await session.commit()
        return result

    username = prefs.github_username
    emails = {e.lower() for e in prefs.github_author_emails}
    since, until = day_bounds_utc(local_date, tz)
    repo_settings = await _repo_settings(session)
    repos_checked = 0
    commits_found = 0
    error: str | None = None
    status = "ok"

    try:
        repos = await client.list_user_repos(username)
        for extra in prefs.extra_repos:
            if not any(r.get("full_name", "").lower() == extra.lower() for r in repos):
                repos.append(await client.get_repo(extra))

        for repo in repos:
            full_name = repo.get("full_name") or ""
            if repo.get("private") is not False:
                continue  # public only: `private` must be explicitly false
            rs = repo_settings.get(full_name)
            if rs and rs.excluded:
                continue
            pushed_at = repo.get("pushed_at")
            if not pushed_at or _parse_ts(pushed_at) < since - timedelta(days=1):
                continue
            repos_checked += 1
            include_diffs = bool(rs and rs.include_diffs)
            for commit in await client.list_commits(full_name, since, until):
                if not is_owner_commit(commit, username, emails):
                    continue
                sha = commit["sha"]
                authored_at = _parse_ts(commit["commit"]["author"]["date"])
                if not (since <= authored_at < until):
                    continue
                existing = await session.get(GithubCommit, sha)
                if existing is not None and (existing.diff is not None or not include_diffs):
                    commits_found += 1
                    continue
                detail = await client.get_commit(full_name, sha)
                files = [
                    {
                        "filename": f.get("filename"),
                        "status": f.get("status"),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                    }
                    for f in detail.get("files", [])
                ]
                values = {
                    "sha": sha,
                    "repo_full_name": full_name,
                    "local_date": local_date,
                    "authored_at": authored_at,
                    "author_email": (commit["commit"]["author"].get("email") or "")[:255],
                    "author_login": ((commit.get("author") or {}).get("login") or None),
                    "message": commit["commit"].get("message") or "",
                    "additions": (detail.get("stats") or {}).get("additions", 0),
                    "deletions": (detail.get("stats") or {}).get("deletions", 0),
                    "files": files,
                    "html_url": commit.get("html_url") or "",
                    "diff": build_diff(detail.get("files", [])) if include_diffs else None,
                }
                stmt = insert(GithubCommit).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[GithubCommit.sha],
                    set_={"files": stmt.excluded.files, "diff": stmt.excluded.diff},
                )
                await session.execute(stmt)
                commits_found += 1
    except RateLimited as e:
        status, error = "error", str(e)
    except (GitHubError, httpx.HTTPError) as e:
        status, error = "error", f"{type(e).__name__}: {e}"

    session.add(
        GithubSync(
            local_date=local_date,
            status=status,
            repos_checked=repos_checked,
            commits_found=commits_found,
            error=error,
        )
    )
    await session.commit()
    if error:
        log.warning("github sync failed", local_date=str(local_date), error=error)
    else:
        log.info(
            "github sync ok", local_date=str(local_date), repos=repos_checked, commits=commits_found
        )
    return SyncResult(
        status=status, repos_checked=repos_checked, commits_found=commits_found, error=error
    )


async def list_commits_for_day(session: AsyncSession, local_date: date) -> list[GithubCommit]:
    stmt = (
        select(GithubCommit)
        .where(GithubCommit.local_date == local_date)
        .order_by(GithubCommit.authored_at)
    )
    return list((await session.execute(stmt)).scalars().all())


class GitHubSource:
    """Reads previously synced commits; the sync itself runs separately so the pipeline never
    waits on GitHub."""

    name = "github"

    async def collect(self, session: AsyncSession, local_date: date) -> list[SourceItem]:
        items: list[SourceItem] = []
        for c in await list_commits_for_day(session, local_date):
            paths = ", ".join(f["filename"] for f in (c.files or [])[:12])
            content = (
                f"[{c.repo_full_name}] {c.message.strip()}\n"
                f"(+{c.additions}/-{c.deletions}, files: {paths or 'n/a'})"
            )
            if c.diff:
                content += f"\n--- diff (truncated) ---\n{c.diff}"
            items.append(
                SourceItem(
                    source="commit",
                    ref_id=f"commit:{c.sha[:12]}",
                    occurred_at=c.authored_at,
                    content=content,
                    metadata={"repo": c.repo_full_name, "url": c.html_url, "sha": c.sha},
                )
            )
        return items
