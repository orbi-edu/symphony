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
        if payload.get("type") == "Comment" and payload.get("action") == "create":
            # A reply to an agent's question is its answer (`questions.py`).
            author = (data.get("user") or {}).get("name") or (payload.get("actor") or {}).get("name")
            questions.queue_reply(s.client, s.namespace, comment_id=str(data.get("id") or ""),
                                  parent_id=str(data.get("parentId") or ""),
                                  body=str(data.get("body") or ""), author=str(author or ""))
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
        targets = github_wakes(event, payload)
        for target, reason in targets:
            wake.wake(s.client, s.namespace, target, reason[:200])
        return {"woke": [target for target, _ in targets]}

    return router
