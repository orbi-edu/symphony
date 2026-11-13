"""§3.7 scenarios (constitution IV): dual daemon, platform re-execution,
orphan claim, mutation replay, exactly-once retro with sweep."""

from dataclasses import replace

from tests.test_dispatch import FakeEffects, cfg

from symphony import state
from symphony.orchestrator import tick
from symphony.runner import RunnerEnv, fencing_ok


def test_two_daemons_only_one_wins_dispatch_token(kv, clock, tracker):
    """The lease is advisory — the real defense is the token: two concurrent ticks
    over the same board dispatch ONCE."""
    tracker.add_issue("i1", "ENG-1")
    c = cfg()
    fx_a, fx_b = FakeEffects(), FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx_a, owner="daemon-A")
    tick(kv, tracker, c, clock.now_ms(), fx_b, owner="daemon-B")
    assert len(fx_a.spawned) + len(fx_b.spawned) == 1


def test_retry_token_collision_between_daemons(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="A")
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="retry_queued", retry_at=clock.now_ms(), attempt=2)
    kv.put(state.issue_key("i1"), entry)
    # daemon B already won the token for the NEXT dispatch (seq 2 = dispatch_seq 1 + 1)
    assert state.acquire_dispatch_token(kv, "i1", 2, "B", clock.now_ms())
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="A")
    assert fx.spawned == []
    assert "dispatch_token_lost" in fx.names()


def test_platform_reexecution_of_old_attempt_is_fenced(kv, clock):
    """A platform may re-execute crashed work with the SAME arguments; an attempt-1
    runner re-executed after the daemon advanced to attempt 2 must shut itself down."""
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    state.acquire_dispatch_token(kv, "i1", 1, "A", clock.now_ms())
    assert fencing_ok(kv, "i1", 1, 1)
    entry = kv.get(state.issue_key("i1"))
    entry["attempt"] = 2  # daemon moved on
    kv.put(state.issue_key("i1"), entry)
    assert not fencing_ok(kv, "i1", 1, 1)
    assert not fencing_ok(kv, "i1", 3, 3)  # a nonexistent token fails too


def test_replayed_runner_of_old_dispatch_seq_is_fenced_after_continuation(kv, clock):
    """Clean continuation reuses the SAME attempt with a new seq — a replay of the
    old dispatch (previous seq) must fail fencing even with the correct attempt."""
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    state.acquire_dispatch_token(kv, "i1", 1, "A", clock.now_ms())
    entry = kv.get(state.issue_key("i1"))
    entry.update(dispatch_seq=2)  # continuation redispatch already happened
    kv.put(state.issue_key("i1"), entry)
    state.acquire_dispatch_token(kv, "i1", 2, "A", clock.now_ms())
    assert not fencing_ok(kv, "i1", 1, 1)  # replay of the old seq: fenced
    assert fencing_ok(kv, "i1", 1, 2)  # the current dispatch remains valid


def test_reopened_issue_never_reuses_an_old_dispatch_identity(kv, clock):
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    state.acquire_dispatch_token(kv, "i1", 9, "A", clock.now_ms())
    kv.delete(state.issue_key("i1"))
    assert state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    entry = kv.get(state.issue_key("i1"))
    assert entry["dispatch_seq"] == 10
    assert not fencing_ok(kv, "i1", 1, 1)


def test_pause_gates_new_and_retry_dispatch_but_reconciles_terminal(kv, clock, tracker):
    tracker.add_issue("retry", "ENG-1")
    tracker.add_issue("terminal", "ENG-2")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="A")
    retry = kv.get(state.issue_key("retry"))
    retry.update(status="retry_queued", retry_at=clock.now_ms())
    kv.put(state.issue_key("retry"), retry)
    tracker.add_issue("new", "ENG-3")
    tracker.add_issue("terminal", "ENG-2", state="Done")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="A", admission_open=lambda: False)
    assert fx.spawned == []
    assert kv.get(state.issue_key("retry"))["status"] == "retry_queued"
    assert kv.get(state.issue_key("terminal")) is None


