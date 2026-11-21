"""Pure orchestrator tick logic: reconcile → retries → dispatch.

No Kubernetes client imports. Side effects go through the `Effects` interface; shared
state through the KV interface (state.py); time is always injected.
Every possible duplication (double daemon, repeated tick, platform re-execution)
collides on the put-if-absent dispatch token — only one wins (§3.7.1, constitution IV).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from . import discovery, intake, prwatch, state, telemetry, triage
from .config import Config, known_runtimes, resolve_workspace
from .repos import validate_runtime_component
from .trackers.base import Issue, TrackerAdapter, TrackerUnavailable

ORPHAN_CLAIM_TICKS = 2
STALL_STRIKES_TO_KILL = 2
CONTINUATION_DELAY_MS = 1_000


class Effects(Protocol):
    """Side-effect boundary of the tick (implemented in app.py; faked in tests)."""

    def spawn_runner(self, issue: Issue, attempt: int, seq: int) -> None: ...

    def cancel_runner(self, call_id: str, *, issue_id: str, seq: int) -> None: ...

    def is_call_running(self, call_id: str) -> bool: ...

    def kill_agent_process(
        self, sandbox_name: str | None, *, issue_id: str | None = None, seq: int | None = None
    ) -> None: ...

    def cleanup_workspace(self, issue_id: str, entry: dict) -> None: ...

    def release_workspace(self, issue_id: str, entry: dict) -> None: ...

    def spawn_reflector(self, issue_id: str, entry: dict) -> None: ...

    def emit(self, event: str, **fields) -> None: ...

    def open_pulls(self, repo: str) -> list[dict]:
        """Open pull requests on the issue's repository (feature 005). Read-only, host-side."""
        ...

    def pr_conversation(self, repo: str, number: int) -> list[dict]:
        """Every comment on a pull request. Read-only, host-side."""
        ...

    def report_to_ticket(self, issue_id: str, message: str) -> None:
        """Tell the humans watching the board that an attempt ended badly.

        The agent reports its own progress, but it cannot report the ways it stops
        existing: a stall kill and a dead runner both leave the ticket sitting in an
        active state with no new comment, indistinguishable from work in progress. This
        is the only path by which those become visible without reading logs.
        """
        ...

    def notify(self, event: str, text: str, *, link: str | None = None) -> None:
        """Post a notice to the team's channel (`symphony.notify`). Best effort: a channel
        outage never fails a tick."""
        ...

    def failure_evidence(self, call_id: str) -> dict:
        """What the runtime knows about a failed runner call (`triage.record` reads `job`,
        `reason`, `container`, `error` and `lines`). Best effort; `{}` when nothing."""
        ...


def compute_backoff_ms(failed_attempt: int, cap_ms: int) -> int:
    """Backoff per upstream spec §8.5: min(10s × 2^(attempt−1), cap)."""
    return min(10_000 * (2 ** max(failed_attempt - 1, 0)), cap_ms)


def _has_required_labels(issue: Issue, cfg: Config) -> bool:
    issue_labels = {label.casefold() for label in issue.labels}
    return all(label.casefold() in issue_labels for label in cfg.required_labels)


def eligible(issue: Issue, cfg: Config, tracked_ids: set[str]) -> bool:
    """FR-002: objective criteria; the orchestrator does not interpret blockers —
    `dispatchable` comes precomputed from the adapter. Labels are compared
    case-insensitively and the description may be empty (upstream spec §8.2 compliance)."""
    return (
        bool(issue.title)
        and issue.state_type == "active"
        and issue.dispatchable
        and _has_required_labels(issue, cfg)
        and issue.id not in tracked_ids
    )


def _priority_bucket(priority: int | None) -> tuple[int, int]:
    if priority is not None and 1 <= priority <= 4:
        return (0, priority)
    return (1, 0)  # other values and None last (upstream spec §8.2)


def _created_key(created_at: str | None) -> tuple[int, str]:
    return (0, created_at) if created_at else (1, "")  # None last


def dispatch_order(issues: list[Issue]) -> list[Issue]:
    """FR-003: priority → created_at (oldest first) → lexicographic identifier."""
    return sorted(
        issues,
        key=lambda i: (_priority_bucket(i.priority), _created_key(i.created_at), i.identifier),
    )


RETRIES_EXHAUSTED = "retries_exhausted"


def retry_after_failure(entry: dict, cfg: Config, now_ms: int, failure_summary: str) -> dict:
    """The fields a failed attempt sets: it consumes the attempt and applies backoff; the
    summary feeds the L1 loop (FR-006). Pure, so a fenced writer can apply it atomically.

    After `agent.max_failed_attempts` failures in a row the ticket is `blocked` instead of
    queued again: a cause that does not go away (an expired credential, a broken image)
    must reach a human, not loop for days. The daemon reports it on its next tick."""
    failed = entry["attempt"]
    failures = entry.get("failures", 0) + 1
    fields = {
        "attempt": failed + 1,
        "failures": failures,
        "runner_call_id": None,
        "last_call_id": entry.get("runner_call_id") or entry.get("last_call_id"),
        "stall_strikes": 0,
        "failure_summary": failure_summary,
        "updated_at": now_ms,
    }
    if cfg.max_failed_attempts and failures >= cfg.max_failed_attempts:
        return {**fields, "status": "blocked", "retry_at": None, "blocked_reason": RETRIES_EXHAUSTED,
                "exhaustion_reported_at": None}
    return {**fields, "status": "retry_queued",
            "retry_at": now_ms + compute_backoff_ms(failed, cfg.max_retry_backoff_ms)}


# The report's own comment moves the ticket's `updated_at` a moment after it is sent; an
# update later than this counts as a person touching the ticket.
REPORT_ECHO_MS = 60_000


