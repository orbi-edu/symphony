"""FR-007/FR-010/FR-011/FR-012: double-window stall, orphans, outage, distinct exits,
startup sweep, structured events per stage."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException
from tests.test_dispatch import FakeEffects, cfg

from symphony import effects, kube, orchestrator, state
from symphony.orchestrator import startup_sweep, tick
from symphony.trackers.memory import MemoryTracker


def dispatch_one(kv, clock, tracker, c=None, issue_id="i1"):
    c = c or cfg()
    tracker.add_issue(issue_id, f"ENG-{issue_id}")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    return c


def set_running(kv, clock, issue_id="i1", call_id="fc-1"):
    entry = kv.get(state.issue_key(issue_id))
    entry.update(status="running", runner_call_id=call_id, last_event_at=clock.now_ms())
    kv.put(state.issue_key(issue_id), entry)


def test_stall_requires_two_windows_and_live_call_confirmation(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    clock.advance(c.stall_timeout_ms + 1_000)

    fx1 = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx1, owner="d1")
    assert fx1.cancelled == []  # 1st window: NEVER kills
    assert "stall_strike" in fx1.names()

    clock.advance(c.stall_timeout_ms + 1_000)
    fx2 = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert fx2.cancelled == ["fc-1"]  # 2nd window + live call confirmed ⇒ cancel
    assert fx2.released == ["i1"]  # the cancelled runner never releases its workspace
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == "retry_queued"
    assert entry["attempt"] == 2
    assert "stall" in entry["failure_summary"]


def test_progress_resets_stall_strikes(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    clock.advance(c.stall_timeout_ms + 1_000)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(running={"fc-1"}), owner="d1")
    # the runner showed signs of life
    entry = kv.get(state.issue_key("i1"))
    entry.update(last_event_at=clock.now_ms())
    kv.put(state.issue_key("i1"), entry)
    clock.advance(c.stall_timeout_ms + 1_000)
    fx = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    # the silence started over from zero: it's strike 1 again, not a kill
    assert fx.cancelled == []


def test_dead_runner_requeued_as_crash_without_waiting_for_the_stall_windows(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    clock.advance(3 * c.polling_interval_ms)  # far below the stall timeout
    fx = FakeEffects(running=set())  # the runner Job ended without recording an outcome
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.cancelled == []  # nothing to cancel — it died on its own
    assert "runner_crash_requeued" in fx.names()
    assert kv.get(state.issue_key("i1"))["attempt"] == 2
    # It died before its own release: the daemon starts the idle clock, once.
    assert fx.released == ["i1"]


def test_a_dead_runner_out_of_retries_still_has_its_workspace_released(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker, cfg(max_failed_attempts=1))
    set_running(kv, clock)
    clock.advance(3 * c.polling_interval_ms)
    fx = FakeEffects(running=set())
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert kv.get(state.issue_key("i1"))["status"] == "blocked"
    # Blocked waits for a human, maybe for hours: the idle workspace must not pin a node.
    assert fx.released == ["i1"]


def test_a_live_silent_runner_is_left_alone_until_the_stall_windows(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    clock.advance(3 * c.polling_interval_ms)
    fx = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert "runner_crash_requeued" not in fx.names()
    assert kv.get(state.issue_key("i1"))["status"] == "running"


@pytest.mark.parametrize("phase", ["Failed", "Succeeded"])
def test_terminal_workspace_cleanup_never_executes_in_dead_pod(kv, clock, tracker, monkeypatch, phase):
    c = dispatch_one(kv, clock, tracker, cfg(learning_enabled=False))
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="running", sandbox_name="dead-workspace")
    kv.put(state.issue_key("i1"), entry)
    tracker.set_state("i1", "Done")
    monkeypatch.setattr(kube, "get_workspace", lambda *args: SimpleNamespace(status=SimpleNamespace(phase=phase)))
    deleted = []
    monkeypatch.setattr(kube, "delete_pod", lambda core, ns, name: deleted.append(("pod", name)))
    monkeypatch.setattr(kube, "delete_session_pvc", lambda core, ns, name: deleted.append(("pvc", name)))
    monkeypatch.setattr(effects, "PodSandbox", Mock(side_effect=AssertionError("Cannot exec a terminal pod")))
    fx = effects.K8sEffects(c, Mock(), Mock(), "test")
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert kv.get(state.issue_key("i1")) is None
    assert deleted == [("pod", "dead-workspace"), ("pvc", "dead-workspace")]


def test_unconfirmed_live_workspace_stop_does_not_advance_retry(kv, clock, tracker, monkeypatch):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    entry = kv.get(state.issue_key("i1"))
    entry.update(sandbox_name="live-workspace", stall_strikes=1,
                 stall_seen_event_at=entry["last_event_at"])
    kv.put(state.issue_key("i1"), entry)
    clock.advance(c.stall_timeout_ms + 1000)
    monkeypatch.setattr(kube, "get_workspace", lambda *args: SimpleNamespace(status=SimpleNamespace(phase="Running")))
    monkeypatch.setattr(kube, "job_status", lambda *args: "failed")
    monkeypatch.setattr(effects, "PodSandbox", lambda *args: Mock())
    monkeypatch.setattr(effects, "kill_agent_processes", lambda sandbox: False)
    fx = effects.K8sEffects(c, Mock(), Mock(), "test")
    with pytest.raises(RuntimeError, match="termination could not be confirmed"):
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert kv.get(state.issue_key("i1"))["status"] == "running"
    assert kv.get(state.issue_key("i1"))["attempt"] == 1


def test_a_runner_dead_before_recording_its_workspace_still_releases_it(
    kv, clock, tracker, monkeypatch
):
    """A first attempt that died while it prepared the workspace (OOM, node loss) left the
    pod busy, guarded against eviction and with no idle clock, and never recorded its name."""
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    assert kv.get(state.issue_key("i1"))["sandbox_name"] is None
    clock.advance(3 * c.polling_interval_ms)
    monkeypatch.setattr(kube, "job_status", lambda *args: "failed")
    released = []
    monkeypatch.setattr(
        kube, "set_idle_since", lambda core, ns, name, at_ms: released.append((name, at_ms))
    )
    fx = effects.K8sEffects(c, Mock(), Mock(), "test")
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert kv.get(state.issue_key("i1"))["status"] == "retry_queued"
    [(name, at_ms)] = released
    assert name == effects.sandbox_name("ENG-i1") and at_ms


def runner_job(*, issue_id="i1", seq="1"):
    container = SimpleNamespace(
        name="runner", command=["python", "-m", "symphony.entry.run_issue"],
        env=[SimpleNamespace(name=key, value=value) for key, value in {
            "SYMPHONY_ISSUE_ID": issue_id, "SYMPHONY_SEQ": seq, "SYMPHONY_CALL_ID": "original-job",
        }.items()],
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(labels={"app": "original-app", "component": "runner"}, uid="original-uid"),
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))),
    )


def test_original_runner_remains_cancellable_after_app_name_reload():
    batch = Mock()
    batch.read_namespaced_job.side_effect = [runner_job(), ApiException(status=404)]
    fx = effects.K8sEffects(cfg(app_name="renamed-app"), Mock(), batch, "test")
    fx.cancel_runner("original-job", issue_id="i1", seq=1)
    deletion = batch.delete_namespaced_job.call_args
    assert deletion.args == ("original-job", "test")
    assert deletion.kwargs["body"]["preconditions"] == {"uid": "original-uid"}


@pytest.mark.parametrize("job", [runner_job(issue_id="other-issue"), runner_job(seq="2")])
def test_cancellation_rejects_job_for_different_dispatch(job):
    batch = Mock()
    batch.read_namespaced_job.return_value = job
    fx = effects.K8sEffects(cfg(), Mock(), batch, "test")
    with pytest.raises(RuntimeError, match="unowned runner"):
        fx.cancel_runner("original-job", issue_id="i1", seq=1)
    batch.delete_namespaced_job.assert_not_called()


def test_outage_keeps_workers_and_backs_off(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    tracker.raise_unavailable = True
    fx = FakeEffects(running={"fc-1"})
    result = tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert result["skipped"] == "tracker_unavailable"
    assert fx.cancelled == [] and fx.cleaned == []  # workers KEPT (FR-010)
    assert kv.get(state.issue_key("i1"))["status"] == "running"
    assert "tracker_unavailable" in fx.names()

    # backoff: the next tick within the window is skipped entirely
    clock.advance(1_000)
    fx2 = FakeEffects()
    assert tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")["skipped"] == "poll_backoff"

    # recovery: window expires, tracker comes back, everything normal — no intervention
    tracker.raise_unavailable = False
    clock.advance(60_000)
    fx3 = FakeEffects(running={"fc-1"})
    result3 = tick(kv, tracker, c, clock.now_ms(), fx3, owner="d1")
    assert "skipped" not in result3
    assert kv.get(state.POLL_BACKOFF_KEY) is None  # success resets


def test_outage_backoff_is_exponential(kv, clock, tracker):
    c = cfg()
    tracker.add_issue("i1", "ENG-1")
    tracker.raise_unavailable = True
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    first = kv.get(state.POLL_BACKOFF_KEY)
    assert first["failures"] == 1
    clock.advance(11_000)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    second = kv.get(state.POLL_BACKOFF_KEY)
    assert second["failures"] == 2
    assert second["next_poll_at"] - clock.now_ms() == 20_000  # 10s·2^(n−1)


def test_exit_to_human_review_releases_claim_keeps_workspace(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    tracker.set_state("i1", "Human Review")  # outside active/terminal ⇒ unknown
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.released == ["i1"]  # workspace KEPT
    assert fx.cleaned == [] and fx.reflected == []  # no cleanup, no retro
    # The claim is released but the RUN is remembered: this is the state every successful
    # issue passes through on its way to Done, and forgetting it here is what stopped the
    # retrospective from ever seeing work that succeeded.
    parked = kv.get(state.issue_key("i1"))
    assert parked is not None and parked["status"] == "parked"
    assert parked.get("runner_call_id") is None  # not owned by anyone


def test_running_issue_in_review_waits_for_runner_self_exit(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    tracker.set_state("i1", "Human Review")
    fx = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert kv.get(state.issue_key("i1")) is not None  # a live runner exits cleanly on its own
    assert fx.released == []


def test_gone_issue_cancels_without_workspace_cleanup(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    set_running(kv, clock)
    tracker.remove_issue("i1")
    fx = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.cancelled == ["fc-1"]
    assert fx.cleaned == []  # workspace kept (FR-012)
    assert "issue_gone" in fx.names()


def test_terminal_fires_retro_before_cleanup_and_aggregates_tokens(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    entry = kv.get(state.issue_key("i1"))
    entry.update(tokens={"input": 5, "output": 5, "total": 10})
    kv.put(state.issue_key("i1"), entry)
    tracker.set_state("i1", "Done")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    events = fx.names()
    assert events.index("retrospective_spawned") < events.index("terminal_cleanup")  # FR-026 ordering
    # the issue's tokens go to METRICS (the global aggregate is fed by the runner
    # in real time — adding them again here would double-count, FR-033)
    metrics = next(v for k, v in kv.data.items() if k.startswith(state.METRICS_PREFIX))
    assert metrics["tokens_total"] == 10
    assert kv.get(state.issue_key("i1")) is None


def test_startup_sweep_cleans_terminal_leftovers(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    tracker.set_state("i1", "Cancelled")
    fx = FakeEffects()
    startup_sweep(kv, tracker, c, clock.now_ms(), fx)
    assert fx.cleaned == ["i1"]
    assert kv.get(state.issue_key("i1")) is None
    assert "startup_sweep_done" in fx.names()


def test_startup_sweep_survives_outage(kv, clock, tracker):
    tracker.raise_unavailable = True
    fx = FakeEffects()
    startup_sweep(kv, tracker, cfg(), clock.now_ms(), fx)
    assert "startup_sweep_skipped" in fx.names()


def test_blocked_issue_reopens_on_ticket_update_or_refresh(kv, clock, tracker):
    c = dispatch_one(kv, clock, tracker)
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="blocked", last_issue_updated_at=None)  # same as the current updated_at
    kv.put(state.issue_key("i1"), entry)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert "blocked_reopened" not in fx.names()  # nothing changed ⇒ stays blocked
    tracker.set_state("i1", "Todo", updated_at="2026-01-02T00:00:00Z")  # a human touched it
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert "blocked_reopened" in fx2.names()


def test_lifecycle_events_are_emitted_per_stage(kv, clock, tracker):
    """FR-032: dispatch, retry, stall, cleanup and retro emit structured events."""
    c = dispatch_one(kv, clock, tracker)
    all_events = []
    set_running(kv, clock)
    clock.advance(c.stall_timeout_ms + 1_000)
    for _ in range(2):
        fx = FakeEffects(running={"fc-1"})
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
        all_events += fx.names()
        clock.advance(c.stall_timeout_ms + 1_000)
    tracker.set_state("i1", "Done")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    all_events += fx.names()
    for expected in ("stall_strike", "stall_killed", "retrospective_spawned", "terminal_cleanup"):
        assert expected in all_events


def test_reassigned_issue_interrupts_execution_keeping_workspace(kv, clock, tracker):
    """FR-012: an issue reassigned mid-execution is interrupted without cleanup."""
    tracker.add_issue("i1", "ENG-i1", assignee="alice", assignee_email="alice@example.com")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    # The claim keeps who the ticket is assigned to; the e-mail names them for the API.
    claimed = kv.get(state.issue_key("i1"))
    assert (claimed["assignee"], claimed["assignee_email"]) == ("alice", "alice@example.com")
    set_running(kv, clock)
    from dataclasses import replace as dc_replace

    tracker._issues["i1"] = dc_replace(tracker._issues["i1"], assignee="bob")
    fx = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.cancelled == ["fc-1"]
    assert fx.cleaned == [] and fx.released == ["i1"]  # workspace kept
    assert "issue_reassigned" in fx.names()
    # the old claim died; if the issue remains eligible (memory does not route by
    # assignee), the SAME tick may re-claim it fresh under the new assignee
    entry = kv.get(state.issue_key("i1"))
    assert entry is None or (entry["assignee"] == "bob" and entry["runner_call_id"] is None)


def test_release_deletes_tokens_so_rework_cycle_redispatches_next_tick(kv, clock, tracker):
    """SC-001: Human Review → Rework redispatches on the NEXT tick — the previous
    cycle's token is deleted along with the claim, with no collision or adoption wait."""
    c = dispatch_one(kv, clock, tracker)
    tracker.set_state("i1", "Human Review")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # releases claim + tokens
    assert kv.get(state.dispatch_key("i1", 1)) is None
    tracker.set_state("i1", "Rework")  # a human requested changes
    clock.advance(c.polling_interval_ms)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert ("i1", 1) in fx.spawned  # ONE interval, no dispatch_token_lost
    assert "dispatch_token_lost" not in fx.names()


