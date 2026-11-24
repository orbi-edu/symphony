"""Webhooks: Linear and GitHub events wake the daemon and the merger now, not at the next poll.

Both are authenticated only by their HMAC-SHA256 signature over the raw body (constant-time
compare). A wake is a hint: the woken loop re-reads the source of truth, so a dropped or
duplicated delivery costs latency, never correctness.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Request

from .. import questions, wake
from ..merger.store import wake_target
from ..prwatch import comment_wake
from .control import Control
from .errors import ApiError

LINEAR_TOLERANCE_MS = 5 * 60 * 1000
MERGER_EVENTS = ("pull_request", "check_suite", "check_run", "workflow_run")
COMMENT_EVENTS = ("issue_comment", "pull_request_review", "pull_request_review_comment")
# GitHub tracker (`tracker.kind: github`): a ticket changed — the issue, its board item, its
# sub-issues or its dependencies. The payload key holding the issue the wake names.
TICKET_EVENTS = {
    "issues": "issue",
    "sub_issues": "parent_issue",
    "issue_dependencies": "blocked_issue",
}


def _verify(secret: str | None, body: bytes, presented: str | None, *, prefix: str = "") -> None:
    if not secret:
        raise ApiError(503, "webhook_unconfigured", "this webhook has no signing secret configured")
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not presented or not hmac.compare_digest(expected, presented.strip()):
        raise ApiError(401, "invalid_signature", "the webhook signature does not match")


def _payload(body: bytes) -> dict:
    try:
        payload = json.loads(body)
    except ValueError:
        raise ApiError(422, "invalid_request", "the webhook body is not JSON") from None
    if not isinstance(payload, dict):
        raise ApiError(422, "invalid_request", "the webhook body is not a JSON object")
    return payload


def github_wakes(event: str, payload: dict) -> list[tuple[str, str]]:
    """`(target, reason)` wakes for one GitHub delivery. Each repository's merger waits on its
    own target (`merger.store.wake_target`), so the wake goes to that repository's merger."""
    repo = str((payload.get("repository") or {}).get("full_name") or "").lower()
    if event in MERGER_EVENTS:
        if not repo:
            return []
        number = (payload.get("pull_request") or {}).get("number")
        if number is None:
            container = payload.get(event) or {}
            prs = container.get("pull_requests") or []
            number = prs[0].get("number") if prs and isinstance(prs[0], dict) else None
        suffix = f"{repo}#{number}" if number else repo
        return [(wake_target(repo), f"github:{event}:{payload.get('action', '')}:{suffix}")]
    if event == "push":
        default = (payload.get("repository") or {}).get("default_branch") or "main"
        if repo and payload.get("ref") == f"refs/heads/{default}":
            return [(wake_target(repo), f"github:push:{repo}:{default}")]
        return []
    if event == "issue_comment":
        issue = payload.get("issue") or {}
        if not (issue.get("number") and repo):
            return []
        if "pull_request" in issue:
            return [(wake.DAEMON, comment_wake(repo, issue["number"]))]
        action = payload.get("action", "")
        return [(wake.DAEMON, f"github:issue_comment:{action}:{repo}#{issue['number']}")]
    if event in TICKET_EVENTS:
        number = (payload.get(TICKET_EVENTS[event]) or {}).get("number")
        suffix = f"{repo}#{number}" if number else repo
        return [(wake.DAEMON, f"github:{event}:{payload.get('action', '')}:{suffix}")]
    if event == "projects_v2_item":
        # org projects only, and no `repository` key: the item names the issue by node id
        item = payload.get("projects_v2_item") or {}
        if item.get("content_type") not in ("Issue", None):
            return []  # drafts and pull requests are not tickets
        ref = item.get("content_node_id") or item.get("node_id") or ""
        return [(wake.DAEMON, f"github:projects_v2_item:{payload.get('action', '')}:{ref}")]
    if event in ("pull_request_review", "pull_request_review_comment"):
        number = (payload.get("pull_request") or {}).get("number")
        if number and repo:
            return [(wake.DAEMON, comment_wake(repo, number))]
    return []


