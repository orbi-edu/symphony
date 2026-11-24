"""Linear and GitHub webhooks: signatures (constant-time HMAC) and the wakes they push."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from tests.api_support import GITHUB_SECRET, LINEAR_SECRET, client_for, make_services

from symphony import questions, wake
from symphony.merger.store import wake_target

APP_MERGER = wake_target("acme/app")


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client)
    return services, client_for(services)


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pending(redis_client, target):
    return wake.wait(redis_client, "test", target, 0.01)


def linear(client, payload, *, secret=LINEAR_SECRET, signature=None):
    body = json.dumps(payload).encode()
    headers = {"Linear-Signature": signature or sign(secret, body),
               "Content-Type": "application/json"}
    return client.post("/v1/webhooks/linear", content=body, headers=headers)


def github(client, event, payload, *, secret=GITHUB_SECRET):
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": "sha256=" + sign(secret, body), "X-GitHub-Event": event,
               "Content-Type": "application/json"}
    return client.post("/v1/webhooks/github", content=body, headers=headers)


def test_linear_good_signature_wakes_the_daemon(api, redis_client):
    _, client = api
    payload = {"action": "update", "type": "Issue", "data": {"identifier": "ENG-7"},
               "webhookTimestamp": int(time.time() * 1000)}
    response = linear(client, payload)
    assert response.status_code == 202 and response.json() == {"woke": ["daemon"]}
    assert pending(redis_client, wake.DAEMON) == ["linear:update:ENG-7"]


@pytest.mark.parametrize("signature", ["0" * 64, "", "nothex"])
def test_linear_bad_signature_is_rejected_without_wake(api, redis_client, signature):
    _, client = api
    body = json.dumps({"action": "create"}).encode()
    response = client.post("/v1/webhooks/linear", content=body,
                           headers={"Linear-Signature": signature} if signature else {})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    assert pending(redis_client, wake.DAEMON) == []


def test_linear_wrong_secret_and_replayed_delivery(api, redis_client):
    _, client = api
    assert linear(client, {"action": "create"}, secret="other").status_code == 401
    old = {"action": "create", "webhookTimestamp": int(time.time() * 1000) - 10 * 60 * 1000}
    assert linear(client, old).status_code == 401
    assert pending(redis_client, wake.DAEMON) == []


def test_unconfigured_secret_is_503(redis_client):
    services = make_services(redis_client)
    services.linear_secret = None
    services.github_secret = None
    client = client_for(services)
    assert linear(client, {"action": "x"}).status_code == 503
    assert github(client, "push", {}).status_code == 503


def test_a_reply_to_an_agents_question_is_queued_for_the_daemon(api, redis_client):
    _, client = api
    questions.ask(redis_client, "test", qid="q1", issue_id="i1", identifier="ENG-1", sid="i1:1",
                  url=None, question="Which?", options=["A", "B"], to=None, now_ms=1)
    redis_client.set("sym:test:question-ref:linear:root", "q1")
    stamp = int(time.time() * 1000)
    for parent in ("root", "unrelated"):
        payload = {"action": "create", "type": "Comment", "webhookTimestamp": stamp,
                   "data": {"id": f"c-{parent}", "parentId": parent, "body": "2",
                            "user": {"name": "Ana"}}}
        assert linear(client, payload).status_code == 202
    queued = [json.loads(raw) for raw in redis_client.lrange("sym:test:questions:inbox", 0, -1)]
    assert queued == [{"id": "c-root", "parent": "root", "body": "2", "author": "Ana"}]


@pytest.mark.parametrize(
    "event,payload,target,reason",
    [
        ("pull_request", {"action": "ready_for_review", "number": 5, "pull_request": {"number": 5},
                          "repository": {"full_name": "Acme/App"}},
         APP_MERGER, "github:pull_request:ready_for_review:acme/app#5"),
        ("check_suite", {"action": "completed", "check_suite": {"pull_requests": [{"number": 8}]},
                         "repository": {"full_name": "acme/app"}},
         APP_MERGER, "github:check_suite:completed:acme/app#8"),
        ("check_run", {"action": "completed", "check_run": {"pull_requests": []},
                       "repository": {"full_name": "acme/app"}},
         APP_MERGER, "github:check_run:completed:acme/app"),
        ("workflow_run", {"action": "completed", "workflow_run": {"pull_requests": [{"number": 2}]},
                          "repository": {"full_name": "acme/app"}},
         APP_MERGER, "github:workflow_run:completed:acme/app#2"),
        ("push", {"ref": "refs/heads/main",
                  "repository": {"full_name": "acme/app", "default_branch": "main"}},
         APP_MERGER, "github:push:acme/app:main"),
        ("issue_comment", {"action": "created", "issue": {"number": 4, "pull_request": {}},
                           "repository": {"full_name": "Acme/App"}},
         wake.DAEMON, "github:comment:acme/app#4"),
        ("pull_request_review", {"action": "submitted", "pull_request": {"number": 6},
                                 "repository": {"full_name": "acme/app"}},
         wake.DAEMON, "github:comment:acme/app#6"),
        ("pull_request_review_comment", {"action": "created", "pull_request": {"number": 6},
                                         "repository": {"full_name": "acme/app"}},
         wake.DAEMON, "github:comment:acme/app#6"),
        # GitHub tracker: ticket changes
        ("issue_comment", {"action": "created", "issue": {"number": 4},
                           "repository": {"full_name": "Acme/App"}},
         wake.DAEMON, "github:issue_comment:created:acme/app#4"),
        ("issues", {"action": "labeled", "issue": {"number": 3},
                    "repository": {"full_name": "acme/app"}},
         wake.DAEMON, "github:issues:labeled:acme/app#3"),
        ("sub_issues", {"action": "sub_issue_added", "parent_issue": {"number": 3},
                        "repository": {"full_name": "acme/app"}},
         wake.DAEMON, "github:sub_issues:sub_issue_added:acme/app#3"),
        ("issue_dependencies", {"action": "blocked_by_added", "blocked_issue": {"number": 5},
                                "repository": {"full_name": "acme/app"}},
         wake.DAEMON, "github:issue_dependencies:blocked_by_added:acme/app#5"),
        ("projects_v2_item", {"action": "edited", "projects_v2_item": {
            "content_type": "Issue", "content_node_id": "I_abc"}},
         wake.DAEMON, "github:projects_v2_item:edited:I_abc"),
    ],
)
def test_github_events_wake_the_right_loop(api, redis_client, event, payload, target, reason):
    _, client = api
    response = github(client, event, payload)
    assert response.status_code == 202 and response.json() == {"woke": [target]}
    assert pending(redis_client, target) == [reason]
    other = wake.DAEMON if target == APP_MERGER else APP_MERGER
    assert pending(redis_client, other) == []
    assert pending(redis_client, wake.MERGER) == []  # no shared merger list anymore


def test_each_repository_wakes_its_own_merger(api, redis_client):
    _, client = api
    github(client, "pull_request", {"action": "synchronize", "pull_request": {"number": 1},
                                    "repository": {"full_name": "acme/lib"}})
    assert pending(redis_client, APP_MERGER) == []
    assert pending(redis_client, wake_target("acme/lib")) == [
        "github:pull_request:synchronize:acme/lib#1"]
    nameless = github(client, "check_run", {"action": "completed", "check_run": {}})
    assert nameless.json() == {"woke": []}


@pytest.mark.parametrize(
    "event,payload",
    [
        ("push", {"ref": "refs/heads/feature", "repository": {"full_name": "acme/app"}}),
        ("issue_comment", {"issue": {"number": 4}}),
        ("projects_v2_item", {"action": "created", "projects_v2_item": {
            "content_type": "DraftIssue", "content_node_id": "DI_abc"}}),
        ("star", {"repository": {"full_name": "acme/app"}}),
        ("ping", {"zen": "Keep it simple."}),
    ],
)
def test_github_events_that_wake_nothing(api, redis_client, event, payload):
    _, client = api
    response = github(client, event, payload)
    assert response.status_code == 202 and response.json() == {"woke": []}
    assert pending(redis_client, wake.DAEMON) == [] and pending(redis_client, APP_MERGER) == []


def test_github_bad_signature(api, redis_client):
    _, client = api
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    for signature in ("sha256=" + "0" * 64, sign(GITHUB_SECRET, body), ""):
        response = client.post("/v1/webhooks/github", content=body,
                               headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "push"})
        assert response.status_code == 401
    assert pending(redis_client, APP_MERGER) == []
