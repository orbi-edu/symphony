"""CI as the merger reads it: the check runs on a head decide, per the repository's policy.

`ci_state` folds a head's check runs into one of `passed`, `failed`, `running` or `none`
(nothing yet, or only runs that ended without a verdict, like a run a newer one cancelled).
The workflow helpers cancel the runs nobody waits for, rerun failed jobs, and revive a head
whose only run was cancelled.
"""

from __future__ import annotations

RED = {"failure", "timed_out", "action_required", "startup_failure"}
NO_VERDICT = {"cancelled", "stale"}
PASS = {"success", "neutral", "skipped"}
# The merger's own checks never decide a head.
IGNORED = {"symphony-merger"}


def ci_state(checks: list[dict], required: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """The CI verdict of one head. With `required` names only those count (a missing one is
    still to come); without, every check run on the head does."""
    latest: dict[str, dict] = {}
    for c in checks:
        name = c["name"]
        if name in IGNORED or (required and name not in required):
            continue
        prev = latest.get(name)
        # A skipped run is a draft's: it counts only when there is no other.
        rank = (c.get("conclusion") != "skipped", c.get("started_at") or "")
        if prev is None or rank > (prev.get("conclusion") != "skipped", prev.get("started_at") or ""):
            latest[name] = c
    if not latest:
        return "none", []
    failed = [
        f"{c['name']} {c['conclusion']} {c.get('url', '')}".strip()
        for c in latest.values()
        if c.get("status") == "completed" and c.get("conclusion") in RED
    ]
    if failed:
        return "failed", failed
    if any(c.get("status") != "completed" for c in latest.values()):
        return "running", []
    if required and set(required) - set(latest):
        return "running", []
    if any(c.get("conclusion") in NO_VERDICT for c in latest.values()):
        return "none", []
    return "passed", []


def _ours(run: dict, workflow: str | None) -> bool:
    return workflow is None or run["name"] == workflow


def cancel_ci(gh, sha: str, workflow: str | None, wait=None, tries: int = 6, every_s: float = 5) -> list[int]:
    """Cancels the runs still going on `sha`: nobody waits for them. With `wait` (a pause),
    a head pushed just now is watched until its run appears (GitHub creates it seconds after
    the push; a cancel before that finds nothing and the run burns its minutes)."""
    for attempt in range(tries if wait else 1):
        runs = [r for r in gh.workflow_runs(sha=sha) if _ours(r, workflow)]
        if runs or wait is None:
            ids = []
            for run in runs:
                if run["status"] != "completed":
                    gh.cancel(run["id"])
                    ids.append(run["id"])
            return ids
        if attempt < tries - 1:
            wait(every_s)
    return []


def restart_ci(gh, sha: str, workflow: str | None) -> int | None:
    """Reruns the latest run on `sha` when it was cancelled: a push of a head the branch
    already has starts none."""
    runs = [r for r in gh.workflow_runs(sha=sha) if _ours(r, workflow)]
    if runs and runs[0]["status"] == "completed" and runs[0]["conclusion"] == "cancelled":
        gh.rerun(runs[0]["id"])
        return runs[0]["id"]
    return None


def revive_ci(gh, sha: str, workflow: str | None) -> int | None:
    """A ready head whose CI ended without a verdict would never turn green: its newest
    cancelled run is run again, up to its third attempt, when nothing else runs on it."""
    runs = [r for r in gh.workflow_runs(sha=sha) if _ours(r, workflow)]
    if any(r["status"] != "completed" for r in runs):
        return None
    for r in runs:
        if r["conclusion"] == "cancelled" and r["attempt"] < 3:
            gh.rerun(r["id"])
            return r["id"]
    return None


def latest_run(gh, sha: str, workflow: str | None) -> dict | None:
    """The run whose failed jobs a red batch reruns: the latest failed one on `sha`."""
    runs = [r for r in gh.workflow_runs(sha=sha) if _ours(r, workflow)]
    failed = [r for r in runs if r["conclusion"] in RED or r["conclusion"] == "failure"]
    return (failed or runs or [None])[0]