def _touched_after_report(issue: Issue, entry: dict) -> bool:
    reported_at = entry.get("exhaustion_reported_at")
    return bool(reported_at) and intake.changed_since(issue, reported_at + REPORT_ECHO_MS)


def mark_retry_after_failure(
    kv: state.KV, issue_id: str, entry: dict, cfg: Config, now_ms: int, failure_summary: str
) -> None:
    entry.update(retry_after_failure(entry, cfg, now_ms, failure_summary))
    kv.put(state.issue_key(issue_id), entry)


def mark_clean_continuation(
    kv: state.KV, issue_id: str, entry: dict, now_ms: int, issue_updated_at: str | None
) -> None:
    """Clean continuation: rescheduled in ~1s, SAME attempt, no backoff (FR-006)."""
    entry.update(
        status="retry_queued",
        retry_at=now_ms + CONTINUATION_DELAY_MS,
        runner_call_id=None,
        stall_strikes=0,
        last_clean_exit_at=now_ms,
        failures=0,
        last_issue_updated_at=issue_updated_at,
        failure_summary=None,
        updated_at=now_ms,
    )
    kv.put(state.issue_key(issue_id), entry)


def _record_poll_failure(kv: state.KV, now_ms: int, cap_ms: int = 300_000) -> int:
    pb = kv.get(state.POLL_BACKOFF_KEY) or {"failures": 0}
    failures = pb["failures"] + 1
    delay = min(10_000 * (2 ** (failures - 1)), cap_ms)
    kv.put(state.POLL_BACKOFF_KEY, {"failures": failures, "next_poll_at": now_ms + delay})
    return delay


def _delete_dispatch_tokens(kv: state.KV, issue_id: str, up_to_seq: int) -> None:
    """On terminal cleanup: stale tokens must not block a future reopen of the issue."""
    state.remember_dispatch_seq(kv, issue_id, up_to_seq)
    for seq in range(1, up_to_seq + 1):
        kv.delete(state.dispatch_key(issue_id, seq))


def _finalize_terminal(
    kv: state.KV, issue_id: str, entry: dict, issue: Issue | None, cfg: Config, now_ms: int, fx: Effects
) -> None:
    """Mandatory order (FR-012/FR-026): retro (data preserved) → cancel → clean up."""
    yyyymm = _yyyymm(now_ms)
    if (
        cfg.learning_enabled
        and issue is not None
        and issue.state in cfg.effective_reflect_on
        and state.acquire_retro_token(kv, issue_id, now_ms)
    ):
        fx.spawn_reflector(issue_id, dict(entry))
        fx.emit("retrospective_spawned", issue_id=issue_id)
    # the issue's accumulated tokens go into the monthly METRICS; the global
    # aggregate (totals:tokens) was already fed in real time by the runner's
    # flushes — adding it again here would double-count (FR-033)
    state.bump_metric(kv, yyyymm, "tokens_total", by=(entry.get("tokens") or {}).get("total", 0))
    state.bump_metric(kv, yyyymm, "runs")
    state.bump_metric(kv, yyyymm, "attempts", by=entry.get("attempt", 1))
    if entry.get("runner_call_id"):
        fx.cancel_runner(entry["runner_call_id"], issue_id=issue_id, seq=entry.get("dispatch_seq", 1))
    fx.kill_agent_process(entry.get("sandbox_name"))
    fx.cleanup_workspace(issue_id, dict(entry))
    _delete_dispatch_tokens(kv, issue_id, max(entry.get("attempt", 1), entry.get("dispatch_seq", 1)))
    kv.delete(state.issue_key(issue_id))
    kv.delete(state.telemetry_key(issue_id))  # FR-017: no residue outlives the tracking entry
    fx.emit("terminal_cleanup", issue_id=issue_id)


def _yyyymm(now_ms: int) -> str:
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(now_ms / 1000, tz=_dt.UTC)
    return f"{dt.year:04d}{dt.month:02d}"


def startup_sweep(kv: state.KV, tracker: TrackerAdapter, cfg: Config, now_ms: int, fx: Effects) -> None:
    """Terminal sweep required on daemon start/respawn (FR-011)."""
    try:
        terminals = tracker.fetch_issues_by_states(list(cfg.terminal_states))
    except TrackerUnavailable:
        fx.emit("startup_sweep_skipped", reason="tracker_unavailable")
        return
    tracked = state.tracked_issues(kv)
    for issue in terminals:
        entry = tracked.get(issue.id)
        if entry is not None:
            _finalize_terminal(kv, issue.id, entry, issue, cfg, now_ms, fx)
        else:
            fx.cleanup_workspace(issue.id, {"identifier": issue.identifier, "sandbox_name": None})
    fx.emit("startup_sweep_done", terminal_count=len(terminals))


def _slots_state(tracked: dict[str, dict], issues_by_id: dict[str, Issue]) -> tuple[int, dict[str, int]]:
    busy = 0
    per_state: dict[str, int] = {}
    for issue_id, entry in tracked.items():
        if entry.get("status") in ("claimed", "running", "interrupting"):
            busy += 1
            issue = issues_by_id.get(issue_id)
            if issue is not None:
                per_state[issue.state] = per_state.get(issue.state, 0) + 1
    return busy, per_state


def _has_slot(cfg: Config, busy: int, per_state: dict[str, int], issue_state: str) -> bool:
    if busy >= cfg.max_concurrent_agents:
        return False
    cap = cfg.max_concurrent_agents_by_state.get(issue_state.strip().casefold())
    if cap is not None and per_state.get(issue_state, 0) >= cap:
        return False
    return True