def test_lost_token_from_crashed_daemon_is_adopted_after_grace(kv, clock, tracker):
    """Liveness FR-005/FR-006: daemon died between token and entry ⇒ the issue does not get stuck."""
    tracker.add_issue("i1", "ENG-i1")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="retry_queued", retry_at=clock.now_ms(), attempt=1, dispatch_seq=1)
    kv.put(state.issue_key("i1"), entry)
    # another daemon acquired the seq-2 token and died BEFORE spawning/writing the entry
    state.acquire_dispatch_token(kv, "i1", 2, "dead-daemon", clock.now_ms())

    fx1 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx1, owner="d1")
    assert fx1.spawned == [] and "dispatch_token_lost" in fx1.names()

    clock.advance(3 * c.polling_interval_ms)  # grace window (2 ticks) expired
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert ("i1", 1) in fx2.spawned  # token adopted — the issue moves again
    assert "dispatch_token_adopted" in fx2.names()


def test_stall_detection_disabled_when_timeout_nonpositive(kv, clock, tracker):
    """Upstream §6.4: stall_timeout_ms <= 0 disables stall detection entirely."""
    from dataclasses import replace as dc_replace

    c = dc_replace(cfg(), stall_timeout_ms=0)
    tracker.add_issue("i1", "ENG-i1")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    set_running(kv, clock)
    for _ in range(4):
        clock.advance(600_000)
        fx = FakeEffects(running={"fc-1"})
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
        assert fx.cancelled == [] and "stall_strike" not in fx.names()


