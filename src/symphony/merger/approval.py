"""Human approval gate and preview announcement for the merge train.

The approval gate (``require_approval: true`` in policy) holds reviewed PRs until a
human approves them on GitHub.  The train re-checks before merging and sends the PR
back to waiting if the approval is dismissed.

The preview announcement (``preview:`` in policy) posts one Slack message per (PR,
head sha) when a matching workflow artifact exists, with a button the assistant
handles.
"""

from __future__ import annotations

import structlog

from . import comments as cm
from .context import Merger
from .policy import Policy

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Human approval gate
# ---------------------------------------------------------------------------

DECISION_STATES = ("APPROVED", "CHANGES_REQUESTED")


def _latest_human_reviews(m: Merger, n: int) -> dict[str, str]:
    """The latest meaningful review per human reviewer: ``{login: state}``."""
    reviews = m.gh.reviews(n)
    latest: dict[str, str] = {}
    for r in reviews:
        if r["state"] in DECISION_STATES:
            latest[r["user"].casefold()] = r["state"]
    return latest


def is_approved(m: Merger, n: int) -> bool:
    """True when the PR is approved: at least one APPROVED and no CHANGES_REQUESTED
    among the latest review per human reviewer.

    We check the latest review per reviewer rather than ``reviewDecision`` because
    ``reviewDecision`` is only present in GraphQL and requires CODEOWNERS to be
    meaningful.  Checking latest-per-reviewer is deterministic and matches what
    GitHub's branch protection rule does.
    """
    latest = _latest_human_reviews(m, n)
    if not latest:
        return False
    return all(s == "APPROVED" for s in latest.values())


def check_approval(m: Merger, policy: Policy, rec: dict) -> bool:
    """Check if a reviewed PR meets the approval requirement.  Returns True when
    the PR may proceed to the train.  When it may not, labels it and notifies
    reviewers once per head sha.  Called from ``settle_parked`` and the train's
    ``take``."""
    if not policy.require_approval:
        return True
    n = rec["pr"]
    if is_approved(m, n):
        # Remove the waiting label if present.
        try:
            m.gh.remove_label(n, cm.WAIT_APPROVAL)
        except Exception:  # noqa: BLE001
            pass
        return True
    # Not yet approved — label and notify.
    try:
        m.gh.add_labels(n, [cm.WAIT_APPROVAL])
    except Exception:  # noqa: BLE001
        pass
    _notify_needs_approval(m, n, rec)
    return False


def _approval_dedup_key(store, n: int, head_sha: str) -> str:
    return store._k(f"approval:{n}:{head_sha[:12]}")  # noqa: SLF001


def _notify_needs_approval(m: Merger, n: int, rec: dict) -> None:
    """One notification per (PR, head sha), mentioning each mapped reviewer (the author
    when none is mapped)."""
    key = _approval_dedup_key(m.store, n, rec["head_sha"])
    if not m.store.client.set(key, "1", nx=True, ex=86400):
        return
    try:
        pr = m.gh.pr(n)
    except Exception:  # noqa: BLE001 — the author is still worth a ping
        pr = {"number": n, "author": rec.get("author") or ""}
    for login in _mapped_reviewers(m, pr) or [None]:
        m.notify(
            "needs_approval",
            f"PR #{n} {rec.get('title', '')[:90]} is reviewed and waiting for a human approval.",
            link=m.pr_url(n),
            login=login,
        )


# ---------------------------------------------------------------------------
# Preview announcement
# ---------------------------------------------------------------------------


MISS_TTL_S = 120


def _preview_dedup_key(store, repo: str, n: int, sha: str) -> str:
    return store._k(f"preview:{n}:{sha[:12]}")  # noqa: SLF001