def tick(
    kv: state.KV,
    tracker: TrackerAdapter,
    cfg: Config,
    now_ms: int,
    fx: Effects,
    owner: str,
    *,
    admission_open: Callable[[], bool] = lambda: True,
    wake_reasons: Iterable[str] = (),
) -> dict:
    """One orchestration pass. `wake_reasons` are the `wake(daemon)` reasons that caused
    it (empty for a safety poll); a GitHub comment wake makes the named pull request's
    review watch due now instead of at its throttle."""
    state.heartbeat(kv, now_ms)
    # Repositories the workflow leaves unconfigured get their discovered runtime/components.
    cfg = discovery.effective(cfg, kv.get(discovery.DISCOVERY_KEY))
    woken = prwatch.parse_comment_wakes(wake_reasons)

    refresh = bool(kv.get(state.REFRESH_KEY))
    if refresh:
        # Granted BEFORE the request is consumed: a refresh that lands while a continuation
        # is not yet due must still reach it, or it would be spent as a no-op and the
        # continuation would then sit out the whole cooldown (the human asked for "now").
        granted = _grant_refresh(kv, state.tracked_issues(kv), now_ms)
        kv.delete(state.REFRESH_KEY)
        fx.emit("refresh_consumed", granted=granted)

    pb = kv.get(state.POLL_BACKOFF_KEY)
    if pb and now_ms < pb.get("next_poll_at", 0) and not refresh:
        return {"skipped": "poll_backoff"}

    tracked = state.tracked_issues(kv)
    held = state.held_issues(kv)  # internals §12: a human holds these tickets

    # -- reconciliation ---------------------------------------------------
    issues_by_id: dict[str, Issue] = {}
    if tracked:
        try:
            fetched = tracker.fetch_issues_by_ids(list(tracked.keys()))
        except TrackerUnavailable:
            delay = _record_poll_failure(kv, now_ms)
            fx.emit("tracker_unavailable", phase="reconcile", backoff_ms=delay)
            return {"skipped": "tracker_unavailable"}  # workers KEPT (FR-010)
        issues_by_id = {i.id: i for i in fetched}

        for issue_id, entry in list(tracked.items()):
            if entry.get("status") == "interrupting":
                continue  # durable command reconciliation owns mandatory stop/recovery
            issue = issues_by_id.get(issue_id)
            if issue is None:
                # gone/reassigned: cancel WITHOUT cleaning the workspace (FR-012)
                if entry.get("runner_call_id"):
                    fx.cancel_runner(entry["runner_call_id"], issue_id=issue_id, seq=entry.get("dispatch_seq", 1))
                fx.kill_agent_process(entry.get("sandbox_name"))
                fx.release_workspace(issue_id, dict(entry))
                _release_claim(kv, issue_id, entry)
                del tracked[issue_id]
                fx.emit("issue_gone", issue_id=issue_id)
            elif issue.state_type == "terminal":
                _finalize_terminal(kv, issue_id, entry, issue, cfg, now_ms, fx)
                del tracked[issue_id]
            elif issue_id in held:
                _hold_run(kv, issue_id, entry, now_ms, fx)
            elif issue.state_type == "unknown" or issue.state not in cfg.active_states:
                # e.g. In Review / Merging (the merger owns the ticket) — release the claim
                # while KEEPING the workspace (FR-014), no retro
                if entry.get("runner_call_id") and fx.is_call_running(entry["runner_call_id"]):
                    continue  # the runner detects it and exits cleanly on its own
                # This branch is re-entered on EVERY tick for as long as the issue sits
                # parked, so anything with an outward effect needs a durable marker rather
                # than the state itself. `session_reported` survives an answer run (which
                # takes the entry out of parked and back, and would re-fire a status-based
                # guard) and dies with the entry when real work resumes — which is exactly
                # what re-arms the report for the next handover (FR-015/FR-016).
                if not entry.get("session_reported"):
                    fx.release_workspace(issue_id, dict(entry))
                    _park_claim(kv, issue_id, entry, now_ms)
                    _report_session(kv, issue_id, entry, fx)
                    entry = kv.get(state.issue_key(issue_id)) or entry
                    entry["session_reported"] = True
                    kv.put(state.issue_key(issue_id), entry)
                    fx.emit("claim_released", issue_id=issue_id, issue_state=issue.state)
                elif entry.get("status") != PARKED:
                    _park_claim(kv, issue_id, entry, now_ms)  # e.g. after an answer run
                else:
                    _watch_review(kv, issue_id, entry, issue, cfg, now_ms, fx, admission_open, woken)
            elif entry.get("assignee") is not None and issue.assignee != entry.get("assignee"):
                # reassigned mid-run: interrupt WITHOUT cleaning the workspace (FR-012)
                if entry.get("runner_call_id"):
                    fx.cancel_runner(entry["runner_call_id"], issue_id=issue_id, seq=entry.get("dispatch_seq", 1))
                fx.kill_agent_process(entry.get("sandbox_name"))
                fx.release_workspace(issue_id, dict(entry))
                _release_claim(kv, issue_id, entry)
                del tracked[issue_id]
                fx.emit("issue_reassigned", issue_id=issue_id, new_assignee=issue.assignee)
            elif not _has_required_labels(issue, cfg):
                # required label removed mid-flight (upstream §8.5): stop work, keep workspace
                if entry.get("runner_call_id"):
                    fx.cancel_runner(entry["runner_call_id"], issue_id=issue_id, seq=entry.get("dispatch_seq", 1))
                fx.kill_agent_process(entry.get("sandbox_name"))
                fx.release_workspace(issue_id, dict(entry))
                _release_claim(kv, issue_id, entry)
                del tracked[issue_id]
                fx.emit("issue_unroutable", issue_id=issue_id)
            elif entry.get("status") == "running":
                _check_stall(kv, tracker, issue, entry, cfg, now_ms, fx)
            elif entry.get("status") == "claimed":
                age_ticks = (now_ms - entry.get("claimed_at", now_ms)) / max(cfg.polling_interval_ms, 1)
                if not entry.get("runner_call_id") and age_ticks > ORPHAN_CLAIM_TICKS:
                    # orphaned claim: reopened WITHOUT burning an attempt (FR-012)
                    entry.update(status="retry_queued", retry_at=now_ms, updated_at=now_ms)
                    kv.put(state.issue_key(issue_id), entry)
                    fx.emit("orphan_claim_reopened", issue_id=issue_id, attempt=entry["attempt"])
            elif entry.get("status") == "blocked":
                exhausted = entry.get("blocked_reason") == RETRIES_EXHAUSTED
                revoked = entry.get("admin_authorization_revoked")
                if exhausted and not refresh and not entry.get("exhaustion_reported_at"):
                    _report_exhausted(kv, tracker, issue, entry, cfg, now_ms, fx)
                    entry.update(exhaustion_reported_at=now_ms, updated_at=now_ms)
                    kv.put(state.issue_key(issue_id), entry)
                elif not revoked and (refresh or (
                    _touched_after_report(issue, entry) if exhausted
                    else issue.updated_at != entry.get("last_issue_updated_at")
                )):
                    entry.update(status="retry_queued", retry_at=now_ms, updated_at=now_ms, failures=0,
                                 blocked_reason=None, exhaustion_reported_at=None)
                    kv.put(state.issue_key(issue_id), entry)
                    fx.emit("blocked_reopened", issue_id=issue_id)

    busy, per_state = _slots_state(tracked, issues_by_id)
    dispatched = 0
    if not admission_open():
        return {"dispatched": 0, "tracked": len(tracked), "busy": busy, "paused": True}

    # -- due retries ------------------------------------------------------
    for issue_id, entry in tracked.items():
        if not admission_open():
            break
        if entry.get("status") != "retry_queued" or (entry.get("retry_at") or 0) > now_ms:
            continue
        if issue_id in held:
            continue  # retries and continuations wait for the release
        issue = issues_by_id.get(issue_id)
        if issue is None or not issue.dispatchable or not _has_required_labels(issue, cfg):
            fx.emit("retry_held", issue_id=issue_id, reason="not_routable")
            continue
        if _cooldown_holds(entry, issue, cfg, now_ms, refresh or bool(entry.get("refresh_grant"))):
            fx.emit("cooldown_hold", issue_id=issue_id)
            continue
        if not _has_slot(cfg, busy, per_state, issue.state):
            continue
        # invariant: on each failure, the new attempt is ≤ dispatch_seq+1 (the failed
        # attempt was dispatched with seq ≥ the previous attempt) — seq+1 never collides
        seq = entry.get("dispatch_seq", entry["attempt"]) + 1
        if state.acquire_dispatch_token(kv, issue_id, seq, owner, now_ms):
            entry.update(
                status="claimed", claimed_at=now_ms, runner_call_id=None,
                dispatch_seq=seq, runs=entry.get("runs", entry["attempt"]) + 1, updated_at=now_ms,
                refresh_grant=None,
            )
            kv.put(state.issue_key(issue_id), entry)
            if not admission_open():
                continue
            fx.spawn_runner(issue, entry["attempt"], seq)
            fx.emit("redispatch", issue_id=issue_id, attempt=entry["attempt"], seq=seq)
            _record_dispatch_metrics(kv, issue, cfg, now_ms)
            _record_retry(kv, entry, now_ms)
            busy += 1
            per_state[issue.state] = per_state.get(issue.state, 0) + 1
            dispatched += 1
        else:
            # liveness: if the token owner died BEFORE spawning (entry stuck in
            # retry_queued for >2 ticks with the same lost seq), we adopt the token —
            # a real runner would have written status=running immediately
            lost_since = entry.get("token_lost_since")
            if lost_since is None:
                entry.update(token_lost_since=now_ms)
                kv.put(state.issue_key(issue_id), entry)
                fx.emit("dispatch_token_lost", issue_id=issue_id, attempt=entry["attempt"])
            elif now_ms - lost_since > 2 * cfg.polling_interval_ms:
                entry.update(
                    status="claimed", claimed_at=now_ms, runner_call_id=None,
                    dispatch_seq=seq, runs=entry.get("runs", entry["attempt"]) + 1,
                    token_lost_since=None, updated_at=now_ms, refresh_grant=None,
                )
                kv.put(state.issue_key(issue_id), entry)
                if not admission_open():
                    continue
                fx.spawn_runner(issue, entry["attempt"], seq)
                fx.emit("dispatch_token_adopted", issue_id=issue_id, seq=seq)
                _record_dispatch_metrics(kv, issue, cfg, now_ms)
                _record_retry(kv, entry, now_ms)
                busy += 1
                per_state[issue.state] = per_state.get(issue.state, 0) + 1
                dispatched += 1

    # -- poll + new dispatch ----------------------------------------------
    try:
        active = tracker.fetch_issues_by_states(list(cfg.active_states))
    except TrackerUnavailable:
        delay = _record_poll_failure(kv, now_ms)
        fx.emit("tracker_unavailable", phase="poll", backoff_ms=delay)
        return {"skipped": "tracker_unavailable", "dispatched": dispatched}
    kv.delete(state.POLL_BACKOFF_KEY)  # success resets the poll backoff

    # A parked issue is remembered, not owned: it holds no slot and must not block a
    # dispatch when the ticket comes back to an active state.
    tracked_ids = {iid for iid, e in tracked.items() if e.get("status") != PARKED}
    valid_runtimes = known_runtimes(cfg)
    # Tickets that are Symphony's already: the intake verdict is only for the others.
    owned = set(tracked) | set(held)
    routing: dict[str, str] = {}
    candidates = []
    for i in active:
        if i.id in held or not eligible(i, cfg, tracked_ids):
            continue
        rc_reason = validate_runtime_component(
            i.labels, i.target_repo, cfg.components, valid_runtimes
        )
        if rc_reason:
            fx.emit("issue_not_routable", issue_id=i.id, identifier=i.identifier, reason=rc_reason)
            routing[i.id] = rc_reason
            continue
        candidates.append(i)
    started: set[str] = set()
    for issue in dispatch_order(candidates):
        if not admission_open():
            break
        if not _has_slot(cfg, busy, per_state, issue.state):
            continue
        held = tracked.get(issue.id)
        if held is not None and held.get("status") == PARKED:
            # The ticket came back to an active state — Rework, typically. The parked
            # entry was keeping the previous run alive for a retrospective; a new run
            # supersedes it, so it goes now. `claim_issue` stays a strict put-if-absent:
            # it is the fencing primitive against duplicate dispatch, and loosening it to
            # accommodate this case would weaken that guarantee everywhere.
            state.remember_dispatch_seq(kv, issue.id, held.get("dispatch_seq", 1))
            kv.delete(state.issue_key(issue.id))
            # The rollup describes the run being superseded, so it goes with it: carrying it
            # forward would blend two runs' traces into one report.
            kv.delete(state.telemetry_key(issue.id))
        if not state.claim_issue(kv, issue.id, issue.identifier, now_ms, assignee=issue.assignee):
            continue  # another replica won the claim
        entry = kv.get(state.issue_key(issue.id))
        seq = entry["dispatch_seq"]
        if not state.acquire_dispatch_token(kv, issue.id, seq, owner, now_ms):
            fx.emit("dispatch_token_lost", issue_id=issue.id, attempt=1)
            continue
        runtime, component, component_path = resolve_workspace(cfg, issue)
        entry.update(title=issue.title, target_repo=issue.target_repo,
                     target_repos=list(issue.target_repos),
                     runtime=runtime, component=component, component_path=component_path,
                     model=cfg.engine_model,
                     tracker_state=issue.state, tracker_state_type=issue.state_type)
        kv.put(state.issue_key(issue.id), entry)
        if not admission_open():
            continue
        fx.spawn_runner(issue, 1, seq)
        fx.emit("dispatch", issue_id=issue.id, identifier=issue.identifier, attempt=1, seq=seq)
        _record_dispatch_metrics(kv, issue, cfg, now_ms)
        busy += 1
        per_state[issue.state] = per_state.get(issue.state, 0) + 1
        dispatched += 1
        started.add(issue.id)

    _intake(kv, tracker, cfg, now_ms, fx, active, owned - started, started, routing, wake_reasons)
    return {"dispatched": dispatched, "tracked": len(tracked), "busy": busy}


