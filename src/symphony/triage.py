"""Failure triage: a failed run becomes one investigation ticket per failure signature.

When a run crashes, stalls, or uses up its retry budget, the daemon collects what the cluster
knows about it (`Effects.failure_evidence`), derives a signature from the error, and opens an
`investigation` ticket aimed at `triage.repo` in the first active state, so an agent starts on
it. The investigation finds the root cause and turns a fix into specified sub-issues.

A signature seen again only adds an occurrence to its ticket (at most one comment an hour);
`triage.max_per_day` caps new tickets; a failure of a triage ticket is never triaged, so the
loop cannot feed itself.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re

from . import state
from .assistant.memory import _BEARER, _PRIVATE_KEY, _TOKEN_PREFIX
from .config import Config
from .trackers.base import Issue

TRIAGE_LABEL = "triage"
SIGNATURE_PREFIX = "triage:signature:"
DAY_PREFIX = "triage:day:"
REPEAT_COMMENT_EVERY_MS = 3_600_000
MAX_EVIDENCE_LINES = 40

_VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # uuids
    r"|\b[0-9a-f]{7,64}\b"  # shas, hex ids
    r"|\d+",
    re.IGNORECASE,
)


def signature(summary: str, evidence: dict) -> str:
    """The failure's identity across runs and tickets: the error text and the cause of a pod
    disruption, with ids, numbers and hashes masked."""
    disruption = evidence.get("disruption")
    text = " ".join(filter(None, [summary, evidence.get("error"), evidence.get("reason"),
                                  disruption and f"pod {disruption}"]))
    normalized = " ".join(_VOLATILE.sub("#", text.casefold()).split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def redact(text: str) -> str:
    text = _TOKEN_PREFIX.sub("[redacted]", text)
    text = _BEARER.sub("bearer [redacted]", text)
    return "[private key redacted]" if _PRIVATE_KEY.search(text) else text


def is_triage_ticket(issue: Issue) -> bool:
    return any(label.casefold() == TRIAGE_LABEL for label in issue.labels)


def record(kv: state.KV, tracker, cfg: Config, issue: Issue, entry: dict, summary: str,
           evidence: dict, fx, now_ms: int) -> str | None:
    """Open, or add an occurrence to, the investigation for this failure; returns its ticket
    identifier. Best effort: a tracker outage loses the triage, never the tick."""
    if not cfg.triage_enabled or not cfg.triage_repo or is_triage_ticket(issue):
        return None
    sig = signature(summary, evidence)
    key = SIGNATURE_PREFIX + sig
    known = kv.get(key)
    try:
        if known and (tracker.state_of(known["identifier"]) or {}).get("type") == "terminal":
            known = None  # that investigation closed: the failure is back, so it is news again
        if known:
            known["count"] = known.get("count", 1) + 1
            if now_ms - known.get("commented_at", 0) >= REPEAT_COMMENT_EVERY_MS:
                tracker.comment(known["identifier"],
                                f"Seen again: {issue.identifier}, attempt {entry.get('attempt', '?')} "
                                f"({known['count']} times so far). {redact(summary)[:200]}")
                known["commented_at"] = now_ms
            kv.put(key, known)
            fx.emit("triage_repeat", signature=sig, ticket=known["identifier"], count=known["count"])
            return known["identifier"]
        day_key = DAY_PREFIX + _dt.datetime.fromtimestamp(now_ms / 1000, tz=_dt.UTC).strftime("%Y%m%d")
        opened = kv.get(day_key) or 0
        if opened >= cfg.triage_max_per_day:
            fx.emit("triage_capped", signature=sig, opened_today=opened)
            return None
        title, body = _ticket(cfg, issue, entry, summary, evidence, sig)
        labels = [*cfg.required_labels, cfg.investigation_label, TRIAGE_LABEL, f"repo:{cfg.triage_repo}"]
        made = tracker.create_issue(title, body, labels=list(dict.fromkeys(labels)),
                                    state=cfg.active_states[0], repo=cfg.triage_repo)
    except Exception as exc:  # noqa: BLE001 — triage is visibility, never a reason to fail
        fx.emit("triage_failed", signature=sig, error=str(exc)[:300])
        return None
    kv.put(day_key, opened + 1)
    kv.put(key, {"identifier": made["identifier"], "url": made.get("url"), "count": 1,
                 "commented_at": now_ms, "first_seen": issue.identifier})
    fx.emit("triage_opened", signature=sig, ticket=made["identifier"], failed=issue.identifier)
    fx.notify(
        "triage_opened",
        f"A run of {issue.identifier} failed: {redact(summary)[:200]}\n"
        f"Who acts: an agent — Symphony opened {made['identifier']} to find the cause.\n"
        f"Next: its report and the fix it specifies land on {made['identifier']}.",
        link=made.get("url"),
        direct_to=issue.assignee_keys,  # the failed ticket's assignee
    )
    return made["identifier"]


def _ticket(cfg: Config, issue: Issue, entry: dict, summary: str, evidence: dict,
            sig: str) -> tuple[str, str]:
    repo = cfg.triage_repo
    short = " ".join(redact(summary).split())[:70]
    lines = [redact(line)[:300] for line in (evidence.get("lines") or [])][-MAX_EVIDENCE_LINES:]
    facts = [f"- **Failed run:** {issue.identifier} ({issue.url or 'no link'}), attempt "
             f"{entry.get('attempt', '?')}, Job `{evidence.get('job') or 'unknown'}`",
             f"- **What failed:** {redact(summary)}",
             f"- **Signature:** `{sig}` — the same failure again adds an occurrence here"]
    for name, label in (("reason", "Job"), ("container", "Runner container"), ("pod", "Runner pod"),
                        ("error", "Error")):
        if evidence.get(name):
            facts.append(f"- **{label}:** {redact(str(evidence[name]))[:300]}")
    log_block = ("\n\n## Runner log (last lines)\n\n```\n" + "\n".join(lines) + "\n```") if lines else ""
    body = (
        "Symphony opened this investigation because a run failed.\n\n" + "\n".join(facts) + log_block
        + "\n\n## What to do\n\n"
        "1. Find the root cause, with evidence: what you ran and what it printed. Decide whether "
        f"it is a defect in `{repo}`, or in the environment (cluster, credentials, the failed "
        "ticket's repository).\n"
        f"2. If it is a defect in `{repo}`: write the report on this ticket, then create the fix "
        "as sub-issues with `symphony_create_issues`. Give each one a spec: the behavior to "
        "change, the acceptance criteria, and the test that fails today.\n"
        "3. If it is not: write the report and name who must act.\n\n"
        f"`{repo}` may be public. Nothing from this ticket — names, account ids, hostnames, "
        "ticket ids, log lines — goes into its code, commits or pull requests: describe the "
        "problem in generic terms."
    )
    return f"Investigate run failure: {short}", body