def github_answer(client, ns: str, event: str, payload: dict, now_ms: int) -> str | None:
    """Pull request conversation for agents' questions (`questions.pull_answer`): remembers a
    pull request's branch, and takes a new comment or review as an answer. Returns the
    question it answered. Inline review comments are about code lines, not answers."""
    repo = str((payload.get("repository") or {}).get("full_name") or "").lower()
    if not repo:
        return None
    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        questions.remember_pull(client, ns, repo, pull.get("number"),
                                (pull.get("head") or {}).get("ref") or "")
        return None
    if event == "issue_comment" and payload.get("action") == "created":
        issue = payload.get("issue") or {}
        if "pull_request" not in issue or not issue.get("number"):
            return None  # a comment on an issue, not on a pull request
        comment = payload.get("comment") or {}
        pull = {k: issue.get(k) for k in ("title", "body", "state")}
        number, at = issue["number"], comment.get("created_at")
    elif event == "pull_request_review" and payload.get("action") == "submitted":
        pull = payload.get("pull_request") or {}
        if not pull.get("number"):
            return None
        comment = payload.get("review") or {}
        number, at = pull["number"], comment.get("submitted_at")
    else:
        return None
    return questions.pull_answer(client, ns, repo=repo, number=int(number), pull=pull,
                                 comment=comment, at_ms=questions.iso_ms(at), now_ms=now_ms)


def build_router(control: Control) -> APIRouter:
    router = APIRouter(prefix="/v1/webhooks")
    s = control.s

    def secret(configured: str | None, name: str) -> str | None:
        # Read per request: setup may have just stored it (local mode updates the env at once).
        return configured or os.environ.get(name) or None

    def delivered(source: str) -> None:
        """The last signed delivery per source: what the setup `webhooks` step shows."""
        s.client.set(f"sym:{s.namespace}:setup:webhook:{source}", str(int(time.time() * 1000)))

    @router.post("/linear", status_code=202)
    async def linear(request: Request):
        body = await request.body()
        _verify(secret(s.linear_secret, "LINEAR_WEBHOOK_SECRET"), body,
                request.headers.get("linear-signature"))
        payload = _payload(body)
        stamp = payload.get("webhookTimestamp")
        if isinstance(stamp, int) and abs(time.time() * 1000 - stamp) > LINEAR_TOLERANCE_MS:
            raise ApiError(401, "stale_webhook", "the webhook timestamp is too old")
        delivered("linear")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        actor = payload.get("actor") or {}
        if (payload.get("type") == "Comment" and payload.get("action") == "create"
                and not data.get("botActor") and actor.get("type", "user") == "user"):
            # A comment may answer an agent's question; the daemon decides (`questions.py`).
            now = int(time.time() * 1000)
            author = ((data.get("user") or {}).get("name")
                      or (data.get("externalUser") or {}).get("name") or actor.get("name"))
            at_ms = (questions.iso_ms(data.get("createdAt"))
                     or (stamp if isinstance(stamp, int) else now))
            questions.queue_reply(
                s.client, s.namespace, comment_id=str(data.get("id") or ""),
                issue_id=str(data.get("issueId") or (data.get("issue") or {}).get("id") or ""),
                parent_id=str(data.get("parentId") or ""), body=str(data.get("body") or ""),
                author=str(author or ""), at_ms=at_ms, now_ms=now)
        reason = f"linear:{payload.get('action', '')}:{data.get('identifier') or payload.get('type', '')}"
        wake.wake(s.client, s.namespace, wake.DAEMON, reason[:200])
        return {"woke": [wake.DAEMON]}

    @router.post("/github", status_code=202)
    async def github(request: Request):
        body = await request.body()
        _verify(
            secret(s.github_secret, "GITHUB_WEBHOOK_SECRET"), body,
            request.headers.get("x-hub-signature-256"), prefix="sha256=",
        )
        delivered("github")
        event = request.headers.get("x-github-event", "")
        payload = _payload(body)
        if event == "ping":
            return {"woke": []}
        # Before the wake: a comment that answers an agent's question is claimed first, so the
        # review watch the wake triggers leaves it alone.
        github_answer(s.client, s.namespace, event, payload, int(time.time() * 1000))
        targets = github_wakes(event, payload)
        for target, reason in targets:
            wake.wake(s.client, s.namespace, target, reason[:200])
        return {"woke": [target for target, _ in targets]}

    return router