_TRACKER_WAKES = ("linear:", "github:issues:", "github:sub_issues:", "github:issue_dependencies:",
                  "github:projects_v2_item:")


def _intake(
    kv: state.KV, tracker: TrackerAdapter, cfg: Config, now_ms: int, fx: Effects,
    active: list[Issue], owned: set[str], started: set[str], routing: dict[str, str],
    wake_reasons: Iterable[str],
) -> None:
    """Tell each ticket addressed to Symphony whether it is queued and, if not, what is
    missing (`intake.py`). Active tickets are checked on every tick, from the poll already
    made; the intake states (Backlog…) on a tracker webhook or every `intake.INTERVAL_MS`."""
    mark = intake.watermark(kv, now_ms)
    seen = list(active)
    woken = any(r.startswith(_TRACKER_WAKES) for r in wake_reasons)
    if cfg.intake_states and intake.due(kv, now_ms, woken):
        kv.put(intake.LAST_PASS_KEY, now_ms)
        try:
            seen += tracker.fetch_issues_by_states(list(cfg.intake_states))
        except TrackerUnavailable:
            fx.emit("tracker_unavailable", phase="intake")
    for issue in seen:
        if issue.id in owned or not intake.addressed(issue, cfg) or not intake.changed_since(issue, mark):
            continue
        if issue.id in started:
            # Its answer is the agent at work; remembered so a later stop is news again.
            intake.remember(kv, issue.id, intake.Verdict("queued", True, False, ""))
            continue
        v = intake.verdict(issue, cfg, routing.get(issue.id))
        if not intake.remember(kv, issue.id, v):
            continue
        fx.report_to_ticket(issue.id, f"**Symphony** — {v.text}")
        if v.human:
            fx.notify("not_queued", v.text, link=issue.url)
        fx.emit("intake_verdict", issue_id=issue.id, verdict=v.key)