def test_required_label_removed_midflight_interrupts_keeping_workspace(kv, clock, tracker):
    """Upstream §8.5: a removed required label stops work; workspace is kept."""
    from dataclasses import replace as dc_replace

    c = dc_replace(cfg(), required_labels=("symphony",))
    tracker.add_issue("i1", "ENG-i1", labels=("symphony",))
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    set_running(kv, clock)
    from dataclasses import replace as issue_replace

    tracker._issues["i1"] = issue_replace(tracker._issues["i1"], labels=())
    fx = FakeEffects(running={"fc-1"})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.cancelled == ["fc-1"]
    assert fx.released == ["i1"] and fx.cleaned == []
    assert "issue_unroutable" in fx.names()


# -- visibility of endings the agent cannot narrate -------------------------


def test_a_stall_kill_is_announced_on_the_ticket(kv):
    """The agent reports its own progress; it cannot report the ways it stops existing.
    A killed attempt leaves the ticket in an active state with no new comment, which from
    the board is indistinguishable from work still under way — the exact ambiguity that
    makes a stuck ticket expensive to notice."""
    c = cfg(stall_timeout_ms=1000)
    kv.put(
        state.issue_key("i1"),
        {
            "status": "running", "attempt": 2, "dispatch_seq": 2, "identifier": "ENG-901",
            "runner_call_id": "fc-1", "last_event_at": 0,
            "stall_strikes": 1, "stall_seen_event_at": 0,
        },
    )
    fx = FakeEffects()
    fx.running.add("fc-1")
    issue = MemoryTracker().add_issue("i1", "ENG-901")
    orchestrator._check_stall(kv, MemoryTracker(), issue, kv.get(state.issue_key("i1")), c, 10_000, fx)

    assert "stall_killed" in fx.names()
    reported = getattr(fx, "reported", [])
    assert reported, "the kill left no trace on the ticket"
    issue_id, message = reported[0]
    assert issue_id == "i1"
    assert "Attempt 2" in message, (
        "must name the attempt that DIED — the counter has already advanced by now, so "
        "reading it after the requeue names the one about to start"
    )
    assert "1 seconds" in message, (
        "must state the threshold that was exceeded, in whatever unit reads naturally at "
        "that scale — 'more than 0 minutes' is what a bare integer division produces"
    )


