"""FR-004 (slots) / FR-005 (claim + per-attempt token) / FR-009 (cooldown + refresh)."""

from symphony import state
from symphony.config import parse_workflow
from symphony.orchestrator import tick


class FakeEffects:
    def __init__(self, running: set[str] | None = None):
        self.spawned: list[tuple[str, int]] = []
        self.events: list[tuple[str, dict]] = []
        self.cancelled: list[str] = []
        self.killed: list = []
        self.cleaned: list[str] = []
        self.released: list[str] = []
        self.reflected: list[str] = []
        self.running = running or set()

    def spawn_runner(self, issue, attempt, seq=1):
        self.spawned.append((issue.id, attempt))
        self.spawned_seqs = getattr(self, "spawned_seqs", [])
        self.spawned_seqs.append((issue.id, attempt, seq))

    def cancel_runner(self, call_id, *, issue_id, seq):
        self.cancelled.append(call_id)

    def is_call_running(self, call_id):
        return call_id in self.running

    def kill_agent_process(self, sandbox_name, *, issue_id=None, seq=None):
        self.killed.append(sandbox_name)

    def cleanup_workspace(self, issue_id, entry):
        self.cleaned.append(issue_id)

    def release_workspace(self, issue_id, entry):
        self.released.append(issue_id)

    def spawn_reflector(self, issue_id, entry):
        self.reflected.append(issue_id)

    def emit(self, event, **fields):
        self.events.append((event, fields))

    def report_to_ticket(self, issue_id, message):
        self.reported = getattr(self, "reported", [])
        self.reported.append((issue_id, message))

    def names(self):
        return [e for e, _ in self.events]


def cfg(**over):
    base = "---\ntracker: {kind: memory}\nkubernetes: {redispatch_cooldown_s: 900}\n---\nx"
    c = parse_workflow(base)
    if over:
        from dataclasses import replace

        c = replace(c, **over)
    return c


def test_fresh_dispatch_claims_token_and_spawns(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    fx = FakeEffects()
    result = tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert result["dispatched"] == 1
    assert fx.spawned == [("i1", 1)]
    assert kv.get(state.issue_key("i1"))["status"] == "claimed"
    assert kv.get(state.dispatch_key("i1", 1)) is not None


def test_no_duplicate_dispatch_on_second_tick(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    fx = FakeEffects()
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    clock.advance(30_000)
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 1)]  # claim already exists ⇒ ineligible


def test_concurrency_cap_and_deterministic_overflow(kv, clock, tracker):
    for i in range(1, 8):
        tracker.add_issue(f"i{i}", f"ENG-{i}", created_at=f"2026-01-0{i}T00:00:00Z")
    fx = FakeEffects()
    tick(kv, tracker, cfg(max_concurrent_agents=3), clock.now_ms(), fx, owner="d1")
    assert len(fx.spawned) == 3
    assert [s[0] for s in fx.spawned] == ["i1", "i2", "i3"]  # oldest first


def test_per_state_override_cap(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1", state="Rework")
    tracker.add_issue("i2", "ENG-2", state="Rework")
    tracker.add_issue("i3", "ENG-3", state="Todo")
    fx = FakeEffects()
    # parse-time normalization casefolds override keys; dataclass replace bypasses
    # the parser, so mirror the normalized form here
    c = cfg(max_concurrent_agents_by_state={"rework": 1})
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    states = {s[0] for s in fx.spawned}
    assert "i3" in states
    assert len([s for s in fx.spawned if s[0] in ("i1", "i2")]) == 1


def test_unconfirmed_stop_retains_global_capacity(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1")
    tracker.add_issue("i2", "ENG-2")
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="interrupting", bridge_token=None)
    kv.put(state.issue_key("i1"), entry)
    fx = FakeEffects()
    result = tick(kv, tracker, cfg(max_concurrent_agents=1), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == []
    assert result["busy"] == 1


def test_unconfirmed_stop_retains_per_state_capacity(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1", state="Rework")
    tracker.add_issue("i2", "ENG-2", state="Rework")
    tracker.add_issue("i3", "ENG-3", state="Todo")
    state.claim_issue(kv, "i1", "ENG-1", clock.now_ms())
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="interrupting", bridge_token=None)
    kv.put(state.issue_key("i1"), entry)
    fx = FakeEffects()
    tick(kv, tracker, cfg(max_concurrent_agents_by_state={"rework": 1}), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i3", 1)]


def test_cooldown_holds_parked_issue_and_refresh_bypasses(kv, clock, tracker):
    issue = tracker.add_issue("i1", "ENG-1", updated_at="2026-01-01T00:00:00Z")
    fx = FakeEffects()
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    # runner exited cleanly with the ticket unchanged
    entry = kv.get(state.issue_key("i1"))
    entry.update(
        status="retry_queued",
        retry_at=clock.now_ms(),
        last_clean_exit_at=clock.now_ms(),
        last_issue_updated_at=issue.updated_at,
    )
    kv.put(state.issue_key("i1"), entry)

    clock.advance(60_000)  # < the 900s cooldown
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert fx2.spawned == []
    assert "cooldown_hold" in fx2.names()

    kv.put(state.REFRESH_KEY, True)  # POST /refresh bypasses the cooldown
    fx3 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx3, owner="d1")
    assert ("i1", 1) in fx3.spawned


def test_cooldown_expires_after_window(kv, clock, tracker):
    issue = tracker.add_issue("i1", "ENG-1", updated_at="2026-01-01T00:00:00Z")
    c = cfg()
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    entry = kv.get(state.issue_key("i1"))
    entry.update(
        status="retry_queued",
        retry_at=clock.now_ms(),
        last_clean_exit_at=clock.now_ms(),
        last_issue_updated_at=issue.updated_at,
    )
    kv.put(state.issue_key("i1"), entry)
    clock.advance(901_000)
    fx2 = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx2, owner="d1")
    assert ("i1", 1) in fx2.spawned


def test_a_multi_repo_ticket_dispatches_with_every_repository_recorded(kv, clock, tracker):
    """The run's record carries the whole set, primary first: the review watch, the
    reflector and the workspace layout all read it from here."""
    tracker.add_issue("i1", "ENG-1", labels=("symphony", "repo:acme/api", "repo:acme/web"))
    fx = FakeEffects()
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 1)]
    entry = kv.get(state.issue_key("i1"))
    assert entry["target_repo"] == "acme/api"
    assert entry["target_repos"] == ["acme/api", "acme/web"]