def test_fenced_runner_makes_no_side_effects(kv, clock, tracker, redis_client):
    from symphony.runner import run_attempt
    from symphony.sessions import SessionStore
    from symphony.teamconfig import TeamConfig

    calls = {"sandbox": 0}

    def ensure_sandbox(issue, bound):
        calls["sandbox"] += 1
        raise AssertionError("must not be called")

    env = RunnerEnv(
        kv=kv, tracker=tracker, cfg=cfg(), now_ms=clock.now_ms, call_id="fc-zombie",
        ensure_sandbox=ensure_sandbox, open_client=lambda s, t, i, c: None,
        kill_agent=lambda s: None, release_sandbox=lambda s: None,
        wall_deadline_ms=clock.now_ms() + 10_000,
        sessions=SessionStore(redis_client, "test"), team=TeamConfig(redis_client, "test"),
    )
    assert run_attempt(env, "ghost-issue", 1) == "fenced"
    assert calls["sandbox"] == 0
    assert kv.get(state.issue_key("ghost-issue")) is None


def test_orphan_claim_reopened_without_burning_attempt(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="A")
    # daemon died between claim and spawn: no runner_call_id
    clock.advance(3 * c.polling_interval_ms)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="A")
    assert "orphan_claim_reopened" in fx.names()
    entry = kv.get(state.issue_key("i1"))
    assert entry["attempt"] == 1  # did NOT burn an attempt


def test_toolcall_replay_answered_from_cache(kv, clock, tracker, redis_client):
    """§3.7.4 preserved across the move: the tracker tool is now a runner host tool rather
    than a bridge endpoint, but the idempotency guarantee is identical — a replayed
    tool-call id answers from cache, and a new dispatch seq opens a fresh namespace."""
    from symphony.hosttools import HostToolBroker
    from symphony.sessions import SessionStore

    results = []

    def call(seq, query):
        broker = HostToolBroker(
            store=SessionStore(redis_client, "test"), sid=f"i1:{seq}", kv=kv, tracker=tracker,
            issue_id="i1", seq=seq, identifier="ENG-1",
            respond=lambda cid, text, is_error=False: results.append((text, is_error)),
            owns=lambda: True, input_timeout_s=1,
        )
        broker._serve({
            "type": "host_tool_call", "id": f"h-{len(results)}", "toolCallId": "call-9",
            "toolName": "linear_graphql", "arguments": {"query": query},
        })
        return results[-1]

    r1 = call(1, "mutation X")
    r2 = call(1, "mutation X")
    assert r1 == r2
    assert len(tracker.mutations) == 1  # replay does NOT re-execute the mutation

    # a NEW dispatch (different seq) with the same call id does NOT collide with the cache
    r3 = call(2, "mutation Z")
    assert len(tracker.mutations) == 2
    assert r3 != r1


def test_terminal_cleanup_removes_dispatch_tokens_for_reopen(kv, clock, tracker):
    """A stale token must not jam a future reopen of the issue (new claim, attempt 1)."""
    issue = tracker.add_issue("i1", "ENG-1")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="A")
    tracker.set_state("i1", "Done")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="A")
    assert fx.cleaned == ["i1"]
    assert kv.get(state.dispatch_key("i1", 1)) is None
    # reopen: a new claim wins and attempt 1 dispatches again
    tracker.set_state("i1", "Todo")
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="A")
    assert fx2.spawned == [(issue.id, 1)]


def test_retro_token_exactly_once_and_sweep_does_not_duplicate(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="A")
    tracker.set_state("i1", "Done")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="A")
    assert fx.reflected == ["i1"]  # retro fired ONCE, before cleanup
    # the nightly sweep sees the token and does NOT duplicate
    assert not state.acquire_retro_token(kv, "i1", clock.now_ms())


def test_reflect_on_scopes_retro_but_default_covers_all_terminals(kv, clock, tracker):
    c = cfg()
    assert c.effective_reflect_on == c.terminal_states  # default inherits everything
    scoped = replace(c, reflect_on=("Done",))
    tracker.add_issue("i1", "ENG-1")
    tick(kv, tracker, scoped, clock.now_ms(), FakeEffects(), owner="A")
    tracker.set_state("i1", "Canceled")
    fx = FakeEffects()
    tick(kv, tracker, scoped, clock.now_ms(), fx, owner="A")
    assert fx.reflected == []  # outside the explicit reflect_on
    assert fx.cleaned == ["i1"]  # cleanup still happens regardless