PARKED = "parked"


def _hold_run(kv: state.KV, issue_id: str, entry: dict, now_ms: int, fx: Effects) -> None:
    """A human holds the ticket (internals §12): stop what runs and park, keeping the
    workspace — the human is taking it over, and the WIP branch, the session and the
    workspace are exactly what they take over.

    Unlike a ticket leaving the active states, a hold does not wait for the runner to
    notice: the runner watches the tracker, and nothing on the ticket changed. Parking
    deletes the dispatch tokens and the gateway bearer, so even a runner that outlives the
    cancellation can no longer write, report or obtain a credential."""
    if entry.get("status") == PARKED:
        return  # already stopped; it stays parked until released
    seq = entry.get("dispatch_seq", 1)
    if entry.get("runner_call_id"):
        fx.cancel_runner(entry["runner_call_id"], issue_id=issue_id, seq=seq)
    if entry.get("status") in ("claimed", "running"):
        fx.kill_agent_process(entry.get("sandbox_name"), issue_id=issue_id, seq=seq)
    fx.release_workspace(issue_id, dict(entry))
    entry["bridge_token"] = None
    _park_claim(kv, issue_id, entry, now_ms)
    if not entry.get("session_reported"):
        _report_session(kv, issue_id, entry, fx)
        entry = kv.get(state.issue_key(issue_id)) or entry
        entry["session_reported"] = True
        kv.put(state.issue_key(issue_id), entry)
    fx.emit("issue_held", issue_id=issue_id, seq=seq)


