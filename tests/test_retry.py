"""FR-006: exponential backoff with a cap; failure consumes an attempt, continuation does not; loop L1."""

from tests.test_dispatch import FakeEffects, cfg

from symphony import state
from symphony.config import parse_workflow
from symphony.orchestrator import (
    compute_backoff_ms,
    mark_clean_continuation,
    mark_retry_after_failure,
    tick,
)


def test_backoff_formula_and_cap():
    assert compute_backoff_ms(1, 300_000) == 10_000
    assert compute_backoff_ms(2, 300_000) == 20_000
    assert compute_backoff_ms(3, 300_000) == 40_000
    assert compute_backoff_ms(6, 300_000) == 300_000  # min(320s, 300s) — cap
    assert compute_backoff_ms(10, 120_000) == 120_000  # configurable cap


def test_failure_consumes_attempt_and_schedules_backoff(kv, clock):
    c = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    entry = kv.get(state.issue_key("i1"))
    mark_retry_after_failure(kv, "i1", entry, c, clock.now_ms(), "boom")
    saved = kv.get(state.issue_key("i1"))
    assert saved["status"] == "retry_queued"
    assert saved["attempt"] == 2  # attempt 1 was consumed
    assert saved["retry_at"] == clock.now_ms() + 10_000
    assert saved["failure_summary"] == "boom"


def test_clean_continuation_keeps_attempt_and_reschedules_1s(kv, clock):
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    entry = kv.get(state.issue_key("i1"))
    mark_clean_continuation(kv, "i1", entry, clock.now_ms(), "2026-01-01T00:00:00Z")
    saved = kv.get(state.issue_key("i1"))
    assert saved["status"] == "retry_queued"
    assert saved["attempt"] == 1  # does NOT consume an attempt
    assert saved["retry_at"] == clock.now_ms() + 1_000
    assert saved["failure_summary"] is None
    assert saved["last_clean_exit_at"] == clock.now_ms()


def test_due_retry_redispatches_with_new_token_and_failure_summary_kept(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1", updated_at="2026-02-02T00:00:00Z")
    c = cfg()
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    entry = kv.get(state.issue_key("i1"))
    mark_retry_after_failure(kv, "i1", entry, c, clock.now_ms(), "failure X")

    clock.advance(9_000)
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert fx2.spawned == []  # retry not due yet

    clock.advance(2_000)
    fx3 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx3, owner="d1")
    assert fx3.spawned == [("i1", 2)]
    assert kv.get(state.dispatch_key("i1", 2)) is not None  # attempt-2 token
    # the failure summary stays available for the retry prompt (loop L1)
    assert kv.get(state.issue_key("i1"))["failure_summary"] == "failure X"


def test_retry_held_when_blocker_appears(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    c = cfg()
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    entry = kv.get(state.issue_key("i1"))
    mark_retry_after_failure(kv, "i1", entry, c, clock.now_ms(), "x")
    tracker._blockers["i1"] = True
    tracker.set_state("i1", "Todo")  # recompute dispatchable with the blocker
    clock.advance(20_000)
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert fx2.spawned == []
    assert "retry_held" in fx2.names()
