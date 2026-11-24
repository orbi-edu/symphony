"""Schema and access layer for the orchestrator's shared state.

In production the backend is Redis (`kv.RedisKV`, put-if-absent via SET NX); in
tests, an in-memory fake. The ONLY conditional primitive this schema relies on is
put-if-absent (`skip_if_exists=True`) — all fencing in the system is built on top of
it (constitution IV). Redis could offer more (CAS via WATCH/Lua), but the contract
deliberately stays at put-if-absent so the fencing design — and every test asserting
it — carries over from the Modal port unchanged. Read+write based locks are
therefore advisory by definition, exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

ISSUE_PREFIX = "issue:"
DISPATCH_PREFIX = "dispatch:"
TOOLCALL_PREFIX = "toolcall:"
RETRO_PREFIX = "retro:"
METRICS_PREFIX = "metrics:"
TELEMETRY_PREFIX = "telemetry:"
HOLD_PREFIX = "hold:"
ANSWERED_PREFIX = "answered:"
HEARTBEAT_KEY = "orchestrator:heartbeat"
LOCK_KEY = "lock:orchestrator"
TOTALS_KEY = "totals:tokens"
REFRESH_KEY = "refresh:requested"
RATELIMITS_KEY = "ratelimits:engine"
POLL_BACKOFF_KEY = "poll:backoff"
LESSONS_AGE_KEY = "lessons:usage_age"  # {slug: number of incorporations since last use}
# Whether the daemon can work: `{status: "ready" | "incomplete", reason, at}` (setup).
SETUP_KEY = "daemon:setup"

LEASE_TTL_MS = 60_000


class KV(Protocol):
    """Minimal boundary over the store; both RedisKV and the test fake implement it."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def put(self, key: str, value: Any, *, skip_if_exists: bool = False) -> bool:
        """Returns False when skip_if_exists=True and the key already existed."""
        ...

    def delete(self, key: str) -> None: ...

    def keys(self) -> Iterable[str]: ...


def issue_key(issue_id: str) -> str:
    return f"{ISSUE_PREFIX}{issue_id}"


def dispatch_key(issue_id: str, seq: int) -> str:
    """Token per DISPATCH (not per attempt): clean continuations reuse the same
    `attempt` but every spawn gets a fresh seq — the old token never blocks
    redispatch, and a runner replayed from an earlier seq fails fencing."""
    return f"{DISPATCH_PREFIX}{issue_id}:{seq}"


def toolcall_key(issue_id: str, seq: int, call_id: str) -> str:
    """Keyed on the engine's tool-call id, which is stable across replay.

    The thread and turn components this key carried in 001 existed because the previous
    engine REPLAYED tool calls on session resume, so the cache had to tell a replayed
    call apart from a new one. The current engine does not replay: a subscriber gets no
    backfill on resume and history is pull-only. The remaining duplication risk is the
    platform re-executing the same runner input under the same dispatch seq, which
    `<seq>:<call_id>` covers exactly.

    Scoping by seq is kept deliberately: a fresh dispatch opens a fresh namespace, so a
    genuinely new mutation is never suppressed by an older attempt's cache.
    """
    return f"{TOOLCALL_PREFIX}{issue_id}:{seq}:{call_id}"


def retro_key(issue_id: str) -> str:
    return f"{RETRO_PREFIX}{issue_id}"


def answered_key(issue_id: str) -> str:
    """Pull request comments (`prwatch.comment_ref`) that answered one of the ticket's
    questions (`questions.pull_answer`): the review watch does not answer them again."""
    return f"{ANSWERED_PREFIX}{issue_id}"


def telemetry_key(issue_id: str) -> str:
    """The attempt's span rollup, deliberately NOT a field on the issue entry.

    The telemetry receiver writes on every export batch while the runner writes the issue
    entry on its heartbeat. With no compare-and-swap available (see the module docstring),
    two writers on one key lose updates — and the update lost would be the token ledger's,
    which is the one piece of data here that must not be approximate. Separate keys mean
    the two writers never contend.
    """
    return f"{TELEMETRY_PREFIX}{issue_id}"


