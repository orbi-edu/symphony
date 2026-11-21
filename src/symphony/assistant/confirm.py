"""Proposals and their confirmation: nothing reaches the tracker until a mapped person clicks.

A planner's `propose_tickets` call is stored at `sym:<ns>:assistant:proposal:<id>` (7 days) and
shown with three buttons: "Create", "Create and start" (adds the tracker's required labels, so
the daemon picks the tickets up) and "Dismiss". Only a person mapped in `notify.users` may
click them; the audit actor is that person's GitHub login.

Creation is idempotent per proposal: a claim key lets one click work at a time, and every
created ticket is recorded by key (`:records`), so a double click, or a retry after a
failure, never creates a ticket twice.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from ..trackers.base import SubIssue, TrackerInputError
from . import Action, Button, Outbound, Ref
from .tools import creation_order

PROPOSAL_TTL_S = 7 * 24 * 3600
CLAIM_TTL_S = 600
ONLY_MAPPED = "Only people mapped in notify.users can create tickets."
CHOICES = ("create", "start", "dismiss")
AUDIT_ACTION = "assistant.tickets_created"


def mapped_login(notify_users: dict, transport: str, user_id: str) -> str | None:
    """The GitHub login `notify.users.<transport>` maps to this chat user, if any."""
    for login, mapped in (notify_users.get(transport) or {}).items():
        if mapped == user_id:
            return login
    return None


def parse_action(action_id: str) -> tuple[str, str] | None:
    """`proposal:<id>:<choice>` → (id, choice)."""
    parts = action_id.split(":")
    if len(parts) != 3 or parts[0] != "proposal" or parts[2] not in CHOICES or not parts[1]:
        return None
    return parts[1], parts[2]


def buttons(pid: str) -> list[Button]:
    return [
        Button(f"proposal:{pid}:create", "Create", "primary"),
        Button(f"proposal:{pid}:start", "Create and start"),
        Button(f"proposal:{pid}:dismiss", "Dismiss", "danger"),
    ]


def render(proposal: dict) -> str:
    lines = [f"Proposed tickets: {proposal['summary'].strip()}", ""]
    titles = {t["key"]: t["title"] for t in proposal["tickets"]}
    for n, t in enumerate(proposal["tickets"], 1):
        extra = []
        if t.get("repo"):
            extra.append(t["repo"])
        if t.get("parent"):
            extra.append(f"part of {titles[t['parent']]!r}")
        if t.get("blocked_by"):
            extra.append("after " + ", ".join(repr(titles[b]) for b in t["blocked_by"]))
        lines.append(f"{n}. {t['title']}" + (f" ({'; '.join(extra)})" if extra else ""))
    lines += ["", "Nothing is created until someone mapped in notify.users clicks Create."]
    return "\n".join(lines)


class _Records:
    """`SubIssueRecords` for one proposal, in Redis next to it."""

    def __init__(self, client, key: str) -> None:
        self.client, self.key = client, key

    def issue(self, key: str) -> dict | None:
        raw = self.client.hget(self.key, f"issue:{key}")
        return json.loads(raw) if raw else None

    def save_issue(self, key: str, record: dict) -> None:
        self.client.hset(self.key, f"issue:{key}", json.dumps(record))
        self.client.expire(self.key, PROPOSAL_TTL_S)

    def relation(self, blocker: str, blocked: str) -> bool:
        return bool(self.client.hexists(self.key, f"rel:{blocker}>{blocked}"))

    def save_relation(self, blocker: str, blocked: str) -> None:
        self.client.hset(self.key, f"rel:{blocker}>{blocked}", "1")
        self.client.expire(self.key, PROPOSAL_TTL_S)


class ProposalStore:
    def __init__(self, client, namespace: str, kind: str = "proposal") -> None:
        """`kind` names the key space: `proposal` (tickets) or `fact` (team facts)."""
        self.client = client
        self.prefix = f"sym:{namespace}:assistant:{kind}:"

    def save(self, doc: dict) -> str:
        pid = secrets.token_hex(8)
        self.client.set(self.prefix + pid, json.dumps({**doc, "id": pid, "status": "open"}),
                        ex=PROPOSAL_TTL_S)
        return pid

    def get(self, pid: str) -> dict | None:
        raw = self.client.get(self.prefix + pid)
        return json.loads(raw) if raw else None

    def update(self, pid: str, **fields) -> dict | None:
        doc = self.get(pid)
        if doc is None:
            return None
        doc.update(fields)
        self.client.set(self.prefix + pid, json.dumps(doc), keepttl=True)
        return doc

    def claim(self, pid: str) -> bool:
        return bool(self.client.set(self.prefix + pid + ":claim", "1", nx=True, ex=CLAIM_TTL_S))

    def release(self, pid: str) -> None:
        self.client.delete(self.prefix + pid + ":claim")

    def records(self, pid: str) -> _Records:
        return _Records(self.client, self.prefix + pid + ":records")


@dataclass
class Policy:
    """What creation needs from WORKFLOW.md, read at each click."""

    notify_users: dict
    ticket_state: str
    start_state: str
    ticket_labels: tuple[str, ...]
    required_labels: tuple[str, ...]


class Confirmer:
    def __init__(
        self,
        store: ProposalStore,
        *,
        tracker: Callable[[], object],
        policy: Callable[[], Policy],
        audit: Callable[[str, str, str], None],
        logger=None,
    ) -> None:
        self.store = store
        self.tracker = tracker
        self.policy = policy
        self.audit = audit  # (actor, target, reason)
        self.log = logger

    def handle(self, action: Action, out: Outbound) -> str:
        """Answers one button click; returns what happened (for logs and tests)."""
        parsed = parse_action(action.action_id)
        if parsed is None:
            out.ack_action(action)
            return "unknown_action"
        pid, choice = parsed
        policy = self.policy()
        login = mapped_login(policy.notify_users, action.transport, action.user_id)
        if login is None:
            out.ack_action(action, ONLY_MAPPED)
            return "unmapped"
        proposal = self.store.get(pid)
        if proposal is None:
            out.ack_action(action, "This proposal expired. Ask the planner again.")
            return "expired"
        if proposal["status"] in ("created", "dismissed"):
            out.ack_action(action, f"This proposal is already {proposal['status']}.")
            return "already_" + proposal["status"]
        if not self.store.claim(pid):
            out.ack_action(action, "These tickets are being created.")
            return "busy"
        out.ack_action(action)  # at once: Discord allows 3 s
        try:
            if choice == "dismiss":
                self.store.update(pid, status="dismissed", by=login)
                out.update(_ref(action), f"Dismissed by {login}. No tickets were created.", ())
                return "dismissed"
            return self._create(pid, proposal, choice == "start", login, policy, action, out)
        finally:
            self.store.release(pid)

    def _create(self, pid: str, proposal: dict, start: bool, login: str, policy: Policy,
                action: Action, out: Outbound) -> str:
        out.update(_ref(action), "Creating the tickets…", ())
        try:
            made = self.create(pid, proposal, start=start, policy=policy)
        except Exception as exc:  # noqa: BLE001 — a tracker failure: the people may retry
            if self.log is not None:
                self.log.warning("assistant_tickets_failed", proposal=pid,
                                 error=type(exc).__name__, detail=str(exc)[:200])
            out.update(_ref(action), render(proposal) + "\n\nCreating the tickets failed: "
                       f"{str(exc)[:200] or type(exc).__name__}. Click again to retry.", buttons(pid))
            return "failed"
        identifiers = [m["identifier"] for m in made]
        self.store.update(pid, status="created", by=login, created=made, started=start)
        lines = [f"Created by {login}" + (" and started" if start else "") + ":"]
        lines += [f"- {m['identifier']}: {m['title']} {m['url']}".rstrip() for m in made]
        labels = ", ".join(f"`{label}`" for label in policy.required_labels)
        # Without "start" the tickets get no required label and the waiting state: they are
        # queued only when the board needs neither.
        if start or (policy.ticket_state == policy.start_state and not policy.required_labels):
            lines.append("Queued: an agent starts each one at the next free slot; a ticket blocked by "
                         "another starts once that one is done.")
        else:
            lines.append(f"Not queued: they wait in {policy.ticket_state}"
                         + (f" without the {labels} label" if labels else "")
                         + f". To queue one, move it to {policy.start_state}"
                         + (f" and add {labels}." if labels else "."))
        out.update(_ref(action), "\n".join(lines), ())
        try:
            self.audit(login, f"proposal:{pid}", ", ".join(identifiers))
        except Exception as exc:  # noqa: BLE001 — the tickets exist; the log keeps the trace
            if self.log is not None:
                self.log.warning("assistant_audit_failed", proposal=pid, error=type(exc).__name__)
        return "created"

    def create(self, pid: str, proposal: dict, *, start: bool, policy: Policy) -> list[dict]:
        """Creates the proposal's tickets once each, in creation order, and links every
        `blocked_by` as a tracker relation. Returns `[{key, identifier, url, title}]` in the
        proposal's order."""
        tracker = self.tracker()
        records = self.store.records(pid)
        state = policy.start_state if start else policy.ticket_state
        base = list(policy.ticket_labels) + (list(policy.required_labels) if start else [])
        for t in creation_order(proposal["tickets"]):
            if records.issue(t["key"]) is None:
                self._create_one(tracker, records, proposal, t, state=state, base=base)
            identifier = records.issue(t["key"])["identifier"]
            if t.get("parent") and not start:
                # A sub-ticket starts in the first active state; without "start" it waits.
                tracker.move(identifier, state)
            for blocker in t.get("blocked_by") or []:
                if records.relation(blocker, t["key"]):
                    continue
                blocker_id = records.issue(blocker)["identifier"]
                try:
                    tracker.add_blocked_by(identifier, blocker_id)
                except TrackerInputError as exc:
                    # The tracker refused the link: say it on the ticket instead.
                    tracker.comment(identifier, f"Blocked by {blocker_id}. The tracker did not "
                                                f"record the link: {str(exc)[:200]}")
                records.save_relation(blocker, t["key"])
        return [{"key": t["key"], "title": t["title"], "identifier": records.issue(t["key"])["identifier"],
                 "url": records.issue(t["key"]).get("url") or ""} for t in proposal["tickets"]]

    @staticmethod
    def _create_one(tracker, records: _Records, proposal: dict, t: dict, *, state: str,
                    base: list[str]) -> None:
        parent = t.get("parent")
        if parent is None:
            repo = t.get("repo") or proposal.get("default_repo")
            labels = [*base, *(t.get("labels") or []), *([f"repo:{repo}"] if repo else [])]
            record = tracker.create_issue(t["title"], t["body"], labels=list(dict.fromkeys(labels)),
                                          state=state, repo=repo)
            records.save_issue(t["key"], record)
            return
        item = SubIssue(key=t["key"], title=t["title"], description=t["body"],
                        labels=tuple(t.get("labels") or ()))
        parent_id = records.issue(parent)["identifier"]
        try:
            tracker.create_sub_issues(parent_id, [item], labels=base, records=records)
        except TrackerInputError:
            # A label the tracker does not have: create the child without its own labels.
            item = SubIssue(key=item.key, title=item.title, description=item.description)
            tracker.create_sub_issues(parent_id, [item], labels=base, records=records)


def _ref(action: Action) -> Ref:
    return Ref(action.channel, action.message_id)
