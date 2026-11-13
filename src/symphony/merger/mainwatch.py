"""T8: the main CI watch. Reads the finished CI runs of pushes to main; a red one opens a ticket
on the tracker with the failing jobs' logs (the Symphony daemon dispatches an agent to fix it)
and tells Slack; the next green run closes the alert. A failure where no step failed (the runner went away)
is held for its rerun instead of alerting; held runs not rerun within two hours are dropped. With
no state yet, only the newest finished run is read: old failures are not replayed."""

from __future__ import annotations

from .context import Merger, log
from .policy import Policy

HOLD_MS = 2 * 3600_000
LOG_LINES = 120


def _interrupted(m: Merger, run: dict) -> bool:
    jobs = m.gh.run_jobs(run["id"])
    return not any(s["conclusion"] == "failure" for j in jobs for s in j["steps"])


def _failure_log(m: Merger, run: dict) -> str:
    parts = []
    for job in [j for j in m.gh.run_jobs(run["id"]) if j["conclusion"] == "failure"][:3]:
        try:
            text = m.gh.job_log(job["id"])
        except Exception as exc:  # noqa: BLE001 — the ticket still links the run
            text = f"(log unavailable: {exc})"
        tail = "\n".join(text.rstrip().split("\n")[-LOG_LINES:])
        failed = ", ".join(s["name"] for s in job["steps"] if s["conclusion"] == "failure")
        parts.append(f"### {job['name']} (failed step: {failed or 'unknown'})\n\n```\n{tail[-12000:]}\n```")
    return "\n\n".join(parts) or "No failed job log was available."


def _open_alert(m: Merger, policy: Policy, run: dict) -> dict | None:
    watch = policy.main_watch
    if m.tracker is None or watch.get("ticket") is not True:
        return None
    body = (
        f"CI on `{m.main}` failed on {run['sha'][:9]} ({run['title'][:200]}).\n\n"
        f"Run: {run['url']}\n\nrepo: {m.repo}\n\nFix main so this workflow passes again. Keep the change to "
        f"what the failure needs.\n\n{_failure_log(m, run)}"
    )
    # Routing: `repo:<owner/name>` and the tracker's required labels, or the daemon can't
    # dispatch the fix (with no default repo the ticket would be unroutable).
    labels = list(dict.fromkeys([f"repo:{m.repo}", *m.ticket_labels, *(watch.get("labels") or [])]))
    try:
        issue = m.tracker.create_issue(f"Main CI red: {run['name']} on {run['sha'][:9]}", body,
                                       labels=labels, state=watch.get("state", "Todo"), repo=m.repo)
        return {"identifier": issue["identifier"], "url": issue.get("url", ""), "sha": run["sha"], "run": run["id"]}
    except Exception as exc:  # noqa: BLE001 — Slack still hears it
        log.warning("merger_main_ticket_failed", error=str(exc)[:300])
        return None


def _close_alert(m: Merger, alert: dict, run: dict) -> None:
    if m.tracker is None:
        return
    try:
        state = m.tracker.state_of(alert["identifier"])
        if state and state.get("type") != "terminal":
            m.tracker.comment(alert["identifier"], f"Main CI is green again on {run['sha'][:9]} ({run['url']}); "
                                                   "closing this alert.")
            m.tracker.move(alert["identifier"], m.states["done"])
    except Exception as exc:  # noqa: BLE001
        log.warning("merger_main_ticket_close_failed", issue=alert["identifier"], error=str(exc)[:300])


def watch_once(m: Merger, policy: Policy) -> list[str]:
    """One pass; returns what it reported."""
    finished = sorted(
        (r for r in m.gh.workflow_runs(branch=m.main, event="push", status="completed")
         if r["conclusion"] not in ("cancelled", "skipped")
         and (policy.ci_workflow is None or r["name"] == policy.ci_workflow)),
        key=lambda r: r["id"],
    )
    state = m.store.mainci()
    if state is None:
        state = {"last": 0, "red": False, "held": [], "alert": None}
        runs = finished[-1:]
    else:
        runs = [r for r in finished if r["id"] > state["last"]]
    now = m.clock()
    again, keep = [], []
    for h in state["held"]:
        cur = m.gh.run(h["id"])
        if cur["status"] == "completed" and cur["attempt"] > h["attempt"]:
            again.append(cur)
        elif now - h["since"] < HOLD_MS:
            keep.append(h)
    state["held"] = keep
    said = []
    for run in sorted(runs + again, key=lambda r: r["id"]):
        state["last"] = max(state["last"], run["id"])
        if run["conclusion"] != "success" and _interrupted(m, run):
            state["held"].append({"id": run["id"], "attempt": run["attempt"], "since": now})
            log.info("merger_main_ci_interrupted", run=run["id"])
            continue
        if run["conclusion"] != "success":
            if not state["red"] or state.get("alert") is None:
                state["alert"] = _open_alert(m, policy, run) or state.get("alert")
            alert = state.get("alert") or {}
            m.notify("main_red", f"CI on {m.main} {run['conclusion']} at {run['sha'][:9]}: {run['title'][:120]}"
                     + (f" (ticket {alert['identifier']})" if alert.get("identifier") else ""), link=run["url"])
            state["red"] = True
            said.append(f"red {run['id']}")
        else:
            if state["red"]:
                if state.get("alert"):
                    _close_alert(m, state["alert"], run)
                m.notify("main_green", f"CI on {m.main} is green again at {run['sha'][:9]}: {run['title'][:120]}",
                         link=run["url"])
                said.append(f"green {run['id']}")
            state["red"] = False
            state["alert"] = None
    m.store.save_mainci(state)
    return said