def test_a_lost_report_never_costs_the_retry(kv):
    """Visibility is worth less than the recovery it describes: a tracker outage must
    degrade the note, never the requeue that follows it."""
    class Mute(FakeEffects):
        def report_to_ticket(self, issue_id, message):
            raise RuntimeError("tracker down")

    c = cfg(stall_timeout_ms=1000)
    kv.put(
        state.issue_key("i1"),
        {
            "status": "running", "attempt": 1, "dispatch_seq": 1, "identifier": "ENG-901",
            "runner_call_id": None, "last_event_at": 0,
            "stall_strikes": 1, "stall_seen_event_at": 0,
        },
    )
    fx = Mute()
    try:
        orchestrator._check_stall(kv, MemoryTracker(), MemoryTracker().add_issue("i1", "ENG-901"),
                                  kv.get(state.issue_key("i1")), c, 10_000, fx)
    except RuntimeError:
        pass  # the effect raised; what matters is the state it left behind
    assert kv.get(state.issue_key("i1"))["status"] == "retry_queued"


def test_the_happy_path_still_reaches_the_retrospective(kv, clock, tracker):
    """Todo → In Progress → In Review → Done is the path every SUCCESSFUL issue takes,
    and the review state is parked (in neither list). Releasing the claim there used to
    delete the entry, so by the time the ticket reached Done nothing tracked it and
    `_finalize_terminal` never ran. The loop therefore learned only from abandoned work —
    never from work that landed."""
    tracker.add_issue("i1", "ENG-1", "Todo", labels=("symphony",))
    c = cfg()
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, "d1")
    assert ("i1", 1) in fx.spawned

    tracker.set_state("i1", "In Review")  # human takes over — claim released, work parked
    clock.advance(60_000)
    tick(kv, tracker, c, clock.now_ms(), fx, "d1")
    assert fx.reflected == [], "parking is not an ending"

    tracker.set_state("i1", "Done")  # merged
    clock.advance(60_000)
    tick(kv, tracker, c, clock.now_ms(), fx, "d1")
    assert fx.reflected == ["i1"], "a merged issue must still produce a retrospective"


