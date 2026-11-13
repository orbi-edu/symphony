"""The assistant's host tools: `symphony_status`, `tracker_search` and `propose_tickets`.

Every tool runs on the host. The first two only read. `propose_tickets` only records a plan:
the engine shows it with buttons, and nothing reaches the tracker until a mapped person clicks
(`confirm.py`).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable

from ..merger.agents import HostTool, _obj
from ..merger.store import MergerStore, registered_repos

MAX_TICKETS = 15
MAX_TITLE = 120
MAX_BODY = 20_000
MAX_SUMMARY = 4_000
MAX_LABELS = 10
MAX_LABEL = 50
KEY = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SEARCH_LIMIT = 20
STATUS_ISSUES = 25

_S = {"type": "string"}
_STRINGS = {"type": "array", "items": _S}


class _Fleet:
    """Every repository a merger serves, for `LiveReader.overview`."""

    def __init__(self, client, namespace: str) -> None:
        self.client, self.namespace = client, namespace

    def repos(self) -> list[str]:
        return registered_repos(self.client, self.namespace)

    def store(self, repo: str) -> MergerStore:
        return MergerStore(self.client, self.namespace, repo)


def status_reader(client, namespace: str, cfg_states: dict) -> Callable[[], dict]:
    """`symphony_status`: the admin overview (counts, daemon, merger, rate limits) plus the
    tracked tickets and the recent failures. No cost, token or credential data."""
    from ..admin_commands import AdminStore
    from ..admin_read import LiveReader
    from ..sessions import SessionStore
    from ..teamconfig import TeamConfig

    reader = LiveReader(
        client,
        sessions=SessionStore(client, namespace),
        teamconfig=TeamConfig(client, namespace),
        store=AdminStore(client, namespace),
        merger=_Fleet(client, namespace),
        review_state=cfg_states.get("review", "In Review"),
        merging_state=cfg_states.get("merging", "Merging"),
    )

    def read() -> dict:
        overview = reader.overview(int(time.time() * 1000))
        issues = reader.issues()
        keep = ("identifier", "title", "repo", "phase", "tracker_state", "attempt", "pr_url",
                "reason", "updated_at")
        failures = [i for i in issues if i["status"] in ("blocked", "retry_queued") or i["reason"]]
        return {
            "daemon": overview["daemon"],
            "counts": overview["counts"],
            "merger": overview["merger"],
            "rate_limits": [{k: r[k] for k in ("model", "stage", "reset_at")}
                            for r in overview["rate_limits"]],
            "held": [{k: h[k] for k in ("identifier", "by", "reason")} for h in overview["held"]],
            "tickets": [{k: i[k] for k in keep} for i in issues[:STATUS_ISSUES]],
            "recent_failures": [{k: i[k] for k in keep} for i in failures[:STATUS_ISSUES]],
        }

    return read


def _safe(fn: Callable[[dict], object]) -> Callable[[dict, int], str]:
    """A host tool answer as JSON text. A failure answers with its kind, never a traceback."""

    def answer(args: dict, _index: int) -> str:
        try:
            return json.dumps(fn(args), ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 — the agent gets the failure, the run goes on
            return json.dumps({"error": type(exc).__name__, "message": str(exc)[:300]})

    return answer


def status_tool(read: Callable[[], dict]) -> HostTool:
    return HostTool(
        "symphony_status",
        "Symphony's live state: tickets and their phase, agent sessions, the merger queue, "
        "rate limits and recent failures. Read-only.",
        _obj([], {}),
        answer=_safe(lambda _args: read()),
    )


def search_tool(search: Callable[[str, int], list[dict]]) -> HostTool:
    def check(args: dict) -> str | None:
        if not args["query"].strip():
            return "query must not be empty"
        limit = args.get("limit", 10)
        if not 1 <= limit <= SEARCH_LIMIT:
            return f"limit must be 1-{SEARCH_LIMIT}"
        return None

    return HostTool(
        "tracker_search",
        "Search the ticket tracker by words in the title or body. Returns "
        "[{identifier, title, state, url}], newest first. Read-only.",
        _obj(["query"], {"query": _S, "limit": {"type": "integer"}}),
        check=check,
        answer=_safe(lambda args: search(args["query"].strip(), args.get("limit", 10))),
    )


def propose_tool(repositories: Iterable[str]) -> HostTool:
    allowed = tuple(repositories)
    ticket = _obj(["key", "title", "body"], {
        "key": _S, "title": _S, "body": _S, "repo": _S, "labels": _STRINGS, "parent": _S,
        "blocked_by": _STRINGS,
    })
    return HostTool(
        "propose_tickets",
        "Propose tickets for the people in this thread. Nothing is created now: they see the "
        "plan with a Create button. `key` is a short id you choose; `parent` and `blocked_by` "
        f"name other keys. At most {MAX_TICKETS} tickets, titles up to {MAX_TITLE} characters, "
        f"bodies up to {MAX_BODY}. Call it once, as your last step.",
        _obj(["summary", "tickets"], {"summary": _S, "tickets": {"type": "array", "items": ticket}}),
        terminal=True,
        check=lambda args: validate_proposal(args, allowed),
        answer=lambda _args, _i: "Proposed. The people in the thread now see it with a Create button.",
    )


def validate_proposal(args: dict, repositories: Iterable[str] = ()) -> str | None:
    """None when `args` is a valid `propose_tickets` call, else what to fix."""
    allowed = {r.casefold() for r in repositories}
    if not args["summary"].strip() or len(args["summary"]) > MAX_SUMMARY:
        return f"summary must be 1-{MAX_SUMMARY} characters"
    tickets = args["tickets"]
    if not 1 <= len(tickets) <= MAX_TICKETS:
        return f"propose 1-{MAX_TICKETS} tickets"
    by_key: dict[str, dict] = {}
    for i, t in enumerate(tickets):
        where = f"tickets[{i}]"
        if not KEY.fullmatch(t["key"]):
            return f"{where}.key must be 1-32 letters, digits, '-' or '_'"
        if t["key"] in by_key:
            return f"{where}.key {t['key']!r} is used twice"
        if not t["title"].strip() or len(t["title"]) > MAX_TITLE:
            return f"{where}.title must be 1-{MAX_TITLE} characters"
        if len(t["body"]) > MAX_BODY:
            return f"{where}.body must be at most {MAX_BODY} characters"
        repo = t.get("repo")
        if repo is not None:
            if not REPO.fullmatch(repo):
                return f"{where}.repo must be owner/name"
            if allowed and repo.casefold() not in allowed:
                return f"{where}.repo must be one of {', '.join(sorted(allowed))}"
        labels = t.get("labels") or []
        if len(labels) > MAX_LABELS:
            return f"{where}.labels: at most {MAX_LABELS}"
        for label in labels:
            if not label.strip() or len(label) > MAX_LABEL:
                return f"{where}.labels must be 1-{MAX_LABEL} characters each"
            if label.lower().startswith("repo:"):
                return f"{where}.labels: use `repo` instead of a repo: label"
        by_key[t["key"]] = t
    for i, t in enumerate(tickets):
        where = f"tickets[{i}]"
        parent = t.get("parent")
        if parent is not None:
            if parent not in by_key or parent == t["key"]:
                return f"{where}.parent must be the key of another ticket"
            parent_repo = by_key[parent].get("repo")
            if t.get("repo") and parent_repo and t["repo"].casefold() != parent_repo.casefold():
                return f"{where}.repo: a sub-ticket uses its parent's repository"
        for blocker in t.get("blocked_by") or []:
            if blocker not in by_key or blocker == t["key"]:
                return f"{where}.blocked_by must name keys of other tickets"
    try:
        creation_order(tickets)
    except ValueError as exc:
        return str(exc)
    return None


def creation_order(tickets: list[dict]) -> list[dict]:
    """Parents before children, blockers before what they block; ties keep the proposal's
    order. Raises ValueError on a cycle."""
    keys = [t["key"] for t in tickets]
    by_key = {t["key"]: t for t in tickets}
    after: dict[str, set[str]] = {k: set() for k in keys}  # key → keys it must follow
    for t in tickets:
        if t.get("parent"):
            after[t["key"]].add(t["parent"])
        after[t["key"]].update(t.get("blocked_by") or [])
    done: list[str] = []
    placed: set[str] = set()
    while len(done) < len(keys):
        ready = [k for k in keys if k not in placed and after[k] <= placed]
        if not ready:
            raise ValueError("parent and blocked_by links form a cycle")
        done.append(ready[0])
        placed.add(ready[0])
    return [by_key[k] for k in done]
