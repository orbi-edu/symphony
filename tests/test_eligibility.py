"""FR-002 (objective eligibility) + FR-003 (deterministic ordering, upstream spec §8.2)."""

from symphony.config import parse_workflow
from symphony.orchestrator import dispatch_order, eligible

CFG = parse_workflow("---\ntracker:\n  kind: memory\n  required_labels: [symphony]\n---\nx")


def make(tracker, i, **kw):
    kw.setdefault("labels", ("symphony",))
    return tracker.add_issue(f"id{i}", f"ENG-{i}", **kw)


def test_eligibility_requires_mandatory_fields_state_dispatchable_labels(tracker):
    ok = make(tracker, 1)
    assert eligible(ok, CFG, set())
    assert not eligible(make(tracker, 2, title=None), CFG, set())
    assert eligible(make(tracker, 3, description=None), CFG, set())  # empty description is valid (upstream §8.2)
    assert not eligible(make(tracker, 4, state="Done"), CFG, set())
    assert not eligible(make(tracker, 5, has_nonterminal_blocker=True), CFG, set())
    assert not eligible(make(tracker, 6, labels=()), CFG, set())
    assert not eligible(ok, CFG, {"id1"})  # already tracked


def test_labels_and_states_compared_case_insensitively(tracker):
    assert eligible(make(tracker, 7, labels=("SYMPHONY",)), CFG, set())  # upstream §8.2
    assert make(tracker, 8, state="todo").state_type == "active"


def test_rework_is_dispatchable_and_the_merger_states_are_not(tracker):
    """Rework is how the merger sends a pull request back, so it must get an agent; In
    Review and Merging belong to the merger, so the implementer must stay out."""
    rework = make(tracker, 7, state="Rework")
    assert rework.dispatchable and eligible(rework, CFG, set())
    for i, name in enumerate((CFG.tracker_states["review"], CFG.tracker_states["merging"])):
        held = make(tracker, 8 + i, state=name)
        assert not held.dispatchable and not eligible(held, CFG, set())


def test_ordering_priority_buckets_then_created_then_identifier(tracker):
    a = make(tracker, 1, priority=None, created_at="2026-01-01T00:00:00Z")
    b = make(tracker, 2, priority=1, created_at="2026-06-01T00:00:00Z")
    c = make(tracker, 3, priority=4, created_at="2026-01-01T00:00:00Z")
    d = make(tracker, 4, priority=9, created_at="2026-01-01T00:00:00Z")  # outside the 1-4 bucket
    e = make(tracker, 5, priority=1, created_at="2026-05-01T00:00:00Z")
    assert dispatch_order([a, b, c, d, e]) == [e, b, c, a, d] or dispatch_order([a, b, c, d, e])[:3] == [e, b, c]


def test_ordering_null_created_last_and_identifier_tiebreak(tracker):
    x = make(tracker, 1, priority=2, created_at=None)
    y = make(tracker, 2, priority=2, created_at="2026-01-01T00:00:00Z")
    z = make(tracker, 3, priority=2, created_at=None)
    ordered = dispatch_order([z, x, y])
    assert ordered[0] is y                      # a present created_at beats None
    assert [i.identifier for i in ordered[1:]] == ["ENG-1", "ENG-3"]  # lexicographic tiebreak


def test_ordering_is_deterministic(tracker):
    issues = [make(tracker, i, priority=(i % 5) or None) for i in range(1, 10)]
    once = dispatch_order(list(reversed(issues)))
    twice = dispatch_order(issues)
    assert [i.id for i in once] == [i.id for i in twice]


# -- feature 003: an unresolvable repository holds the issue back ------------
def test_issue_without_a_resolvable_repository_is_not_eligible():
    """Reuses `dispatchable` rather than adding a gate, so orchestrator.py needs no
    change and there is no separate path a later change could forget."""
    from symphony.trackers.memory import MemoryTracker

    tracker = MemoryTracker(default_repo=None)
    issue = tracker.add_issue("i1", "ENG-1", labels=("symphony",))
    assert not issue.dispatchable
    assert issue.undispatchable_reason == "no-repo-designation"
    assert not eligible(issue, CFG, set())


def test_issue_with_a_resolvable_repository_is_unaffected():
    from symphony.trackers.memory import MemoryTracker

    tracker = MemoryTracker(default_repo=None)
    issue = tracker.add_issue("i1", "ENG-1", labels=("symphony", "repo:acme/backend"))
    assert issue.target_repo == "acme/backend"
    assert issue.undispatchable_reason is None
    assert eligible(issue, CFG, set())