def test_a_parked_issue_returning_to_work_is_dispatched_not_blocked(kv, clock, tracker):
    """Remembering a run must not look like owning it: Human Review → Rework has to
    redispatch on the next tick, or the parked entry becomes a permanent lock."""
    tracker.add_issue("i1", "ENG-1", "Todo", labels=("symphony",))
    c = cfg()
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, "d1")

    tracker.set_state("i1", "In Review")
    clock.advance(60_000)
    tick(kv, tracker, c, clock.now_ms(), fx, "d1")

    tracker.set_state("i1", "Rework")
    clock.advance(60_000)
    tick(kv, tracker, c, clock.now_ms(), fx, "d1")
    assert len(fx.spawned) == 2, "the parked entry blocked the claim instead of yielding"


def test_report_to_ticket_comments_through_the_adapter(monkeypatch):
    posted = []
    adapter = SimpleNamespace(comment=lambda issue, body: posted.append((issue, body)) or "c1")
    monkeypatch.setattr(effects, "make_tracker", lambda cfg: adapter)
    fx = effects.K8sEffects(cfg(), Mock(), Mock(), "test")
    fx.report_to_ticket("i1", "attempt 2 failed")
    assert posted == [("i1", "attempt 2 failed")]
    adapter.comment = Mock(side_effect=RuntimeError("tracker down"))
    fx.report_to_ticket("i1", "best effort")  # never raises
