"""In-memory tracker for tests and the local stack (like the memory adapter in the Elixir
reference). Writes land in plain attributes tests can read: `comments`, `relations`,
`evidence`."""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import replace

from ..repos import PREFIX as REPO_LABEL_PREFIX
from ..repos import resolve_target_repos
from .base import (
    WORKPAD_HEADING,
    Issue,
    SubIssue,
    SubIssueRecords,
    ToolSpec,
    TrackerInputError,
    TrackerUnavailable,
    classify_state,
    make_dispatchable,
    pick_person,
)


class MemoryTracker:
    kind = "memory"

    def __init__(
        self,
        active_states: Sequence[str] = ("Todo", "In Progress", "Rework"),
        terminal_states: Sequence[str] = ("Closed", "Cancelled", "Canceled", "Duplicate", "Done"),
        # Defaults to a repository so issues built without a `repo:` label stay
        # dispatchable — otherwise every test fixture would need one.
        default_repo: str | None = "memory/repo",
    ):
        self.active_states = list(active_states)
        self.terminal_states = list(terminal_states)
        self.default_repo = default_repo
        self._issues: dict[str, Issue] = {}
        self._blockers: dict[str, bool] = {}
        self.mutations: list[dict] = []
        self.raise_unavailable = False
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.comments: dict[str, list[dict]] = {}  # issue id -> [{id, body, parent}]
        self.people: dict[str, list[dict]] = {}  # issue id -> [{name, email, mention}], author first
        self.relations: list[tuple[str, str]] = []  # (blocker id, blocked id)
        self.evidence: dict[str, list[dict]] = {}  # issue id -> [{title, url, kind}]
        self.labels: set[str] = set()  # labels the board has, beyond those on issues
        self.parents: dict[str, str] = {}  # child id -> parent id
        self._seq = itertools.count(1)

    # -- test helpers ----------------------------------------------------
    def add_issue(self, issue_id: str, identifier: str, state: str = "Todo", **kw) -> Issue:
        blocker = kw.pop("has_nonterminal_blocker", False)
        state_type = classify_state(state, self.active_states, self.terminal_states)
        target_repos, repo_reason = resolve_target_repos(kw.get("labels", ()), self.default_repo)
        target_repo = target_repos[0] if target_repos else None
        issue = Issue(
            id=issue_id,
            identifier=identifier,
            title=kw.pop("title", f"title {identifier}"),
            description=kw.pop("description", "desc"),
            state=state,
            state_type=state_type,
            target_repo=target_repo,
            target_repos=target_repos,
            undispatchable_reason=repo_reason,
            dispatchable=make_dispatchable(state_type, blocker, target_repo),
            **kw,
        )
        self._issues[issue_id] = issue
        self._blockers[issue_id] = blocker
        return issue

    def set_state(self, issue_id: str, state: str, updated_at: str | None = None) -> None:
        issue = self._issues[issue_id]
        state_type = classify_state(state, self.active_states, self.terminal_states)
        self._issues[issue_id] = replace(
            issue,
            state=state,
            state_type=state_type,
            dispatchable=make_dispatchable(
                state_type, self._blockers.get(issue_id, False), issue.target_repo
            ),
            updated_at=updated_at if updated_at is not None else issue.updated_at,
        )

    def remove_issue(self, issue_id: str) -> None:
        self._issues.pop(issue_id, None)

    def _find(self, ref: str) -> Issue | None:
        """By id or identifier (case-insensitive)."""
        if ref in self._issues:
            return self._issues[ref]
        folded = ref.casefold()
        return next((i for i in self._issues.values() if i.identifier.casefold() == folded), None)

    def _require(self, ref: str) -> Issue:
        issue = self._find(ref)
        if issue is None:
            raise TrackerInputError(f"no ticket {ref!r}")
        return issue

    def _new_issue(self, title: str, body: str, labels: Sequence[str], state: str) -> Issue:
        n = next(self._seq)
        while self._find(f"MEM-{n}") is not None or f"mem-{n}" in self._issues:
            n = next(self._seq)
        return self.add_issue(f"mem-{n}", f"MEM-{n}", state=state, title=title, description=body,
                              labels=tuple(dict.fromkeys(label.lower() for label in labels)))

    @staticmethod
    def _url(issue: Issue) -> str:
        return issue.url or f"memory://issues/{issue.identifier}"

    # -- TrackerAdapter --------------------------------------------------
    def fetch_issues_by_states(self, states: Sequence[str]) -> list[Issue]:
        self.fetch_calls.append(("by_states", tuple(states)))
        if self.raise_unavailable:
            raise TrackerUnavailable()
        return [i for i in self._issues.values() if i.state in states]

    def fetch_issues_by_ids(self, ids: Sequence[str]) -> list[Issue]:
        self.fetch_calls.append(("by_ids", tuple(ids)))
        if self.raise_unavailable:
            raise TrackerUnavailable()
        return [self._issues[i] for i in ids if i in self._issues]

    def agent_tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name="linear_graphql", description="Tracker GraphQL (host-side)")]

    def execute_agent_tool(self, name: str, args: dict) -> dict:
        self.mutations.append({"name": name, "args": args})
        return {"ok": True, "echo": args, "seq": len(self.mutations)}

    def secret_environment_names(self) -> list[str]:
        return ["MEMORY_TRACKER_FAKE_KEY"]

    def move(self, issue: str, state_name: str) -> bool:
        found = self._find(issue)
        if found is None or found.state == state_name:
            return False
        self.set_state(found.id, state_name)
        return True

    def comment(self, issue: str, body: str, *, parent: str | None = None) -> str:
        found = self._require(issue)
        comment_id = f"comment-{next(self._seq)}"
        entry = {"id": comment_id, "body": body, **({"parent": parent} if parent else {})}
        self.comments.setdefault(found.id, []).append(entry)
        return comment_id

    def person(self, issue: str, hint: str | None = None) -> dict | None:
        found = self._require(issue)
        return pick_person(self.people.get(found.id, []), hint)

    def assignee(self, issue: str) -> dict | None:
        found = self._require(issue)
        return pick_person([{"name": found.assignee, "email": found.assignee_email}]
                           if found.assignee or found.assignee_email else [], None)

    def upsert_workpad(self, issue: str, body: str) -> str:
        found = self._require(issue)
        for entry in self.comments.get(found.id, []):
            if entry["body"].startswith(WORKPAD_HEADING):
                entry["body"] = body
                return entry["id"]
        return self.comment(found.id, body)

    def fetch_issue_workpad(self, identifier: str) -> str:
        found = self._find(identifier)
        if found is None:
            return ""
        return "\n\n".join(c["body"] for c in self.comments.get(found.id, []))

    def create_sub_issues(
        self, parent: str, items: Sequence[SubIssue], *, labels: Sequence[str],
        records: SubIssueRecords,
    ) -> list[dict]:
        ticket = self._require(parent)
        inherit = {name.casefold() for name in labels}
        inherited = [
            label for label in ticket.labels
            if label.startswith(REPO_LABEL_PREFIX) or label.casefold() in inherit
        ]
        made: dict[str, dict] = {}
        for item in items:
            record = records.issue(item.key)
            if record is None:
                child = self._new_issue(item.title, item.description, [*inherited, *item.labels],
                                        self.active_states[0])
                self.parents[child.id] = ticket.id
                record = {"id": child.id, "identifier": child.identifier, "url": self._url(child)}
                records.save_issue(item.key, record)
            made[item.key] = record
        for item in items:
            for blocker in item.blocked_by:
                if records.relation(blocker, item.key):
                    continue
                blocker_id = (made.get(blocker) or records.issue(blocker))["id"]
                blocked_id = made[item.key]["id"]
                self.relations.append((blocker_id, blocked_id))
                self._block(blocked_id, self._issues[blocker_id])
                records.save_relation(blocker, item.key)
        return [{"key": item.key, "identifier": made[item.key]["identifier"],
                 "url": made[item.key]["url"]} for item in items]

    def _block(self, blocked_id: str, blocker: Issue) -> None:
        issue = self._issues[blocked_id]
        self._blockers[blocked_id] = True
        ref = {"id": blocker.id, "identifier": blocker.identifier, "state": blocker.state}
        self._issues[blocked_id] = replace(issue, blocked_by=(*issue.blocked_by, ref),
                                           dispatchable=False)

    def attach_evidence(
        self, issue: str, *, title: str, kind: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        found = self._require(issue)
        entry = {"title": title, "url": f"memory://evidence/{found.identifier}/{filename}",
                 "kind": kind}
        self.evidence.setdefault(found.id, []).append(entry)
        return entry

    def create_issue(
        self, title: str, body: str, *, labels: Sequence[str], state: str | None = None,
        repo: str | None = None,
    ) -> dict:
        issue = self._new_issue(title, body, labels, state or self.active_states[0])
        return {"identifier": issue.identifier, "url": self._url(issue)}

    def ensure_labels(self, names: Sequence[str]) -> None:
        known = {label.casefold() for label in self.labels}
        self.labels.update(n for n in names if n.strip() and n.casefold() not in known)

    def state_of(self, identifier: str) -> dict | None:
        found = self._find(identifier)
        return {"name": found.state, "type": found.state_type} if found else None

    def attachments(self, identifier: str) -> list[dict]:
        found = self._find(identifier)
        return list(self.evidence.get(found.id, [])) if found else []

    def identifier_in(self, text: str, repo: str | None = None) -> str | None:
        """The first known identifier in `text`, as a whole word."""
        hits = []
        for issue in self._issues.values():
            m = re.search(rf"(?<![A-Za-z0-9]){re.escape(issue.identifier)}(?![A-Za-z0-9])",
                          text or "", re.IGNORECASE)
            if m:
                hits.append((m.start(), issue.identifier))
        return min(hits)[1] if hits else None

    def add_blocked_by(self, issue: str, blocker: str) -> bool:
        blocked, blocking = self._require(issue), self._require(blocker)
        if blocked.id == blocking.id:
            raise TrackerInputError("a ticket cannot block itself")
        if (blocking.id, blocked.id) in self.relations:
            return False
        self.relations.append((blocking.id, blocked.id))
        self._block(blocked.id, blocking)
        return True

    def search(self, query: str, limit: int = 10) -> list[dict]:
        if self.raise_unavailable:
            raise TrackerUnavailable()
        folded = query.casefold()
        hits = [
            issue for issue in self._issues.values()
            if folded in " ".join((issue.identifier, issue.title or "", issue.description or "")).casefold()
        ]
        # newest first: tickets created later have a higher sequence in insertion order
        return [{"identifier": i.identifier, "title": i.title or "", "state": i.state,
                 "url": self._url(i)} for i in reversed(hits)][:limit]
