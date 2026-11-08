"""`xbot` quick-entry CLI. Talks to the running XBot server using the dashboard password
from the repo-root .env, so journaling from a terminal is one command.

    xbot today                      show quests, entries, check-ins, latest run
    xbot log "text" [--kind ...]    add a journal entry
    xbot quests "a" "b" "c"         set today's quests (up to 3)
    xbot done 2                     mark quest 2 done
    xbot run [--supersede]          start today's pipeline run
    xbot status                     show the latest run and what it is waiting for
    xbot answer <checkin_id> "..."  answer a check-in question
    xbot submit | xbot skip         resume a run waiting on check-ins
    xbot retry                      retry today's failed run
    xbot keygen                     print a value for XBOT_SECRET_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from collections.abc import Sequence
from typing import Any

import httpx

from xbot.config import get_settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xbot", description="XBot quick-entry CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("today", help="show today's quests, entries, check-ins and latest run")
    log = sub.add_parser("log", help="add a journal entry for today")
    log.add_argument("text")
    log.add_argument(
        "--kind", default="update", choices=["update", "worked_on", "learned", "challenge", "plan"]
    )
    q = sub.add_parser("quests", help="set today's quests (up to 3)")
    q.add_argument("texts", nargs="+")
    d = sub.add_parser("done", help="mark today's quest at POSITION (1-3) as done")
    d.add_argument("position", type=int)
    r = sub.add_parser("run", help="start today's pipeline run")
    r.add_argument("--supersede", action="store_true", help="replace a live run for today")
    sub.add_parser("status", help="show today's latest run")
    a = sub.add_parser("answer", help="answer a check-in question")
    a.add_argument("checkin_id", type=int)
    a.add_argument("text")
    sub.add_parser("submit", help="resume today's run after answering its check-ins")
    sub.add_parser("skip", help="resume today's run without more check-in answers")
    sub.add_parser("retry", help="retry today's failed run")
    sub.add_parser("keygen", help="print a random value for XBOT_SECRET_KEY")
    return p


def _print_run(run: dict[str, Any] | None) -> None:
    if not run:
        print("run: (none yet)")
        return
    cost = run.get("total_cost_usd") or 0.0
    print(f"run #{run['id']}: {run['status']}  cost=${cost:.4f}")
    if run.get("outcome_reason"):
        print(f"  {run['outcome_reason']}")
    if run.get("error"):
        print(f"  error: {run['error'][:200]}")


def _print_day(view: dict[str, Any]) -> None:
    print(f"{view['local_date']}  decision={view['decision']}")
    quests: list[dict[str, Any]] = view.get("quests") or []
    print("quests:" if quests else "quests: (none)")
    for q in quests:
        mark = {"planned": " ", "done": "x", "dropped": "-"}[q["status"]]
        print(f"  [{mark}] {q['position']}. {q['text']}")
    entries: list[dict[str, Any]] = view.get("entries") or []
    print("entries:" if entries else "entries: (none)")
    for e in entries:
        print(f"  ({e['kind']}) {e['content']}")
    commits: list[dict[str, Any]] = view.get("commits") or []
    if commits:
        print(f"commits: {len(commits)}")
    checkins: list[dict[str, Any]] = view.get("checkins") or []
    pending = [c for c in checkins if not c["answered_at"]]
    if pending:
        print("unanswered check-ins:")
        for c in pending:
            print(f"  #{c['id']}: {c['question']}")
    _print_run(view.get("latest_run"))


async def _today(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get("/api/days/today")
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


async def run(argv: Sequence[str], client: httpx.AsyncClient) -> int:
    """Execute one command against an authenticated client. Returns an exit code."""
    args = _parser().parse_args(argv)
    if args.command == "keygen":
        print(secrets.token_urlsafe(48))
        return 0
    view = await _today(client)
    today = view["local_date"]
    if args.command == "today":
        _print_day(view)
        return 0
    if args.command == "log":
        r = await client.post(
            f"/api/days/{today}/entries",
            json={"kind": args.kind, "content": args.text, "source": "cli"},
        )
        r.raise_for_status()
        print(f"logged ({args.kind}) for {today}")
        return 0
    if args.command == "quests":
        r = await client.put(f"/api/days/{today}/quests", json={"quests": args.texts})
        if r.status_code == 422:
            print(f"error: {r.json().get('detail')}", file=sys.stderr)
            return 2
        r.raise_for_status()
        _print_day(r.json())
        return 0
    if args.command == "done":
        match = [q for q in view["quests"] if q["position"] == args.position]
        if not match:
            print(f"error: no quest at position {args.position}", file=sys.stderr)
            return 2
        r = await client.patch(f"/api/quests/{match[0]['id']}", json={"status": "done"})
        r.raise_for_status()
        print(f"done: {match[0]['text']}")
        return 0
    if args.command == "run":
        r = await client.post(
            f"/api/days/{today}/pipeline/run", params={"supersede": str(args.supersede).lower()}
        )
        if r.status_code == 409:
            print(f"error: {r.json().get('detail')}", file=sys.stderr)
            return 2
        r.raise_for_status()
        print(f"started run #{r.json()['id']} for {today}; check `xbot status` shortly")
        return 0
    if args.command == "status":
        _print_run(view.get("latest_run"))
        return 0
    if args.command == "answer":
        r = await client.post(f"/api/checkins/{args.checkin_id}/answer", json={"answer": args.text})
        if r.status_code == 404:
            print("error: no such check-in", file=sys.stderr)
            return 2
        r.raise_for_status()
        print(f"answered check-in #{args.checkin_id}")
        return 0
    if args.command in ("submit", "skip", "retry"):
        latest = view.get("latest_run")
        if not latest:
            print("error: no run for today", file=sys.stderr)
            return 2
        suffix = {"submit": "checkins/submit", "skip": "checkins/skip", "retry": "retry"}
        r = await client.post(f"/api/pipeline/runs/{latest['id']}/{suffix[args.command]}")
        if r.status_code in (404, 409):
            print(f"error: {r.json().get('detail')}", file=sys.stderr)
            return 2
        r.raise_for_status()
        print(f"run #{latest['id']}: {args.command} accepted, now {r.json()['status']}")
        return 0
    return 1


async def _main_async(argv: Sequence[str]) -> int:
    settings = get_settings()
    if argv and argv[0] == "keygen":
        return await run(argv, httpx.AsyncClient())
    async with httpx.AsyncClient(
        base_url=settings.public_url, headers={"x-requested-with": "xbot"}, timeout=30
    ) as client:
        try:
            login = await client.post(
                "/api/auth/login", json={"password": settings.dashboard_password}
            )
        except httpx.ConnectError:
            print(f"error: cannot reach XBot at {settings.public_url}", file=sys.stderr)
            return 3
        if login.status_code != 200:
            print("error: login failed; check XBOT_DASHBOARD_PASSWORD in .env", file=sys.stderr)
            return 3
        return await run(argv, client)


def main() -> None:
    sys.exit(asyncio.run(_main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