def metrics_key(yyyymm: str) -> str:
    return f"{METRICS_PREFIX}{yyyymm}"


def tracked_issues(kv: KV) -> dict[str, dict]:
    """All issue:* entries — the per-tick read also renews their TTL."""
    out: dict[str, dict] = {}
    for key in list(kv.keys()):
        if key.startswith(ISSUE_PREFIX):
            entry = kv.get(key)
            if entry is not None:
                out[key[len(ISSUE_PREFIX) :]] = entry
    return out


def hold_key(issue_id: str) -> str:
    """A human holds this ticket (internals §12): `{by, reason, at}` with `at` in epoch ms.

    While it exists the orchestrator neither dispatches, retries nor continues the issue,
    and a running attempt is stopped and parked with its workspace kept. Deleting it hands
    the ticket back. Its own key, never a field on the issue entry: a hold must outlive the
    entry (it can precede any dispatch) and is written by the command path, not the tick."""
    return f"{HOLD_PREFIX}{issue_id}"


def hold_record(by: str, reason: str, at_ms: int) -> dict:
    return {"by": by, "reason": reason, "at": at_ms}


def held_issues(kv: KV) -> dict[str, dict]:
    """Every held issue → its hold record. Holds never expire and reading one never adds a
    TTL (`kv.NO_TTL_PREFIXES`): only an explicit release ends a hold."""
    out: dict[str, dict] = {}
    for key in list(kv.keys()):
        if key.startswith(HOLD_PREFIX):
            record = kv.get(key)
            if record is not None:
                out[key[len(HOLD_PREFIX) :]] = record
    return out


def claim_issue(kv: KV, issue_id: str, identifier: str, now_ms: int, assignee: str | None = None) -> bool:
    """Initial put-if-absent claim — protects only the FIRST attempt (§3.7.1)."""
    if kv.get(issue_key(issue_id)) is not None:
        return False
    generation_key = f"generation:{issue_id}"
    allocate = getattr(kv, "next_generation", None)
    seq = allocate(generation_key) if allocate else (kv.get(generation_key) or 0) + 1
    if allocate is None:
        kv.put(generation_key, seq)
    return kv.put(
        issue_key(issue_id),
        {
            "status": "claimed",
            "identifier": identifier,
            "assignee": assignee,
            "attempt": 1,
            "dispatch_seq": seq,
            "runs": 1,  # every spawn (fresh/retry/continuation) counts; drives the template `attempt`
            "claimed_at": now_ms,
            "retry_at": None,
            "runner_call_id": None,
            "sandbox_id": None,
            "sandbox_name": None,
            "last_event_at": now_ms,
            "stall_strikes": 0,
            "last_clean_exit_at": None,
            "last_issue_updated_at": None,
            "failure_summary": None,
            # cost_estimate is a LOCAL estimate from the engine's bundled price table,
            # never a billed charge — every surface that shows it must say so (FR-031)
            "tokens": {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "total": 0,
                "cost_estimate": 0.0,
            },
            # engine sessions are addressed by file path, not by an opaque thread id
            "session_path": None,
            "session_id": None,
            # authorizes THIS attempt against the sandbox gateway (gateway_api.py); rotated
            # per dispatch and deleted at cleanup. Not a credential for any external service.
            "bridge_token": None,
            "updated_at": now_ms,
        },
        skip_if_exists=True,
    )


def acquire_dispatch_token(kv: KV, issue_id: str, attempt: int, owner: str, now_ms: int) -> bool:
    """EVERY spawn (new or retry) goes through here; only the put winner dispatches."""
    remember_dispatch_seq(kv, issue_id, attempt)
    return kv.put(
        dispatch_key(issue_id, attempt),
        {"daemon_owner": owner, "created_at": now_ms},
        skip_if_exists=True,
    )


def remember_dispatch_seq(kv: KV, issue_id: str, seq: int) -> None:
    generation_key = f"generation:{issue_id}"
    remember = getattr(kv, "remember_generation", None)
    if remember:
        remember(generation_key, seq)
    else:
        kv.put(generation_key, max(kv.get(generation_key) or 0, seq))