def _find_preview_artifact(m: Merger, head_sha: str, artifact_name: str) -> bool:
    """True when a workflow run on ``head_sha`` has a non-expired artifact named
    ``artifact_name``."""
    try:
        runs = m.gh.workflow_runs(sha=head_sha)
    except Exception:  # noqa: BLE001
        return False
    for run in runs:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        try:
            url = m.gh.artifact_download_url(run["id"], artifact_name)
            if url:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _mapped_reviewers(m: Merger, pr: dict) -> list[str]:
    """GitHub logins of the PR's reviewers per contract §4: requested reviewers plus
    those with a submitted review, mapped in `notify.users.slack`. With none mapped, the
    PR author (Symphony does not know the tracker creator's GitHub login)."""
    from .. import notify as _notify

    logins: list[str] = []
    # requested_reviewers come from the PR dict (FakeGitHub provides them, REST sets them)
    for login in pr.get("requested_reviewers", []):
        if isinstance(login, dict):
            login = login.get("login", "")
        if login and login.casefold() not in [x.casefold() for x in logins]:
            logins.append(login)
    # submitted reviews
    try:
        reviews = m.gh.reviews(pr["number"])
        for r in reviews:
            user = r.get("user", "")
            if user and user.casefold() not in [x.casefold() for x in logins]:
                logins.append(user)
    except Exception:  # noqa: BLE001
        pass
    # Filter to mapped logins
    users = (_notify._configured_users  # noqa: SLF001
             if _notify._configured_users is not None  # noqa: SLF001
             else _notify._users_from_workflow())  # noqa: SLF001
    slack_map = users.get("slack", {})
    mapped = [lg for lg in logins if slack_map.get(lg.casefold())]
    if mapped:
        return mapped
    # Fallback: the PR author.
    author = pr.get("author", "")
    if author and slack_map.get(author.casefold()):
        return [author]
    return [author] if author else []


def button_id(repo: str, pr_number: int, head_sha: str) -> str:
    """The action id for the preview button (contract §2)."""
    return f"preview:{repo}:{pr_number}:{head_sha[:12]}"


def check_preview(m: Merger, policy: Policy, pr: dict) -> None:
    """If the policy has ``preview`` and a matching artifact exists for the PR's
    current head, post one Slack announcement per (repo, PR, head sha)."""
    preview = policy.preview
    if not preview:
        return
    artifact_name = preview.get("artifact", "")
    if not artifact_name:
        return
    n = pr["number"]
    head_sha = pr["head_sha"]
    if pr.get("draft"):
        return
    # Deduplicate in Redis.
    key = _preview_dedup_key(m.store, m.repo, n, head_sha)
    # Check dedup first (cheap) before the artifact lookup (network call).
    if m.store.client.exists(key):
        return
    # A head without the artifact yet is asked again after MISS_TTL_S, not on every pass.
    miss = m.store._k(f"preview-miss:{n}:{head_sha[:12]}")  # noqa: SLF001
    if m.store.client.exists(miss):
        return
    if not _find_preview_artifact(m, head_sha, artifact_name):
        m.store.client.set(miss, "1", ex=MISS_TTL_S)
        return
    # Claim the dedup key.
    if not m.store.client.set(key, "1", nx=True, ex=7 * 86400):
        return
    reviewers = _mapped_reviewers(m, pr)
    text = f"#{n} {pr.get('title', '')[:90]}: an iOS build of this head is ready to try in a simulator"
    bid = button_id(m.repo, n, head_sha)
    from .. import notify as _notify

    _notify.post_with_button(
        "preview_ready",
        text,
        button_label="Open in simulator",
        button_action_id=bid,
        link=m.pr_url(n),
        github_logins=reviewers,
    )


def check_previews_for_open_prs(m: Merger, policy: Policy) -> None:
    """Scan open PRs for preview artifacts.  Called during the normal review pass."""
    if not policy.preview:
        return
    try:
        prs = m.gh.list_open()
    except Exception:  # noqa: BLE001
        return
    for pr in prs:
        if pr.get("draft") or pr.get("base") != m.main:
            continue
        try:
            check_preview(m, policy, pr)
        except Exception:  # noqa: BLE001
            log.warning("merger_preview_check_failed", pr=pr["number"])
