"""Runner turn loop over Oh My Pi RPC (FR-005, FR-006, FR-013, FR-017)."""

import pytest
from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, FakeSandbox, assistant, runner_env

from symphony import state
from symphony.config import parse_workflow
from symphony.runner import fencing_ok, new_bridge_token, run_attempt
from symphony.trackers.memory import MemoryTracker

CFG = parse_workflow(
    "---\ntracker: {kind: memory}\nagent: {max_turns: 2}\n"
    "engine: {provider: acme, model: acme-1}\n---\nissue {{ issue.identifier }}\n"
)


def _cfg(max_turns):
    return parse_workflow(
        f"---\ntracker: {{kind: memory}}\nagent: {{max_turns: {max_turns}}}\n"
        "engine: {provider: acme, model: acme-1}\n---\nx\n"
    )


def _claim(kv, session_path=None):
    kv.put(
        state.issue_key("i1"),
        {
            "status": "claimed",
            "identifier": "ENG-1",
            "attempt": 1,
            "dispatch_seq": 1,
            "runs": 1,
            "tokens": {},
            "session_path": session_path,
            "failure_summary": None,
        },
    )
    kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "d1"})


@pytest.fixture
def env_factory(redis_client):
    def make(issue_state="Todo", scripted=None, max_turns=None, refresh_ok=True):
        kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
        tracker.add_issue("i1", "ENG-1", state=issue_state)
        _claim(kv)
        engine = FakeEngine()
        if scripted:
            scripted(engine)
        env = runner_env(
            redis_client,
            kv=kv,
            tracker=tracker,
            cfg=CFG if max_turns is None else _cfg(max_turns),
            clock=clock,
            engine=engine,
            refresh_credentials=lambda sb: refresh_ok,
        )
        return env, kv, tracker, engine, clock

    return make


def _one_run(engine):
    engine.run()


@pytest.mark.parametrize(("startup_delay_s", "outcome"), [(6, "continuation"), (120, "failed")])
def test_cold_engine_startup_has_a_bounded_grace_period(env_factory, startup_delay_s, outcome):
    env, _, _, engine, clock = env_factory(scripted=_one_run, max_turns=1)
    ready_at = clock.now_ms() + startup_delay_s * 1000
    read_line = engine.read_line

    def cold_read(timeout_s):
        remaining = max(0, ready_at - clock.now_ms())
        clock.advance(min(remaining, int(timeout_s * 1000)))
        if clock.now_ms() < ready_at:
            return None
        return read_line(timeout_s)

    engine.read_line = cold_read
    assert run_attempt(env, "i1", 1, 1) == outcome


def test_fencing_rejects_stale_attempt_and_stale_seq():
    kv = FakeKV()
    kv.put(state.issue_key("i1"), {"attempt": 2, "dispatch_seq": 5, "status": "claimed"})
    kv.put(state.dispatch_key("i1", 5), {"daemon_owner": "d"})
    assert fencing_ok(kv, "i1", 2, 5)
    assert not fencing_ok(kv, "i1", 1, 5)  # stale attempt
    assert not fencing_ok(kv, "i1", 2, 4)  # stale seq


def test_lost_fence_exits_with_zero_side_effects(env_factory):
    env, kv, tracker, engine, _ = env_factory()
    kv.delete(state.dispatch_key("i1", 1))
    assert run_attempt(env, "i1", 1, 1) == "fenced"
    assert engine.sent == []  # never even opened a client
    assert tracker.mutations == []
    assert env.sessions.list() == []  # no session for an attempt that never owned the issue


def test_runner_losing_generation_during_setup_cannot_overwrite_successor(env_factory):
    env, kv, _, _, _ = env_factory()
    successor = {
        "attempt": 2,
        "dispatch_seq": 2,
        "status": "running",
        "bridge_token": "successor",
        "tokens": {"total": 90},
        "session_path": "/successor.jsonl",
    }
    original = env.ensure_sandbox
    killed = []

    def supersede(issue, bound):
        kv.put(state.issue_key("i1"), dict(successor))
        kv.put(state.dispatch_key("i1", 2), {"daemon_owner": "new"})
        return original(issue, bound)

    env.ensure_sandbox = supersede
    env.kill_agent = lambda sandbox: killed.append(sandbox)
    assert run_attempt(env, "i1", 1, 1) == "fenced"
    assert kv.get(state.issue_key("i1")) == successor
    assert killed == []
    assert env.sessions.meta("i1:1")["outcome"] == "fenced"


def test_interrupted_runner_cannot_flush_or_finish_over_fence(env_factory):
    env, kv, _, _, _ = env_factory()
    original = env.tracker.fetch_issues_by_ids
    interrupted = {}

    def interrupt(ids):
        current = dict(kv.get(state.issue_key("i1")))
        current.update(status="interrupting", bridge_token=None, admin_interrupt="command")
        kv.put(state.issue_key("i1"), current)
        interrupted.update(current)
        return original(ids)

    env.tracker.fetch_issues_by_ids = interrupt
    assert run_attempt(env, "i1", 1, 1) == "fenced"
    assert kv.get(state.issue_key("i1")) == interrupted