def _park_claim(kv: state.KV, issue_id: str, entry: dict, now_ms: int) -> None:
    """Give back the slot without forgetting the work.

    Releasing a claim and forgetting an issue were the same operation, and they are not
    the same thing: the first returns a concurrency slot, the second discards everything
    known about the run. The cost fell on the happy path, because that is the path that
    parks — Todo → In Progress → In Review → Done. By the time the ticket reached a
    terminal state nothing tracked it, so `_finalize_terminal` never ran and the
    retrospective never fired. The learning loop therefore only ever saw work that was
    abandoned, never work that succeeded.

    Dispatch tokens still go: a parked issue may come back through Rework, and a stale
    token would collide with the new dispatch.
    """
    _delete_dispatch_tokens(kv, issue_id, max(entry.get("attempt", 1), entry.get("dispatch_seq", 1)))
    entry.update(status=PARKED, runner_call_id=None, stall_strikes=0, updated_at=now_ms)
    kv.put(state.issue_key(issue_id), entry)


def _entry_repos(entry: dict) -> list[str]:
    """The repositories recorded for a run, primary first."""
    repos = entry.get("target_repos") or ([entry["target_repo"]] if entry.get("target_repo") else [])
    return [str(repo).lower() for repo in repos]


def _comment_woken(entry: dict, woken: set[tuple[str, int]]) -> bool:
    """Did a GitHub comment webhook name one of this run's pull requests — or a pull
    request in one of its repositories that we have not attributed yet?"""
    if not woken:
        return False
    repos = set(_entry_repos(entry))
    known = entry.get("pr_numbers") or {}
    return any(
        repo in repos and (repo not in known or int(known[repo]) == number)
        for repo, number in woken
    )


def _watch_review(
    kv: state.KV, issue_id: str, entry: dict, issue: Issue, cfg: Config, now_ms: int, fx: Effects,
    admission_open: Callable[[], bool] = lambda: True,
    woken: set[tuple[str, int]] = frozenset(),
) -> None:
    """Listen for people while the issue is parked, and answer when they speak.

    Due on the per-issue throttle, or at once when a GitHub comment webhook names one of
    the run's pull requests. A multi-repository ticket has one pull request per repository
    that changed; all of them are one conversation for the purpose of answering.

    Everything here is best-effort and bounded: a code-host outage, a rate limit, or a pull
    request we cannot identify all resolve to "no information this cycle". The issue simply
    stays parked, which is where it already was (FR-005).
    """
    if not cfg.review_watch_enabled or entry.get("runner_call_id"):
        return  # already answering, or the operator has not opted in
    if cfg.review_watch_states and issue.state not in cfg.review_watch_states:
        return
    if not (
        _comment_woken(entry, woken)
        or prwatch.is_due(entry, now_ms, cfg.review_watch_interval_s)
    ):
        return

    entry["pr_checked_at"] = now_ms  # written even on failure: a broken repo must not be
    kv.put(state.issue_key(issue_id), entry)  # re-checked on every tick forever

    repos = _entry_repos(entry)
    if not repos:
        return
    numbers: dict[str, int] = dict(entry.get("pr_numbers") or {})
    comments: list[dict] = []
    try:
        for repo in repos:
            number = numbers.get(repo)
            if not number:
                number = prwatch.find_pull_number(
                    fx.open_pulls(repo), entry.get("identifier") or ""
                )
                if not number:
                    continue  # no pull request in this repository, or none we can attribute
                numbers[repo] = number
            comments.extend(fx.pr_conversation(repo, int(number)))
    except Exception as exc:  # noqa: BLE001 — an outage is never a reason to break a tick
        fx.emit("review_watch_unavailable", issue_id=issue_id, error=str(exc))
        return
    finally:
        if numbers != (entry.get("pr_numbers") or {}):
            entry["pr_numbers"] = numbers
            kv.put(state.issue_key(issue_id), entry)
    if not numbers:
        return  # no pull request yet

    if entry.get("pr_comment_watermark") is None:
        # First sight: record where the conversation already is. Without this, turning the
        # watch on would wake the agent to answer a review that finished days ago (FR-003).
        # A comment newer than the run's end is above the baseline and is answered now.
        entry["pr_comment_watermark"] = prwatch.baseline(entry, comments)
        kv.put(state.issue_key(issue_id), entry)

    newest = prwatch.new_question(entry, comments)
    if newest is None:
        return

    if not admission_open():
        return
    seq = entry.get("dispatch_seq", entry.get("attempt", 1)) + 1
    if not state.acquire_dispatch_token(kv, issue_id, seq, "review-watch", now_ms):
        return  # another replica is already answering this one
    entry.update(
        status="claimed",
        mode=prwatch.ANSWER_MODE,
        claimed_at=now_ms,
        runner_call_id=None,
        dispatch_seq=seq,
        # `attempt` is deliberately NOT touched: answering is not an implementation attempt,
        # and a chatty reviewer must not be able to exhaust an issue's retries (FR-010).
        pr_comment_watermark=newest,
        updated_at=now_ms,
    )
    kv.put(state.issue_key(issue_id), entry)
    if not admission_open():
        return
    fx.spawn_runner(issue, entry.get("attempt", 1), seq)
    fx.emit("answer_dispatch", issue_id=issue_id, seq=seq, prs=dict(numbers))


