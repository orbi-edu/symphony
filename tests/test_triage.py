"""A failed run opens one investigation per failure signature, aimed at the triage repo."""

from dataclasses import replace

import pytest
from tests.test_dispatch import FakeEffects, cfg

from symphony import state, triage
from symphony.config import ConfigError, parse_workflow
from symphony.orchestrator import mark_retry_after_failure, tick

ERROR = "Agent generation termination could not be confirmed"


def board(**over):
    base = replace(cfg(), required_labels=("symphony",), triage_enabled=True,
                   triage_repo="acme/platform", triage_max_per_day=3)
    return replace(base, **over)


def crash(kv, clock, tracker, c, issue_id, identifier, fx=None):
    """Dispatch `identifier` and let its runner Job die without an outcome."""
    if tracker._find(identifier) is None:
        tracker.add_issue(issue_id, identifier, labels=("symphony",), url=f"https://t/{identifier}")
    entry = kv.get(state.issue_key(issue_id))
    if entry is None:
        tick(kv, tracker, c, clock.now_ms(), FakeEffects(running={"ignored"}), owner="d1")
        entry = kv.get(state.issue_key(issue_id))
    entry.update(status="running", runner_call_id=f"job-{identifier}", last_event_at=clock.now_ms(),
                 retry_at=None)
    kv.put(state.issue_key(issue_id), entry)
    clock.advance(3 * c.polling_interval_ms)
    fx = fx or FakeEffects(running=set())
    fx.evidence = {"job": f"job-{identifier}", "error": ERROR, "container": "Error (exit 1)",
                   "lines": [f"error='{ERROR}' event='runner_crashed'",
                             "token=ghs_abcdefghijklmnopqrstuvwxyz0123 leaked"]}
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    return fx


def investigations(tracker):
    return [i for i in tracker._issues.values() if "triage" in i.labels]


def test_a_crash_opens_an_investigation_that_an_agent_starts(kv, clock, tracker):
    fx = crash(kv, clock, tracker, board(), "i1", "ENG-1")
    [inv] = investigations(tracker)
    assert inv.state == "Todo"
    assert {"symphony", "investigation", "triage", "repo:acme/platform"} <= set(inv.labels)
    assert ERROR in inv.description and "ENG-1" in inv.description
    assert "ghs_" not in inv.description  # credentials never reach the ticket
    assert "symphony_create_issues" in inv.description
    [(event, text, link)] = fx.notified
    assert event == "triage_opened" and inv.identifier in text and "Who acts" in text


def test_the_same_failure_elsewhere_adds_an_occurrence_not_a_ticket(kv, clock, tracker):
    c = board()
    crash(kv, clock, tracker, c, "i1", "ENG-1")
    clock.advance(triage.REPEAT_COMMENT_EVERY_MS)
    crash(kv, clock, tracker, c, "i2", "ENG-2")
    [inv] = investigations(tracker)
    comments = [x["body"] for x in tracker.comments.get(inv.id, [])]
    assert any("Seen again: ENG-2" in body for body in comments)
    # a third time within the hour: counted, not commented
    crash(kv, clock, tracker, c, "i1", "ENG-1")
    assert len(tracker.comments.get(inv.id, [])) == len(comments)


def test_a_closed_investigation_is_reopened_as_a_new_one(kv, clock, tracker):
    c = board()
    crash(kv, clock, tracker, c, "i1", "ENG-1")
    [first] = investigations(tracker)
    tracker.set_state(first.id, "Done")
    crash(kv, clock, tracker, c, "i2", "ENG-2")
    assert len(investigations(tracker)) == 2


def test_an_investigation_that_fails_is_not_triaged_again(kv, clock, tracker):
    c = board()
    tracker.add_issue("t1", "ENG-5", labels=("symphony", "triage", "investigation"))
    crash(kv, clock, tracker, c, "t1", "ENG-5")
    assert [i.identifier for i in investigations(tracker)] == ["ENG-5"]


def test_new_investigations_are_capped_per_day(kv, clock, tracker):
    c = board(triage_max_per_day=1)
    crash(kv, clock, tracker, c, "i1", "ENG-1")
    fx = FakeEffects(running=set())
    tracker.add_issue("i2", "ENG-2", labels=("symphony",))
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(running={"x"}), owner="d1")
    entry = kv.get(state.issue_key("i2"))
    entry.update(status="running", runner_call_id="job-2", last_event_at=clock.now_ms(), retry_at=None)
    kv.put(state.issue_key("i2"), entry)
    clock.advance(3 * c.polling_interval_ms)
    fx.evidence = {"job": "job-2", "error": "a different failure: out of memory"}
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert len(investigations(tracker)) == 1
    assert "triage_capped" in fx.names()


def test_triage_off_opens_nothing(kv, clock, tracker):
    crash(kv, clock, tracker, board(triage_enabled=False), "i1", "ENG-1")
    assert investigations(tracker) == []


def test_exhausted_retries_name_the_investigation_as_who_acts(kv, clock, tracker):
    c = board(max_failed_attempts=1)
    tracker.add_issue("i1", "ENG-1", labels=("symphony",))
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key("i1"))
    entry["runner_call_id"] = "job-1"
    mark_retry_after_failure(kv, "i1", entry, c, clock.now_ms(), "the model provider refused the key")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    [inv] = investigations(tracker)
    [(_, comment)] = fx.reported
    assert inv.identifier in comment and "stopped retrying" in comment


def test_triage_needs_a_repository():
    with pytest.raises(ConfigError, match="triage.repo"):
        parse_workflow("---\ntracker: {kind: memory}\ntriage: {enabled: true}\n---\nx")


def test_the_signature_ignores_ids_and_numbers():
    a = triage.signature("runner died mid-attempt: job sym-run-eng-1-abc1234567 exit 1", {})
    b = triage.signature("runner died mid-attempt: job sym-run-eng-22-def7654321 exit 137", {})
    assert a == b
    assert a != triage.signature("stall detected", {})
