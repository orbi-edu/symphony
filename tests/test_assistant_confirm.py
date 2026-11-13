"""Confirmation of proposed tickets: mapped people only, created once, audited."""

from __future__ import annotations

from symphony.admin_commands import AdminStore
from symphony.assistant import Action, Ref
from symphony.assistant.confirm import (
    AUDIT_ACTION,
    ONLY_MAPPED,
    Confirmer,
    Policy,
    ProposalStore,
)
from symphony.assistant.tools import MAX_TICKETS, creation_order, validate_proposal
from symphony.trackers.memory import MemoryTracker

PROPOSAL = {
    "summary": "Split the importer",
    "tickets": [
        {"key": "tests", "title": "Add importer tests", "body": "Cover it.", "parent": "epic",
         "blocked_by": ["lexer"]},
        {"key": "epic", "title": "Split the importer", "body": "The parent.", "repo": "acme/app",
         "labels": ["refactor"]},
        {"key": "lexer", "title": "Extract the lexer", "body": "Move it.", "parent": "epic"},
        {"key": "docs", "title": "Document the importer", "body": "Write it.", "blocked_by": ["epic"]},
    ],
}


class Out:
    name = "slack"
    message_limit = 40_000

    def __init__(self) -> None:
        self.acks: list[str | None] = []
        self.updates: list[tuple[Ref, str, list]] = []

    def ack_action(self, action, text=None):
        self.acks.append(text)

    def update(self, ref, text, buttons=()):
        self.updates.append((ref, text, list(buttons)))


def setup(redis_client):
    tracker = MemoryTracker()
    store = ProposalStore(redis_client, "t")
    admin = AdminStore(redis_client, "t")
    policy = Policy(notify_users={"slack": {"alice": "U1"}}, ticket_state="Backlog",
                    start_state="Todo", ticket_labels=("from-chat",), required_labels=("symphony",))
    confirmer = Confirmer(
        store, tracker=lambda: tracker, policy=lambda: policy,
        audit=lambda actor, target, reason: admin.audit_event(
            actor=actor, action=AUDIT_ACTION, target=target, reason=reason),
    )
    pid = store.save({**PROPOSAL, "channel": "C1", "thread": "t1"})
    return tracker, store, admin, confirmer, pid


def click(pid, choice, user="U1"):
    return Action("slack", "C1", "t1", "m7", user, f"proposal:{pid}:{choice}")


def by_title(tracker):
    return {i.title: i for i in tracker._issues.values()}


def test_an_unmapped_click_is_refused(redis_client):
    tracker, store, admin, confirmer, pid = setup(redis_client)
    out = Out()
    assert confirmer.handle(click(pid, "create", user="U2"), out) == "unmapped"
    assert out.acks == [ONLY_MAPPED]
    assert out.updates == []
    assert tracker._issues == {}
    assert store.get(pid)["status"] == "open"


def test_a_mapped_click_creates_the_tickets_once_with_links(redis_client):
    tracker, store, admin, confirmer, pid = setup(redis_client)
    out = Out()
    assert confirmer.handle(click(pid, "create"), out) == "created"
    assert out.acks == [None]  # a silent ack, first
    issues = by_title(tracker)
    assert len(issues) == 4
    epic, lexer, tests, docs = (issues[t] for t in (
        "Split the importer", "Extract the lexer", "Add importer tests", "Document the importer"))
    # the parent and the children
    assert tracker.parents == {lexer.id: epic.id, tests.id: epic.id}
    # every blocked_by is a tracker relation, between siblings and across parents
    assert tracker.relations == [(lexer.id, tests.id), (epic.id, docs.id)]
    assert "Blocked by" not in docs.description and tracker.comments == {}
    # "Create" leaves every ticket in ticket_state, without the required label
    assert {i.state for i in issues.values()} == {"Backlog"}
    assert "repo:acme/app" in epic.labels and "from-chat" in epic.labels and "refactor" in epic.labels
    assert "repo:acme/app" in lexer.labels  # children inherit the repository
    assert not any("symphony" in i.labels for i in issues.values())
    # the message lists the identifiers and loses its buttons
    ref, text, buttons = out.updates[-1]
    assert ref == Ref("C1", "m7") and buttons == []
    assert all(i.identifier in text for i in issues.values()) and "Created by alice" in text
    # audited, with the person's GitHub login
    [event] = [e for e in admin.audit()["items"] if e["action"] == AUDIT_ACTION]
    assert event["actor"] == "alice" and event["target"] == f"proposal:{pid}"
    assert all(i.identifier in event["reason"] for i in issues.values())

    # a double click creates nothing more
    again = Out()
    assert confirmer.handle(click(pid, "create"), again) == "already_created"
    assert len(tracker._issues) == 4
    assert again.acks == ["This proposal is already created."]