def _report_session(kv: state.KV, issue_id: str, entry: dict, fx: Effects) -> None:
    """Hand the reviewer the run's shape and cost at the moment they inherit the work.

    Everything here is best-effort by construction: the report is an observation, and an
    observation that can fail a tick is a liability, not an instrument. A ticket that cannot
    be commented on, a rollup that never arrived, a renderer that trips over an unexpected
    shape — all of them leave the park itself untouched.
    """
    if not telemetry.has_recorded_run(entry):
        return  # claimed but never ran: nothing happened worth reporting (FR-007)
    try:
        rollup = kv.get(state.telemetry_key(issue_id))
    except Exception:  # noqa: BLE001 — an unavailable rollup still leaves a token report
        rollup = None
    try:
        fx.report_to_ticket(issue_id, telemetry.render_session_report(entry, rollup))
    except Exception as exc:  # noqa: BLE001
        fx.emit("session_report_failed", issue_id=issue_id, error=str(exc))


def _release_claim(kv: state.KV, issue_id: str, entry: dict) -> None:
    """Releases the claim AND the dispatch tokens: without this, a Human Review→Rework
    cycle would collide with the stale token and burn extra ticks until adoption (SC-001).
    Safe: the runner has already exited (release) or was cancelled+killed (reassign/gone) —
    deleting the token only strengthens the fencing against a potential replay."""
    _delete_dispatch_tokens(kv, issue_id, max(entry.get("attempt", 1), entry.get("dispatch_seq", 1)))
    kv.delete(state.issue_key(issue_id))
    kv.delete(state.telemetry_key(issue_id))  # FR-017


def _record_dispatch_metrics(kv: state.KV, issue: Issue, cfg: Config, now_ms: int) -> None:
    """FR-032/SC-007 instrumentation: dispatches by state (rework rate =
    dispatches_by_state['Rework'] / runs) recorded under the current month."""
    yyyymm = _yyyymm(now_ms)
    state.bump_metric(kv, yyyymm, "dispatched")
    state.bump_metric_category(kv, yyyymm, "dispatches_by_state", issue.state)
    if issue.state.casefold() == cfg.tracker_states["rework"].casefold():
        state.bump_metric(kv, yyyymm, "rework")


def _record_retry(kv: state.KV, entry: dict, now_ms: int) -> None:
    """`symphony_retries_total`: a redispatch after a failure (it left a failure summary) or
    after a clean continuation, under the current month."""
    reason = "failure" if entry.get("failure_summary") else "continuation"
    state.bump_metric_category(kv, _yyyymm(now_ms), "retries", reason)


def _check_stall(
    kv: state.KV, tracker: TrackerAdapter, issue: Issue, entry: dict, cfg: Config, now_ms: int,
    fx: Effects,
) -> None:
    """Stall: silence beyond the threshold for TWO consecutive windows + confirmation
    that the call is still running — the double check avoids killing a healthy runner (FR-007).
    Timestamps compared here (`last_event_at`) are written by the runner and read by
    the daemon, but the comparison is always against the WRITER's clock over whole
    tolerance windows — never a decision on a thin clock-skew margin.

    A runner whose Job has ended while the entry still says running is not silent but dead,
    and is requeued at once instead of after the stall windows."""
    if cfg.stall_timeout_ms <= 0:
        return  # upstream §6.4: non-positive stall timeout disables stall detection
    issue_id = issue.id
    silence = now_ms - entry.get("last_event_at", now_ms)
    call_id = entry.get("runner_call_id")
    if (call_id and silence > 2 * cfg.polling_interval_ms and not fx.is_call_running(call_id)
            and _still_owned_by(kv, issue_id, call_id)):
        # The runner records its outcome before it exits: a finished Job whose entry still
        # names it as running died without one.
        _requeue_failed(kv, tracker, issue, entry, cfg, now_ms, fx, still_running=False)
        return
    if silence <= cfg.stall_timeout_ms:
        if entry.get("stall_strikes"):
            entry.update(stall_strikes=0, stall_seen_event_at=None)
            kv.put(state.issue_key(issue_id), entry)
        return
    # "two CONSECUTIVE windows": if there was progress since the last strike, the
    # sequence restarts — never kill over two silences separated by activity
    if entry.get("stall_seen_event_at") != entry.get("last_event_at"):
        entry.update(stall_strikes=0, stall_seen_event_at=entry.get("last_event_at"))
    strikes = entry.get("stall_strikes", 0) + 1
    if strikes < STALL_STRIKES_TO_KILL:
        entry.update(stall_strikes=strikes, updated_at=now_ms)
        kv.put(state.issue_key(issue_id), entry)
        fx.emit("stall_strike", issue_id=issue_id, strikes=strikes)
        return
    still_running = bool(call_id) and fx.is_call_running(call_id)
    _requeue_failed(kv, tracker, issue, entry, cfg, now_ms, fx, still_running=still_running)


def _still_owned_by(kv: state.KV, issue_id: str, call_id: str) -> bool:
    fresh = kv.get(state.issue_key(issue_id)) or {}
    return fresh.get("status") == "running" and fresh.get("runner_call_id") == call_id


