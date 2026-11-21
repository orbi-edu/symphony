"""A ticket whose attempts keep failing stops being retried and reaches a human."""

import datetime as dt

from tests.test_dispatch import FakeEffects, cfg

from symphony import state
from symphony.orchestrator import mark_clean_continuation, mark_retry_after_failure, tick


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def _dispatched(kv, clock, tracker, c):
    tracker.add_issue("i1", "ENG-1", updated_at="2026-01-01T00:00:00Z", url="https://t/ENG-1")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    return kv.get(state.issue_key("i1"))


def _fail(kv, clock, c, n, summary="model credential expired"):
    for _ in range(n):
        entry = kv.get(state.issue_key("i1"))
        mark_retry_after_failure(kv, "i1", entry, c, clock.now_ms(), summary)


def test_retries_stop_at_the_budget_and_report_once(kv, clock, tracker):
    c = cfg(max_failed_attempts=3)
    _dispatched(kv, clock, tracker, c)
    _fail(kv, clock, c, 2)
    assert kv.get(state.issue_key("i1"))["status"] == "retry_queued"
    _fail(kv, clock, c, 1)
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == "blocked"
    assert entry["retry_at"] is None

    fx = FakeEffects()
    clock.advance(600_000)
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == []
    [(_, comment)] = fx.reported
    assert "stopped retrying" in comment and "model credential expired" in comment
    assert "symphony reevaluate ENG-1" in comment
    [(event, text, link)] = fx.notified
    assert event == "retries_exhausted" and "Who acts" in text and link == "https://t/ENG-1"

    # The report's own comment moves the ticket's updated_at a moment later: not a human touch.
    tracker.set_state("i1", "Todo", updated_at=iso(clock.now_ms() + 2_000))
    for _ in range(3):
        fx = FakeEffects()
        clock.advance(60_000)
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
        assert fx.spawned == [] and getattr(fx, "reported", []) == [] and getattr(fx, "notified", []) == []


def test_a_human_touch_after_the_report_reopens_with_a_fresh_budget(kv, clock, tracker):
    c = cfg(max_failed_attempts=2)
    _dispatched(kv, clock, tracker, c)
    _fail(kv, clock, c, 2)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # reports
    clock.advance(300_000)
    tracker.set_state("i1", "Todo", updated_at=iso(clock.now_ms()))  # someone commented
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 3)]
    assert kv.get(state.issue_key("i1"))["failures"] == 0
    # one more failure is not the end: the budget started over
    _fail(kv, clock, c, 1)
    assert kv.get(state.issue_key("i1"))["status"] == "retry_queued"


def test_a_refresh_reopens_an_exhausted_ticket(kv, clock, tracker):
    c = cfg(max_failed_attempts=1)
    _dispatched(kv, clock, tracker, c)
    _fail(kv, clock, c, 1)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # reports
    kv.put(state.REFRESH_KEY, {"commandId": "c1"})
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 2)]


def test_a_clean_exit_resets_the_count(kv, clock, tracker):
    c = cfg(max_failed_attempts=2)
    _dispatched(kv, clock, tracker, c)
    _fail(kv, clock, c, 1)
    entry = kv.get(state.issue_key("i1"))
    mark_clean_continuation(kv, "i1", entry, clock.now_ms(), None)
    _fail(kv, clock, c, 1)
    assert kv.get(state.issue_key("i1"))["status"] == "retry_queued"


def test_zero_retries_forever(kv, clock, tracker):
    c = cfg(max_failed_attempts=0)
    _dispatched(kv, clock, tracker, c)
    _fail(kv, clock, c, 50)
    assert kv.get(state.issue_key("i1"))["status"] == "retry_queued"


def test_crash_reports_count_the_budget_and_the_last_one_does_not_promise_a_retry(kv, clock, tracker):
    c = cfg(max_failed_attempts=2)
    _dispatched(kv, clock, tracker, c)
    reports = []
    for _ in range(2):
        entry = kv.get(state.issue_key("i1"))
        entry.update(status="running", runner_call_id="fc-1", last_event_at=clock.now_ms(), retry_at=None)
        kv.put(state.issue_key("i1"), entry)
        for _ in range(2):  # two silent windows, and the call is gone
            clock.advance(c.stall_timeout_ms + 1_000)
            fx = FakeEffects(running=set())
            tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
            reports += [m for _, m in getattr(fx, "reported", [])]
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")  # the tick after reports the stop
    reports += [m for _, m in fx.reported]
    assert len(reports) == 2
    assert "failure 1 of 2 in a row" in reports[0]
    assert "stopped retrying" in reports[1] and "new attempt is queued" not in reports[1]
