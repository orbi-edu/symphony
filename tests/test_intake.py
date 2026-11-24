"""A ticket addressed to Symphony is told whether it is queued and, if not, what is missing."""

from dataclasses import replace

from tests.test_dispatch import FakeEffects, cfg

from symphony import intake
from symphony.orchestrator import tick
from symphony.trackers.memory import MemoryTracker

LATER = "2030-01-01T00:00:00Z"


def board(**over):
    return cfg(required_labels=("symphony",), max_concurrent_agents=1, **over)


def run(kv, clock, tracker, c, reasons=()):
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1", wake_reasons=reasons)
    return fx


def test_a_backlog_ticket_is_told_to_move_and_the_channel_hears_it_once(kv, clock, tracker):
    c = board()
    run(kv, clock, tracker, c)  # first look: the watermark
    tracker.add_issue("b1", "ENG-7", state="Backlog", labels=("symphony", "repo:acme/app"),
                      updated_at=LATER, url="https://t/ENG-7", assignee="Alice",
                      assignee_email="alice@example.com")
    fx = run(kv, clock, tracker, c, reasons=["linear:create:ENG-7"])
    [(issue_id, comment)] = fx.reported
    assert issue_id == "b1" and "not queued" in comment and "**Backlog**" in comment and "Todo" in comment
    [(event, text, link)] = fx.notified
    assert event == "not_queued" and "Who acts" in text and link == "https://t/ENG-7"
    assert fx.direct == [("not_queued", ("alice@example.com", "Alice"))]  # the assignee's DM
    # same verdict on the next pass: silence
    fx = run(kv, clock, tracker, c, reasons=["linear:update:ENG-7"])
    assert getattr(fx, "reported", []) == [] and getattr(fx, "notified", []) == []


def test_a_missing_label_is_named(kv, clock, tracker):
    c = board()
    run(kv, clock, tracker, c)
    tracker.add_issue("t1", "ENG-8", labels=("repo:acme/app",), updated_at=LATER)
    fx = run(kv, clock, tracker, c)
    assert fx.spawned == []
    [(_, comment)] = fx.reported
    assert "lacks the `symphony` label" in comment


def test_a_missing_repository_is_named(kv, clock):
    tracker = MemoryTracker(default_repo=None)
    c = board()
    run(kv, clock, tracker, c)
    tracker.add_issue("t1", "ENG-9", labels=("symphony",), updated_at=LATER)
    fx = run(kv, clock, tracker, c)
    [(_, comment)] = fx.reported
    assert "names no repository" in comment


def test_a_ticket_waiting_for_a_slot_is_told_it_is_queued_and_nobody_is_pinged(kv, clock, tracker):
    c = board()
    run(kv, clock, tracker, c)
    for n in (1, 2):
        tracker.add_issue(f"t{n}", f"ENG-{n}", labels=("symphony", "repo:acme/app"), updated_at=LATER,
                          created_at=f"2029-01-0{n}T00:00:00Z")
    fx = run(kv, clock, tracker, c)
    assert fx.spawned == [("t1", 1)]  # the one started gets no intake comment
    [(issue_id, comment)] = fx.reported
    assert issue_id == "t2" and "is queued" in comment
    assert getattr(fx, "notified", []) == []


def test_tickets_untouched_since_symphony_first_looked_are_left_alone(kv, clock, tracker):
    # the fake clock starts at the epoch: Symphony first looks at 00:16:40
    tracker.add_issue("b1", "ENG-7", state="Backlog", labels=("symphony",), updated_at="1970-01-01T00:00:00Z")
    fx = run(kv, clock, tracker, board())
    assert getattr(fx, "reported", []) == []


def test_tickets_not_addressed_to_symphony_are_ignored(kv, clock, tracker):
    c = board()
    run(kv, clock, tracker, c)
    tracker.add_issue("b1", "ENG-7", state="Backlog", labels=("bug",), updated_at=LATER)
    fx = run(kv, clock, tracker, c, reasons=["linear:create:ENG-7"])
    assert getattr(fx, "reported", []) == []


def test_the_backlog_is_read_on_a_tracker_wake_or_on_the_interval(kv, clock, tracker):
    c = board()
    run(kv, clock, tracker, c)
    tracker.add_issue("b1", "ENG-7", state="Backlog", labels=("symphony", "repo:acme/app"), updated_at=LATER)
    clock.advance(1_000)
    assert getattr(run(kv, clock, tracker, c), "reported", []) == []  # not due yet
    clock.advance(intake.INTERVAL_MS)
    assert len(run(kv, clock, tracker, c).reported) == 1


def test_no_intake_states_means_no_backlog_read(kv, clock, tracker):
    c = replace(board(), intake_states=())
    run(kv, clock, tracker, c)
    tracker.add_issue("b1", "ENG-7", state="Backlog", labels=("symphony",), updated_at=LATER)
    fx = run(kv, clock, tracker, c, reasons=["linear:create:ENG-7"])
    assert getattr(fx, "reported", []) == []
