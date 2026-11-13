"""The review handoff report: posted once per park, never twice, never fatal.

FR-001..FR-008. The interesting property is FR-002: the parked branch of the reconciliation
loop is re-entered on EVERY tick for as long as the issue sits in review, so "report on
park" and "report while parked" differ by a comment every 30 seconds, forever.
"""

from tests.test_dispatch import FakeEffects, cfg

from symphony import state, telemetry
from symphony.orchestrator import PARKED, tick


def dispatch_and_run(kv, clock, tracker, issue_id="i1", tokens=True):
    """Get an issue to the state a real one is in when it parks: dispatched, worked,
    with a token ledger."""
    c = cfg()
    tracker.add_issue(issue_id, f"ENG-{issue_id}")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key(issue_id))
    entry.update(status="running", runner_call_id=None, sandbox_name="sb-1", runs=2)
    if tokens:
        entry["tokens"] = {"input": 90_000, "output": 6_000, "cache_read": 0,
                           "cache_write": 0, "total": 96_000, "cost_estimate": 0.87}
    kv.put(state.issue_key(issue_id), entry)
    return c


def park(tracker, issue_id="i1", state_name="In Review"):
    tracker.set_state(issue_id, state_name)


def reports(fx):
    return getattr(fx, "reported", [])


def test_parking_posts_one_report_carrying_tokens_and_cost(kv, clock, tracker):
    c = dispatch_and_run(kv, clock, tracker)
    park(tracker)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")

    assert len(reports(fx)) == 1
    issue_id, message = reports(fx)[0]
    assert issue_id == "i1"
    assert "96 000" in message
    assert "0.87" in message
    assert "estimate" in message.lower()


def test_a_parked_issue_is_not_re_announced_on_every_tick(kv, clock, tracker):
    """SC-002. The branch re-fires each cycle; without a transition guard an issue parked
    for a day collects ~2,900 identical comments."""
    c = dispatch_and_run(kv, clock, tracker)
    park(tracker)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")

    later = FakeEffects()
    for _ in range(50):
        clock.advance(c.polling_interval_ms)
        tick(kv, tracker, c, clock.now_ms(), later, owner="d1")
    assert reports(later) == []
    assert "claim_released" not in later.names()  # the same guard silences the event flood


def test_parking_again_after_rework_reports_again(kv, clock, tracker):
    """FR-003. The second park covers a second body of work and deserves its own report."""
    c = dispatch_and_run(kv, clock, tracker)
    park(tracker)
    first = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), first, owner="d1")
    assert len(reports(first)) == 1

    # back to work, then parked again
    tracker.set_state("i1", "Rework")
    clock.advance(c.polling_interval_ms)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key("i1"))
    entry.update(sandbox_name="sb-2", tokens={"total": 5_000, "cost_estimate": 0.05})
    kv.put(state.issue_key("i1"), entry)

    park(tracker)
    clock.advance(c.polling_interval_ms)
    second = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), second, owner="d1")
    assert len(reports(second)) == 1


def test_a_failing_report_never_breaks_the_park(kv, clock, tracker):
    """FR-006. The report is an observation. An observation that can fail a tick is a
    liability, not an instrument."""
    c = dispatch_and_run(kv, clock, tracker)
    park(tracker)

    class Exploding(FakeEffects):
        def report_to_ticket(self, issue_id, message):
            raise RuntimeError("linear is down")

    fx = Exploding()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")

    assert kv.get(state.issue_key("i1"))["status"] == PARKED
    assert "session_report_failed" in fx.names()


def test_an_issue_that_never_ran_parks_silently(kv, clock, tracker):
    """FR-007. A human moving a fresh ticket to review must not get a report about a run
    that never happened."""
    c = cfg()
    tracker.add_issue("i1", "ENG-1")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    park(tracker)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert reports(fx) == []
    assert kv.get(state.issue_key("i1"))["status"] == PARKED


def test_the_trace_summary_is_included_when_one_was_collected(kv, clock, tracker):
    c = dispatch_and_run(kv, clock, tracker)
    kv.put(state.telemetry_key("i1"), dict(telemetry.new_rollup(), spans=9, llm_requests=4,
                                           tool_calls=5, tool_ms=1_000, llm_ms=2_000))
    park(tracker)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert "Session trace" in reports(fx)[0][1]
    assert "4 model requests" in reports(fx)[0][1]


def test_an_unreadable_rollup_still_yields_the_token_report(kv, clock, tracker):
    """SC-006: losing telemetry degrades the report, it never removes it."""
    c = dispatch_and_run(kv, clock, tracker)
    park(tracker)

    real_get = kv.get

    def flaky(key, default=None):
        if key == state.telemetry_key("i1"):
            raise RuntimeError("state down")
        return real_get(key, default)

    kv.get = flaky
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    kv.get = real_get
    assert "96 000" in reports(fx)[0][1]


def test_the_rollup_is_discarded_with_the_issue(kv, clock, tracker):
    """FR-017: no residue outlives the tracking entry."""
    c = dispatch_and_run(kv, clock, tracker)
    kv.put(state.telemetry_key("i1"), telemetry.new_rollup())
    tracker.set_state("i1", "Done")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    assert kv.get(state.telemetry_key("i1")) is None