def _requeue_failed(
    kv: state.KV, tracker: TrackerAdapter, issue: Issue, entry: dict, cfg: Config, now_ms: int,
    fx: Effects, *, still_running: bool,
) -> None:
    """Stop what is left of a stalled or dead attempt, requeue it, say so on the ticket, and
    hand the failure to triage. The evidence is read first: cancelling deletes the Job."""
    issue_id = issue.id
    call_id = entry.get("runner_call_id")
    evidence = fx.failure_evidence(call_id) if call_id else {}
    if still_running:
        fx.cancel_runner(call_id, issue_id=issue_id, seq=entry.get("dispatch_seq", 1))
    fx.kill_agent_process(entry.get("sandbox_name"))
    summary = "stall detected (silence > threshold for 2 windows)" if still_running else "runner died mid-attempt"
    if evidence.get("error"):
        summary += f": {evidence['error']}"
    # Read BEFORE the requeue: `mark_retry_after_failure` advances the counter, so
    # afterwards `attempt` names the one about to start, not the one that just died.
    attempt = entry.get("attempt", "?")
    triage.record(kv, tracker, cfg, issue, dict(entry), summary, evidence, fx, now_ms)
    mark_retry_after_failure(kv, issue_id, entry, cfg, now_ms, summary)
    fx.emit("stall_killed" if still_running else "runner_crash_requeued", issue_id=issue_id)
    if entry.get("status") == "blocked":
        return  # the retries ran out: the next tick reports that, with the cause
    window_min = cfg.stall_timeout_ms / 60_000
    window = f"{window_min:.0f} minutes" if window_min >= 1 else f"{cfg.stall_timeout_ms / 1000:.0f} seconds"
    detail = (f"went silent for more than {window} and was stopped" if still_running
              else "stopped unexpectedly" + (f" ({triage.redact(evidence['error'])[:200]})"
                                             if evidence.get("error") else ""))
    budget = (f" This is failure {entry['failures']} of {cfg.max_failed_attempts} in a row; at "
              f"{cfg.max_failed_attempts} Symphony stops retrying and asks for a human."
              if cfg.max_failed_attempts else "")
    fx.report_to_ticket(
        issue_id,
        f"**Attempt {attempt} ended without finishing** — it {detail}. "
        f"A new attempt is queued; the branch and this Workpad are kept, so the next one "
        f"continues rather than restarts.{budget}\n\n_Reported by the orchestrator, not the agent: "
        f"it was no longer running to report for itself._",
    )


def _report_exhausted(
    kv: state.KV, tracker: TrackerAdapter, issue: Issue, entry: dict, cfg: Config, now_ms: int,
    fx: Effects,
) -> None:
    """Retries ran out: say on the ticket and in the channel what failed, who acts, and
    what brings the ticket back. One report per exhaustion (`exhaustion_reported_at`). With
    triage on, the failure goes to its investigation, which then is who acts."""
    failures = entry.get("failures") or cfg.max_failed_attempts
    cause = (entry.get("failure_summary") or "no failure summary was recorded").strip()[:300]
    call_id = entry.get("last_call_id")
    investigation = triage.record(kv, tracker, cfg, issue, entry, cause,
                                  fx.failure_evidence(call_id) if call_id else {}, fx, now_ms)
    who = (f"an agent, in {investigation}, finds the cause; a Symphony operator if it cannot fix it"
           if investigation else
           "a Symphony operator: retrying did not clear it, so the cause is likely outside this ticket")
    queue_again = (f"run `symphony reevaluate {issue.identifier}`, or edit the ticket (a comment "
                   "is enough) to queue a new attempt")
    fx.report_to_ticket(
        issue.id,
        f"**Symphony stopped retrying this ticket** — {failures} attempts in a row ended without "
        f"finishing.\n\n- **What failed:** {triage.redact(cause)}\n- **Who acts:** {who}.\n"
        f"- **Next step:** once the cause is fixed, {queue_again}. The branch and this Workpad are "
        f"kept.\n\n_Reported by the orchestrator, not the agent._",
    )
    fx.notify(
        "retries_exhausted",
        f"{issue.identifier} stopped after {failures} failed attempts in a row.\n"
        f"What failed: {triage.redact(cause)}\nWho acts: {who}.\n"
        f"Next: once the cause is fixed, {queue_again}.",
        link=issue.url,
    )


def _cooldown_holds(entry: dict, issue: Issue, cfg: Config, now_ms: int, refresh: bool) -> bool:
    """Anti-runaway (FR-009): an issue parked with no change since the last clean exit
    only comes back after the cooldown — or immediately under POST /refresh."""
    if refresh:
        return False
    last_exit = entry.get("last_clean_exit_at")
    if not last_exit:
        return False
    if issue.updated_at != entry.get("last_issue_updated_at"):
        return False
    return (now_ms - last_exit) < cfg.redispatch_cooldown_s * 1_000


def _grant_refresh(kv: state.KV, tracked: dict[str, dict], now_ms: int) -> int:
    """Turn a refresh request into a durable, one-shot grant on every queued run.

    The request itself lives for one tick, but what it releases may not be due yet: a clean
    continuation is queued ~1 s out and a failed attempt sits in backoff. So each queued
    entry carries `refresh_grant` until its next dispatch, which bypasses the cooldown once.
    A clean continuation is also pulled forward to now — the human asked for it now. A
    failure's backoff is left alone: it is the retry policy, not anti-runaway. Dispatch
    still goes through the per-dispatch token, so the grant never bypasses fencing."""
    granted = 0
    for issue_id, entry in tracked.items():
        if entry.get("status") != "retry_queued":
            continue
        entry["refresh_grant"] = True
        if entry.get("last_clean_exit_at") and not entry.get("failure_summary"):
            entry["retry_at"] = min(entry.get("retry_at") or now_ms, now_ms)
        kv.put(state.issue_key(issue_id), entry)
        granted += 1
    return granted