def test_redis_failure_after_launch_cannot_skip_generation_stop(env_factory):
    env, kv, _, _, _ = env_factory()
    open_client = env.open_client
    stopped, released = [], []

    def unavailable(key, default=None):
        raise ConnectionError("Redis unavailable")

    def launch(sandbox, token, issue, bound):
        client = open_client(sandbox, token, issue, bound)
        kv.get = unavailable
        return client

    env.open_client = launch
    env.kill_agent = lambda sandbox: stopped.append(sandbox.name)
    env.release_sandbox = lambda sandbox: released.append(sandbox.name)
    with pytest.raises(ConnectionError):
        run_attempt(env, "i1", 1, 1)
    assert stopped == ["ws-ENG-1"]
    assert released == []


def test_superseded_launched_runner_stops_only_its_generation_without_release(env_factory):
    env, kv, _, _, _ = env_factory()
    open_client = env.open_client
    stopped, released = [], []
    successor = {
        "attempt": 2,
        "dispatch_seq": 2,
        "status": "running",
        "bridge_token": "successor",
    }

    def launch(sandbox, token, issue, bound):
        client = open_client(sandbox, token, issue, bound)
        kv.put(state.issue_key("i1"), dict(successor))
        kv.put(state.dispatch_key("i1", 2), {"daemon_owner": "new"})
        return client

    env.open_client = launch
    env.kill_agent = lambda sandbox: stopped.append(sandbox.name)
    env.release_sandbox = lambda sandbox: released.append(sandbox.name)
    assert run_attempt(env, "i1", 1, 1) == "fenced"
    assert stopped == ["ws-ENG-1"]
    assert released == []
    assert kv.get(state.issue_key("i1")) == successor


def test_duplicate_running_job_cannot_rotate_owner_token(env_factory):
    env, kv, _, engine, _ = env_factory()
    entry = dict(kv.get(state.issue_key("i1")))
    entry.update(status="running", bridge_token="existing-runner")
    kv.put(state.issue_key("i1"), entry)
    assert run_attempt(env, "i1", 1, 1) == "fenced"
    assert kv.get(state.issue_key("i1"))["bridge_token"] == "existing-runner"
    assert engine.sent == []


def test_bridge_token_is_generated_per_attempt_and_cleared_at_finish(env_factory):
    """The token authorizes the gateway for THIS attempt only, so a leak cannot be
    replayed against a later one."""
    env, kv, _, _, _ = env_factory(issue_state="Done")
    run_attempt(env, "i1", 1, 1)
    assert kv.get(state.issue_key("i1"))["bridge_token"] is None
    assert new_bridge_token() != new_bridge_token()
    assert len(new_bridge_token()) >= 32


def test_issue_left_active_states_finishes_done(env_factory):
    env, kv, _, _, _ = env_factory(issue_state="Human Review")
    assert run_attempt(env, "i1", 1, 1) == "done"


def test_turn_cap_ends_cleanly_without_consuming_an_attempt(env_factory):
    """FR-005: hitting the ceiling is a continuation, not a failure."""

    def script(engine):
        for _ in range(3):
            engine.run()

    env, kv, _, _, _ = env_factory(scripted=script, max_turns=1)
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    entry = kv.get(state.issue_key("i1"))
    assert entry["attempt"] == 1  # NOT incremented
    assert entry["status"] == "retry_queued"
    assert env.sessions.meta("i1:1")["outcome"] == "continuation"


def test_run_failure_consumes_the_attempt_and_records_the_reason(env_factory):
    def script(engine):
        engine.run(stop_reason="error", error="529 overloaded_error")

    env, kv, _, _, _ = env_factory(scripted=script)
    assert run_attempt(env, "i1", 1, 1) == "failed"
    entry = kv.get(state.issue_key("i1"))
    assert entry["attempt"] == 2
    assert "529 overloaded" in entry["failure_summary"]


def test_preflight_rejection_terminates_instead_of_hanging(env_factory):
    """A rejected prompt can fail without emitting any agent lifecycle events."""
    env, kv, _, engine, _ = env_factory()
    engine.responses["prompt"] = {"success": False, "error": "No API key found for acme."}
    assert run_attempt(env, "i1", 1, 1) == "failed"
    assert "No API key" in kv.get(state.issue_key("i1"))["failure_summary"]