def test_create_and_start_adds_the_required_labels(redis_client):
    tracker, store, admin, confirmer, pid = setup(redis_client)
    assert confirmer.handle(click(pid, "start"), Out()) == "created"
    issues = by_title(tracker)
    assert all("symphony" in i.labels for i in issues.values())
    assert {i.state for i in issues.values()} == {"Todo"}
    assert store.get(pid)["started"] is True


def test_a_click_while_another_creates_does_nothing(redis_client):
    tracker, store, admin, confirmer, pid = setup(redis_client)
    assert store.claim(pid)
    out = Out()
    assert confirmer.handle(click(pid, "create"), out) == "busy"
    assert tracker._issues == {}


def test_a_retry_after_a_failure_does_not_duplicate(redis_client):
    tracker, store, admin, confirmer, pid = setup(redis_client)
    calls = {"n": 0}
    real = tracker.create_sub_issues

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("tracker hiccup")
        return real(*args, **kwargs)

    tracker.create_sub_issues = flaky
    out = Out()
    assert confirmer.handle(click(pid, "create"), out) == "failed"
    assert "Click again to retry" in out.updates[-1][1] and out.updates[-1][2]  # buttons back
    assert confirmer.handle(click(pid, "create"), out) == "created"
    assert len(tracker._issues) == 4


def test_a_refused_link_is_said_on_the_ticket(redis_client):
    from symphony.trackers.base import TrackerInputError

    tracker, store, admin, confirmer, pid = setup(redis_client)

    def refuse(issue, blocker):
        raise TrackerInputError("dependencies are off for this repository")

    tracker.add_blocked_by = refuse
    assert confirmer.handle(click(pid, "create"), Out()) == "created"
    issues = by_title(tracker)
    epic, docs = issues["Split the importer"], issues["Document the importer"]
    [note] = tracker.comments[docs.id]
    assert note["body"].startswith(f"Blocked by {epic.identifier}.")
    assert "dependencies are off" in note["body"]
    assert tracker.relations == []


def test_dismiss_removes_the_buttons(redis_client):
    tracker, store, admin, confirmer, pid = setup(redis_client)
    out = Out()
    assert confirmer.handle(click(pid, "dismiss"), out) == "dismissed"
    assert out.updates[-1][2] == [] and tracker._issues == {}
    assert confirmer.handle(click(pid, "create"), Out()) == "already_dismissed"


def test_proposal_validation():
    ok = {"summary": "s", "tickets": [{"key": "a", "title": "t", "body": "b"}]}
    assert validate_proposal(ok) is None
    many = {"summary": "s", "tickets": [{"key": f"k{i}", "title": "t", "body": ""}
                                        for i in range(MAX_TICKETS + 1)]}
    assert "1-15" in validate_proposal(many)
    long_title = {"summary": "s", "tickets": [{"key": "a", "title": "x" * 121, "body": ""}]}
    assert "title" in validate_proposal(long_title)
    long_body = {"summary": "s", "tickets": [{"key": "a", "title": "t", "body": "x" * 20_001}]}
    assert "body" in validate_proposal(long_body)
    cycle = {"summary": "s", "tickets": [
        {"key": "a", "title": "t", "body": "", "blocked_by": ["b"]},
        {"key": "b", "title": "t", "body": "", "blocked_by": ["a"]}]}
    assert "cycle" in validate_proposal(cycle)
    unknown = {"summary": "s", "tickets": [{"key": "a", "title": "t", "body": "", "parent": "z"}]}
    assert "parent" in validate_proposal(unknown)
    other_repo = {"summary": "s", "tickets": [{"key": "a", "title": "t", "body": "", "repo": "x/y"}]}
    assert "acme/app" in validate_proposal(other_repo, ["acme/app"])
    assert [t["key"] for t in creation_order(PROPOSAL["tickets"])] == ["epic", "lexer", "tests", "docs"]