def test_a_cross_owner_ticket_is_never_dispatched(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1", labels=("repo:acme/api", "repo:other/web"))
    fx = FakeEffects()
    tick(kv, tracker, cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [] and kv.get(state.issue_key("i1")) is None


def test_rework_dispatches_count_against_the_configured_rework_state(kv, clock):
    from symphony.trackers.memory import MemoryTracker

    c = cfg(active_states=("Todo", "Fix It"), tracker_states={**cfg().tracker_states, "rework": "Fix It"})
    tracker = MemoryTracker(active_states=c.active_states)
    tracker.add_issue("i1", "ENG-1", state="Fix It")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    metrics = next(v for k, v in kv.data.items() if k.startswith(state.METRICS_PREFIX))
    assert metrics["rework"] == 1


# -- refresh is never a no-op ---------------------------------------------------


def _clean_exit(kv, tracker, clock, c):
    """Dispatch, then the runner's own clean-exit bookkeeping: continuation queued ~1 s out,
    ticket unchanged — exactly what arms the anti-runaway cooldown."""
    from symphony.orchestrator import mark_clean_continuation

    issue = tracker.add_issue("i1", "ENG-1", updated_at="2026-01-01T00:00:00Z")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="running", runner_call_id="job-1")
    mark_clean_continuation(kv, "i1", entry, clock.now_ms(), issue.updated_at)
    return kv.get(state.issue_key("i1"))


def test_a_refresh_before_the_continuation_is_due_still_releases_it_now(kv, clock, tracker):
    """Regression (kind certification): the refresh tick ran before the 1 s continuation
    was due, consumed the request as a no-op, and the continuation then sat out the whole
    900 s cooldown — the human's refresh did nothing."""
    c = cfg()
    entry = _clean_exit(kv, tracker, clock, c)
    seq = entry["dispatch_seq"]
    clock.advance(100)  # the continuation is due at +1 s: not yet
    kv.put(state.REFRESH_KEY, {"commandId": "c1"})  # what process_commands writes

    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 1)]  # dispatched by the refresh tick itself
    assert "cooldown_hold" not in fx.names()
    after = kv.get(state.issue_key("i1"))
    assert after["dispatch_seq"] == seq + 1 and after["status"] == "claimed"
    assert not after.get("refresh_grant")  # one-shot: spent by this dispatch
    assert kv.get(state.dispatch_key("i1", seq + 1)) is not None  # still fenced by token


def test_the_refresh_grant_is_one_shot(kv, clock, tracker):
    """A refresh releases the run it found queued, once; the next clean exit with the
    ticket still unchanged is held by the anti-runaway cooldown again."""
    from symphony.orchestrator import mark_clean_continuation

    c = cfg()
    _clean_exit(kv, tracker, clock, c)
    kv.put(state.REFRESH_KEY, True)
    first = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), first, owner="d1")
    assert first.spawned == [("i1", 1)] and kv.get(state.REFRESH_KEY) is None

    entry = kv.get(state.issue_key("i1"))
    entry.update(status="running", runner_call_id="job-2")
    mark_clean_continuation(kv, "i1", entry, clock.now_ms(), "2026-01-01T00:00:00Z")
    clock.advance(2_000)
    later = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), later, owner="d1")
    assert later.spawned == [] and "cooldown_hold" in later.names()


def test_a_refresh_during_backoff_waits_for_the_backoff_then_skips_the_cooldown(kv, clock, tracker):
    c = cfg()
    entry = _clean_exit(kv, tracker, clock, c)
    entry.update(status="retry_queued", retry_at=clock.now_ms() + 30_000,
                 failure_summary="stall detected")
    kv.put(state.issue_key("i1"), entry)

    kv.put(state.REFRESH_KEY, True)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == []  # backoff is the retry policy; refresh does not shorten it
    assert kv.get(state.issue_key("i1"))["refresh_grant"] is True

    clock.advance(31_000)  # due, still well inside the 900 s cooldown
    due = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), due, owner="d1")
    assert due.spawned == [("i1", 1)] and "cooldown_hold" not in due.names()