def test_an_extension_error_is_published_not_fatal(env_factory):
    """No Symphony code runs as an omp extension any more; a built-in extension failing is
    something people watching should see, not a reason to burn the attempt."""

    def script(engine):
        engine.push({"type": "extension_error", "extensionPath": "/x.ts", "error": "boom"})
        engine.run()

    env, _, _, _, _ = env_factory(scripted=script, max_turns=1)
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    kinds = [kind for _, kind, _ in env.sessions.read_events("i1:1")]
    assert "extension_error" in kinds


def test_usage_accumulates_into_the_claim_and_global_totals(env_factory):
    env, kv, _, _, _ = env_factory(scripted=_one_run, max_turns=1)
    run_attempt(env, "i1", 1, 1)
    tokens = kv.get(state.issue_key("i1"))["tokens"]
    assert tokens["input"] == 10 and tokens["output"] == 5
    assert round(tokens["cost_estimate"], 4) == 0.01
    assert kv.get(state.TOTALS_KEY)["input"] == 10


def test_internal_retry_is_one_turn_and_accounts_for_both_model_calls(env_factory):
    def script(engine):
        failed = assistant("", "error", error="transient", tokens=(3, 1))
        engine.push({"type": "message_end", "message": failed})
        engine.push({"type": "auto_retry_start", "attempt": 1})
        engine.push({"type": "agent_end", "isTerminal": False, "messages": [failed]})
        engine.push({"type": "agent_start"})
        engine.push({"type": "auto_retry_end", "success": True, "attempt": 1})
        engine.run()

    env, kv, _, engine, _ = env_factory(scripted=script, max_turns=1)
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    entry = kv.get(state.issue_key("i1"))
    assert entry["attempt"] == 1
    assert entry["tokens"]["total"] == 19
    assert len(engine.commands("prompt")) == 1


@pytest.mark.parametrize("cancel_new", [False, True])
def test_stale_session_restarts_only_when_new_session_succeeds(redis_client, cancel_new):
    """A lost session must not burn an attempt."""
    kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
    tracker.add_issue("i1", "ENG-1", state="Todo")
    _claim(kv, session_path="/gone.jsonl")
    engine = FakeEngine()
    engine.responses["switch_session"] = {"success": False, "error": "no such session"}
    engine.responses["new_session"] = {"success": True, "data": {"cancelled": cancel_new}}
    engine.responses["get_state"] = {
        "success": True,
        "data": {"sessionFile": "/new.jsonl", "sessionId": "s-2"},
    }
    engine.run()
    env = runner_env(
        redis_client, kv=kv, tracker=tracker, cfg=_cfg(1), clock=clock, engine=engine,
        sandbox=FakeSandbox(),
    )
    outcome = run_attempt(env, "i1", 1, 1)
    entry = kv.get(state.issue_key("i1"))
    if cancel_new:
        assert outcome == "failed"
        assert entry["session_path"] is None
        assert engine.commands("prompt") == []
    else:
        assert outcome == "continuation"
        assert entry["attempt"] == 1
        assert entry["session_path"] == "/new.jsonl"


def test_cancelled_session_switch_never_prompts_in_the_previous_session(env_factory):
    env, kv, _, engine, _ = env_factory(max_turns=1)
    entry = kv.get(state.issue_key("i1"))
    entry["session_path"] = "/saved.jsonl"
    kv.put(state.issue_key("i1"), entry)
    engine.responses["switch_session"] = {"success": True, "data": {"cancelled": True}}
    assert run_attempt(env, "i1", 1, 1) == "failed"
    assert kv.get(state.issue_key("i1"))["session_path"] == "/saved.jsonl"
    assert engine.commands("prompt") == []


def test_credential_refresh_failure_fails_with_its_own_reason(env_factory):
    """FR-018: not an opaque push rejection hours into the attempt."""
    env, kv, _, _, _ = env_factory(scripted=_one_run, refresh_ok=False)
    assert run_attempt(env, "i1", 1, 1) == "failed"
    assert "credential" in kv.get(state.issue_key("i1"))["failure_summary"]


def test_wall_clock_guard_yields_a_continuation(env_factory):
    env, kv, _, _, clock = env_factory()
    env.wall_deadline_ms = clock.now_ms() - 1
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert kv.get(state.issue_key("i1"))["attempt"] == 1


def test_agent_is_sigtermed_then_swept_before_the_sandbox_is_released(env_factory):
    """The engine gets SIGTERM and time to exit (its last telemetry batch is exported on
    SIGTERM, lost on EOF) before the generation sweep and the release (constitution VI)."""
    order = []
    env, _, _, engine, _ = env_factory(scripted=_one_run, max_turns=1)
    engine.terminate = lambda grace_s: order.append(("sigterm", grace_s)) or True
    env.kill_agent = lambda sb: order.append("kill")
    env.release_sandbox = lambda sb: order.append("release")
    run_attempt(env, "i1", 1, 1)
    assert order[0][0] == "sigterm" and order[0][1] > 0
    assert order[1:] == ["kill", "release"]