def acquire_retro_token(kv: KV, issue_id: str, now_ms: int) -> bool:
    return kv.put(
        retro_key(issue_id),
        {"status": "started", "reflector_call_id": None, "learnings_pr": None, "at": now_ms},
        skip_if_exists=True,
    )


def heartbeat(kv: KV, now_ms: int) -> None:
    kv.put(HEARTBEAT_KEY, now_ms)


def report_setup(kv: KV, now_ms: int, reason: str | None) -> None:
    """`reason` None ⇒ ready; otherwise the secret-free sentence saying what setup lacks."""
    kv.put(
        SETUP_KEY,
        {"status": "incomplete" if reason else "ready", "reason": reason, "at": now_ms},
    )


def renew_lease(
    kv: KV, owner: str, now_ms: int, ttl_ms: int = LEASE_TTL_MS, *, require_live: bool = False
) -> bool:
    """Production leases are atomic and use Redis time; injected stores are sequential."""
    atomic = getattr(kv, "renew_lease", None)
    if atomic is not None:
        return atomic(LOCK_KEY, owner, ttl_ms, require_live=require_live)
    current = kv.get(LOCK_KEY)
    if require_live and (not current or current.get("owner") != owner
                         or current.get("lease_expires", 0) <= now_ms):
        return False
    if current and current.get("owner") != owner and current.get("lease_expires", 0) > now_ms:
        return False
    kv.put(LOCK_KEY, {"owner": owner, "lease_expires": now_ms + ttl_ms})
    readback = kv.get(LOCK_KEY)
    return bool(readback and readback.get("owner") == owner)


def release_lease(kv: KV, owner: str) -> bool:
    """A stopping daemon hands the lease back, so its successor need not wait for the TTL.
    Only the holder can release it; anyone else's lease is left untouched."""
    atomic = getattr(kv, "release_lease", None)
    if atomic is not None:
        return atomic(LOCK_KEY, owner)
    current = kv.get(LOCK_KEY)
    if not current or current.get("owner") != owner:
        return False
    kv.delete(LOCK_KEY)
    return True


def fenced_put(kv: KV, key: str, value: Any, expected: dict, token_key: str) -> bool:
    """Compare the runner generation and authorization atomically with its write."""
    atomic = getattr(kv, "fenced_put", None)
    if atomic is not None:
        return atomic(key, value, expected, token_key)
    current = kv.get(key)
    if not current or not kv.get(token_key):
        return False
    if any(current.get(field) != wanted for field, wanted in expected.items()):
        return False
    return kv.put(key, value)


TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "total", "cost_estimate")


def add_tokens(totals: dict, usage: dict) -> dict:
    """Field-wise accumulation. `cost_estimate` is a float; the rest are counts."""
    return {field: totals.get(field, 0) + usage.get(field, 0) for field in TOKEN_FIELDS}


def touch_metrics(kv: KV, now_yyyymm: str, months: int = 12) -> None:
    """Re-reads the last N months of metrics to renew the Dict's 7-day TTL —
    historical metrics feed SC-007 and must not expire."""
    year, month = int(now_yyyymm[:4]), int(now_yyyymm[4:6])
    for _ in range(months):
        kv.get(metrics_key(f"{year:04d}{month:02d}"))
        month -= 1
        if month == 0:
            year, month = year - 1, 12


def bump_metric(kv: KV, yyyymm: str, field: str, by: int = 1) -> None:
    key = metrics_key(yyyymm)
    m = kv.get(key) or {}
    m[field] = m.get(field, 0) + by
    kv.put(key, m)


def bump_metric_category(kv: KV, yyyymm: str, field: str, category: str, by: int = 1) -> None:
    key = metrics_key(yyyymm)
    m = kv.get(key) or {}
    bucket = m.get(field) or {}
    bucket[category] = bucket.get(category, 0) + by
    m[field] = bucket
    kv.put(key, m)
