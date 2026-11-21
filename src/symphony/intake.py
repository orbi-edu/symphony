"""Intake: a ticket addressed to Symphony learns whether it is queued and, if not, why.

A ticket is addressed to Symphony when it carries one of `tracker.required_labels` or a
`repo:` label. For each one in an intake state (`tracker.intake_states`, e.g. Backlog) or in
an active state, the daemon computes a verdict: queued, or not queued with the missing
requirement — the state, a label, the repository, or an open blocker. The verdict goes on the
ticket as a comment, and to the channel when a person has to act. It is said again only when
it changes.

Tickets that were last updated before Symphony first looked (the watermark) are left alone,
so turning this on does not comment on a whole backlog at once.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from . import repos, state
from .config import Config
from .trackers.base import Issue

VERDICT_PREFIX = "intake:verdict:"
WATERMARK_KEY = "intake:watermark"
LAST_PASS_KEY = "intake:last_pass"
# How often the intake states are read without a tracker webhook. A webhook makes the pass
# due at once, so this only bounds the delay of a missed one.
INTERVAL_MS = 300_000

_REPO_REASONS = {
    repos.MISSING: "it names no repository",
    repos.MALFORMED: "a `repo:` label is not `repo:owner/name`",
    repos.MIXED_OWNERS: "its `repo:` labels name repositories of two owners",
    repos.UNKNOWN_RUNTIME: "a `runtime:` label names a runtime Symphony does not have",
    repos.CONFLICTING_RUNTIMES: "it has two `runtime:` labels",
    repos.UNKNOWN_COMPONENT: "a `component:` label names no configured component",
    repos.CONFLICTING_COMPONENTS: "it has two `component:` labels",
}


@dataclass(frozen=True)
class Verdict:
    key: str  # identifies the verdict: the same key is never reported twice in a row
    queued: bool
    human: bool  # a person has to act for the ticket to be queued
    text: str


def addressed(issue: Issue, cfg: Config) -> bool:
    labels = {label.casefold() for label in issue.labels}
    if any(label.casefold() in labels for label in cfg.required_labels):
        return True
    return any(label.startswith(repos.PREFIX) for label in labels)


def verdict(issue: Issue, cfg: Config, routing_reason: str | None = None) -> Verdict:
    """The ticket's verdict. `routing_reason` is the runtime/component check the orchestrator
    runs on active tickets; the repository reason comes with the issue."""
    ref = issue.identifier
    active = ", ".join(cfg.active_states)
    if issue.state not in cfg.active_states:
        first = cfg.active_states[0] if cfg.active_states else "an active state"
        return Verdict(
            f"state:{issue.state}", False, True,
            f"{ref} is not queued: it is in **{issue.state}**, and Symphony only takes tickets in "
            f"{active}.\nWho acts: the ticket's owner.\nNext: move it to **{first}** when it is ready.",
        )
    have = {label.casefold() for label in issue.labels}
    missing = [label for label in cfg.required_labels if label.casefold() not in have]
    if missing:
        names = ", ".join(f"`{label}`" for label in missing)
        return Verdict(
            "labels:" + ",".join(missing), False, True,
            f"{ref} is not queued: it lacks the {names} label.\nWho acts: the ticket's owner.\n"
            f"Next: add {names}.",
        )
    reason = issue.undispatchable_reason or routing_reason
    if reason or issue.target_repo is None:
        why = _REPO_REASONS.get(reason or repos.MISSING, reason or "")
        return Verdict(
            f"routing:{reason}", False, True,
            f"{ref} is not queued: {why}.\nWho acts: the ticket's owner.\n"
            "Next: fix the labels — one `repo:owner/name` label per repository.",
        )
    if issue.blocked_by:
        names = ", ".join(f"{b.get('identifier')} ({b.get('state')})" for b in issue.blocked_by)
        return Verdict(
            "blocked:" + ",".join(sorted(str(b.get("identifier")) for b in issue.blocked_by)), False, False,
            f"{ref} is not queued yet: it waits on {names}.\nWho acts: nobody — it is queued on its "
            "own once those are done.",
        )
    if not issue.dispatchable:
        return Verdict(
            "undispatchable", False, True,
            f"{ref} is not queued: the tracker reports an open blocker, or an assignee this "
            "Symphony does not work for.\nWho acts: the ticket's owner.\nNext: clear the blocker, "
            "or assign the ticket to Symphony's user.",
        )
    return Verdict("queued", True, False,
                   f"{ref} is queued: an agent starts it at the next free slot.")


def due(kv: state.KV, now_ms: int, woken_by_tracker: bool) -> bool:
    last = kv.get(LAST_PASS_KEY) or 0
    return woken_by_tracker or now_ms - last >= INTERVAL_MS


def watermark(kv: state.KV, now_ms: int) -> int:
    """When Symphony first looked; set once."""
    mark = kv.get(WATERMARK_KEY)
    if mark is None:
        kv.put(WATERMARK_KEY, now_ms)
        return now_ms
    return mark


def changed_since(issue: Issue, mark_ms: int) -> bool:
    if not issue.updated_at:
        return False
    try:
        at = _dt.datetime.fromisoformat(issue.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return at.timestamp() * 1000 > mark_ms


def remember(kv: state.KV, issue_id: str, v: Verdict) -> bool:
    """True when the verdict is new for this ticket (and records it)."""
    key = VERDICT_PREFIX + issue_id
    if kv.get(key) == v.key:
        return False
    kv.put(key, v.key)
    return True
