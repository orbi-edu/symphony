"""The assistant's host tools: `symphony_status`, `tracker_search`, `open_repository`,
`search_knowledge`, `propose_tickets` and `offer_plan`.

Every tool runs on the host. The first four only read. `propose_tickets` only records a plan:
the engine shows it with buttons, and nothing reaches the tracker until a mapped person clicks
(`confirm.py`). Its `attachments` are files of the thread's sandbox, read when the call is made
(`share.proposal_attacher`). `offer_plan` only asks the engine to put a "Plan this" button under
the answer.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable

from ..merger.agents import HostTool, HostToolError, _obj
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
MAX_ATTACHMENTS = 10  # per proposal
MAX_ATTACHMENT_TITLE = 200
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # per proposal; each file at most share.MAX_BYTES
MAX_PLAN_SUMMARY = 500
NO_ATTACHMENTS = ("attachments need the sandbox, which is off in this thread: propose without "
                  "them and describe the files in the body")

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


def open_repository_tool(reachable: Callable[[], list[str]],
                         open_repo: Callable[[str], str]) -> HostTool:
    """Check one more repository out, read-only, beside the ones already in the working dir."""
    def find(repo: str) -> str | None:
        wanted = repo.strip().lower()
        return next((r for r in reachable() if r.lower() == wanted), None)

    def check(args: dict) -> str | None:
        if find(args["repo"]) is None:
            return (f"{args['repo']!r} is not reachable: the GitHub App is not installed on it or "
                    "it is archived. Reachable: " + ", ".join(reachable()[:60]))
        return None

    def answer(args: dict) -> dict:
        repo = find(args["repo"])
        return {"path": f"./{repo}", "commit": open_repo(repo)}

    return HostTool(
        "open_repository",
        "Check out one repository read-only at its base branch, so read, grep and glob can see "
        "it under ./owner/name in your working directory. Use it whenever the person names a "
        "repository that is not checked out yet, before you say you cannot see it. `repo` is "
        "owner/name from the reachable list in your instructions. Returns {path, commit}; an "
        "unknown repository fails with the list of reachable ones.",
        _obj(["repo"], {"repo": _S}),
        check=check,
        answer=_safe(answer),
    )


def knowledge_tool(knowledge) -> HostTool:
    """`search_knowledge`: BM25 over the sections of the team knowledge bases (`knowledge.py`)."""
    from .knowledge import SEARCH_DEFAULT, SEARCH_MAX

    def check(args: dict) -> str | None:
        if not args["query"].strip():
            return "query must not be empty"
        limit = args.get("limit", SEARCH_DEFAULT)
        if not 1 <= limit <= SEARCH_MAX:
            return f"limit must be 1-{SEARCH_MAX}"
        base = args.get("base")
        if base is not None and knowledge.find(base) is None:
            return (f"unknown base {base!r}. Bases: "
                    + ", ".join(b.label for b in knowledge.bases()))
        return None

    return HostTool(
        "search_knowledge",
        "Search the team knowledge bases (process, environments, releases, playbooks, decision "
        "records) by words. Returns up to `limit` (default 8, at most 20) sections, best first: "
        "[{base, path, heading, lines: [start, end], score, snippet, url}]. `base` limits the "
        "search to one base by its name. Read the page before you answer and cite its `url`. "
        "Read-only.",
        _obj(["query"], {"query": _S, "base": _S, "limit": {"type": "integer"}}),
        check=check,
        answer=_safe(lambda args: knowledge.search(args["query"].strip(), args.get("base"),
                                                   args.get("limit", SEARCH_DEFAULT))),
    )


def propose_tool(repositories: Iterable[str],
                 attach: Callable[[list[dict]], int] | None = None) -> HostTool:
    """`attach(tickets)` reads the call's sandbox files and records them on each attachment
    (`share.proposal_attacher`); it raises HostToolError with what to fix. Without it, a call
    with attachments is refused."""
    allowed = tuple(repositories)
    attachment = _obj(["path", "title"], {
        "path": {"type": "string", "description": "a file in your sandbox: absolute, or relative "
                 "to the repository root sandbox_run runs in"},
        "title": {"type": "string", "description": "what the file is, up to "
                  f"{MAX_ATTACHMENT_TITLE} characters"},
    })
    ticket = _obj(["key", "title", "body"], {
        "key": _S, "title": _S, "body": _S, "repo": _S, "labels": _STRINGS, "parent": _S,
        "blocked_by": _STRINGS, "attachments": {"type": "array", "items": attachment},
    })

    def check(args: dict) -> str | None:
        problem = validate_proposal(args, allowed)
        if problem is None and attach is None and any(t.get("attachments") for t in args["tickets"]):
            return NO_ATTACHMENTS
        return problem

    def answer(args: dict, _index: int) -> str:
        files = attach(args["tickets"]) if attach is not None else 0
        return ("Proposed. The people in the thread now see it with a Create button."
                + (f" {files} file(s) go with it and are attached on Create." if files else ""))

    return HostTool(
        "propose_tickets",
        "Propose tickets for the people in this thread. Nothing is created now: they see the "
        "plan with a Create button. `key` is a short id you choose; `parent` and `blocked_by` "
        f"name other keys. At most {MAX_TICKETS} tickets, titles up to {MAX_TITLE} characters, "
        f"bodies up to {MAX_BODY}. `attachments` carries files you drafted in the sandbox to "
        f"the ticket: at most {MAX_ATTACHMENTS} files and "
        f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB per proposal, 25 MB each, under a "
        "repository clone or /tmp; .git, credential files and files holding a credential are "
        "refused. For a directory, pack it first (tar czf /tmp/draft.tgz -C <dir> .) and attach "
        "the archive. Call it once, as your last step.",
        _obj(["summary", "tickets"], {"summary": _S, "tickets": {"type": "array", "items": ticket}}),
        terminal=True,
        check=check,
        answer=answer,
    )


def offer_plan_tool(notes) -> HostTool:
    """`offer_plan(summary)`: the answer of this turn gets a "Plan this" button. `notes` is the
    thread's `memory.TurnNotes`; the engine reads `notes.plan` after the turn."""

    def check(args: dict) -> str | None:
        if not args["summary"].strip() or len(args["summary"]) > MAX_PLAN_SUMMARY:
            return f"summary must be 1-{MAX_PLAN_SUMMARY} characters"
        return None

    def answer(args: dict, _index: int) -> str:
        if notes.plan is not None:
            raise HostToolError("already offered in this answer")
        notes.plan = args["summary"].strip()
        return "Offered: your answer ends with a Plan this button. Keep the answer short."

    return HostTool(
        "offer_plan",
        "Offer to hand this thread to the planner, for a request that needs a multi-step plan "
        "(several tickets, a design to settle first). Your answer gets a Plan this button; a "
        "click starts the planner with this thread as context. `summary`: one sentence, what to "
        "plan. Write a short answer that says why a plan fits.",
        _obj(["summary"], {"summary": _S}),
        check=check,
        answer=answer,
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
            lower = label.lower()
            if lower.startswith("repo:"):
                return f"{where}.labels: use `repo` instead of a repo: label"
            # component: and runtime: labels are valid for monorepo dispatch.
        for j, a in enumerate(t.get("attachments") or []):
            if not a["path"].strip() or "\x00" in a["path"] or "\n" in a["path"]:
                return f"{where}.attachments[{j}].path must be one file path"
            if not a["title"].strip() or len(a["title"]) > MAX_ATTACHMENT_TITLE:
                return f"{where}.attachments[{j}].title must be 1-{MAX_ATTACHMENT_TITLE} characters"
        by_key[t["key"]] = t
    if sum(len(t.get("attachments") or []) for t in tickets) > MAX_ATTACHMENTS:
        return (f"attach at most {MAX_ATTACHMENTS} files per proposal: pack a directory or "
                "several drafts into one archive (tar czf)")
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
