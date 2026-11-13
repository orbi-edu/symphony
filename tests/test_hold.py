"""Holding a ticket (internals §12): a human takes it over, and Symphony stays out until it is
released."""

import time

import pytest
from tests.test_dispatch import FakeEffects, cfg

from symphony import state
from symphony.orchestrator import PARKED, tick
from symphony.trackers.memory import MemoryTracker


def _hold(kv, issue_id="i1"):
    kv.put(state.hold_key(issue_id), state.hold_record("alice", "taking this one over", 1))


def _running(kv, tracker, clock, issue_id="i1"):
    tracker.add_issue(issue_id, "ENG-1")
    fx = FakeEffects()
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    entry = kv.get(state.issue_key(issue_id))
    entry.update(status="running", runner_call_id="job-1", sandbox_name="ws-eng-1",
                 bridge_token="tok", last_event_at=clock.now_ms())
    kv.put(state.issue_key(issue_id), entry)
    return entry


def test_a_held_issue_is_never_dispatched(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    tracker.add_issue("i2", "ENG-2")
    _hold(kv, "i1")
    fx = FakeEffects()
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i2", 1)]
    assert kv.get(state.issue_key("i1")) is None


def test_a_running_held_issue_is_stopped_and_parked_with_its_workspace_kept(kv, clock, tracker):
    entry = _running(kv, tracker, clock)
    seq = entry["dispatch_seq"]
    _hold(kv)
    fx = FakeEffects(running={"job-1"})
    clock.advance(1_000)
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")

    assert fx.cancelled == ["job-1"]        # the runner does not notice a hold on its own
    assert fx.killed == ["ws-eng-1"]
    assert fx.released == ["i1"] and fx.cleaned == []  # workspace kept for the human
    parked = kv.get(state.issue_key("i1"))
    assert parked["status"] == PARKED and parked["runner_call_id"] is None
    assert parked["bridge_token"] is None   # no more gateway access for the stopped run
    assert kv.get(state.dispatch_key("i1", seq)) is None  # its writes are fenced out
    assert "issue_held" in fx.names()

    again = FakeEffects()
    clock.advance(60_000)
    tick(kv, tracker, cfg(), clock.now_ms(), again, owner="d1")
    assert again.spawned == [] and again.cancelled == [] and again.released == []


def test_a_queued_retry_or_continuation_of_a_held_issue_waits(kv, clock, tracker):
    entry = _running(kv, tracker, clock)
    entry.update(status="retry_queued", runner_call_id=None, retry_at=clock.now_ms())
    kv.put(state.issue_key("i1"), entry)
    _hold(kv)
    fx = FakeEffects()
    clock.advance(5_000)
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [] and fx.killed == []
    assert kv.get(state.issue_key("i1"))["status"] == PARKED


def test_release_makes_the_issue_eligible_on_the_next_tick(kv, clock, tracker):
    entry = _running(kv, tracker, clock)
    _hold(kv)
    tick(kv, tracker, cfg(), clock.now_ms(), FakeEffects(running={"job-1"}), owner="d1")

    kv.delete(state.hold_key("i1"))
    fx = FakeEffects()
    clock.advance(1_000)
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 1)]
    fresh = kv.get(state.issue_key("i1"))
    assert fresh["status"] == "claimed" and fresh["dispatch_seq"] > entry["dispatch_seq"]


def test_a_held_ticket_under_review_is_not_woken_to_answer(kv, clock, tracker):
    """The review watch dispatches answer runs; a held ticket belongs to the human."""
    from tests.test_review_watch import WatchEffects, comment, park
    from tests.test_review_watch import cfg as watch_cfg

    c = watch_cfg()
    park(kv, clock, tracker, c)
    _hold(kv)
    fx = WatchEffects(comments=[comment("2026-08-07T09:00:00Z")])
    for _ in range(3):
        clock.advance(200_000)
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [] and fx.conversation_calls == 0


# -- the command path (daemon side of POST /v1/commands) ----------------------


@pytest.fixture
def daemon(redis_client):
    from symphony.admin_commands import AdminStore
    from symphony.kv import RedisKV

    kv = RedisKV(redis_client)
    now = time.time_ns() // 1_000_000
    assert state.renew_lease(kv, "daemon", now)
    state.heartbeat(kv, now)  # commands apply only under a fresh, active daemon
    return AdminStore(redis_client, "test"), kv


def _command(store, action, key):
    return store.accept(
        actor="alice", role="operator", credential_fingerprint="f" * 64, action=action,
        target={"issueId": "i1"}, reason="Human takeover of ENG-1", idempotency_key=key,
    )


def _process(store, kv, tracker):
    from symphony.admin_commands import process_commands

    return process_commands(
        store, kv, tracker, cfg(), FakeEffects(), "daemon", time.time_ns() // 1_000_000,
        publish_workflow=lambda *a, **k: None, authorize=lambda record: True,
    )


def test_hold_and_release_commands_write_and_delete_the_hold(daemon):
    store, kv = daemon
    tracker = MemoryTracker()
    tracker.add_issue("i1", "ENG-1")
    tracker.raise_unavailable = True  # a takeover must work during a tracker outage

    _command(store, "hold", "h1")
    assert _process(store, kv, tracker) == {"completed": 1}
    record = kv.get(state.hold_key("i1"))
    assert record["by"] == "alice" and record["reason"] == "Human takeover of ENG-1"
    assert isinstance(record["at"], int) and record["at"] > 0
    assert state.held_issues(kv) == {"i1": record}

    tracker.raise_unavailable = False
    fx = FakeEffects()
    tick(kv, tracker, cfg(), time.time_ns() // 1_000_000, fx, owner="daemon")
    assert fx.spawned == []

    _command(store, "release", "r1")
    assert _process(store, kv, tracker) == {"completed": 1}
    assert kv.get(state.hold_key("i1")) is None
    fx = FakeEffects()
    tick(kv, tracker, cfg(), time.time_ns() // 1_000_000, fx, owner="daemon")
    assert fx.spawned == [("i1", 1)]


def test_a_hold_never_expires_and_reads_never_give_it_a_ttl(daemon, redis_client):
    """A takeover lasts until it is released — not until 7 days of daemon downtime pass.
    The production tick reads through LeaseKV, the API through RedisKV: neither may add a
    TTL to the hold."""
    from symphony.kv import LeaseKV

    store, kv = daemon
    tracker = MemoryTracker()
    tracker.add_issue("i1", "ENG-1")
    _command(store, "hold", "h1")
    _process(store, kv, tracker)
    key = state.hold_key("i1")
    assert redis_client.ttl(key) == -1  # exists, no expiry

    for reader in (kv, LeaseKV(kv, "daemon")):
        fx = FakeEffects()
        tick(reader, tracker, cfg(), time.time_ns() // 1_000_000, fx, owner="daemon")
        assert fx.spawned == []
        assert set(state.held_issues(reader)) == {"i1"}
        assert kv.get(key)["by"] == "alice"
        assert redis_client.ttl(key) == -1

    _command(store, "release", "r1")
    _process(store, kv, tracker)
    assert redis_client.exists(key) == 0
    fx = FakeEffects()
    tick(LeaseKV(kv, "daemon"), tracker, cfg(), time.time_ns() // 1_000_000, fx, owner="daemon")
    assert fx.spawned == [("i1", 1)]
