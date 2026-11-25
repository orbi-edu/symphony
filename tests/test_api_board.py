"""Overview, issues, durable commands, audit and the workflow document."""

from __future__ import annotations

import json
import time

import pytest
from tests.api_support import (
    FakeMerger,
    bearer,
    client_for,
    fresh_daemon,
    make_services,
    tokens_for,
)

from symphony import state, wake
from symphony.admin_workflow import WorkflowError


def put_issue(client, issue_id, **entry):
    client.set(state.issue_key(issue_id), json.dumps({"dispatch_seq": 1, "attempt": 1, **entry}))


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client, merger=FakeMerger())
    return services, client_for(services), tokens_for(services)


def test_overview_counts_tokens_and_etag(api, redis_client):
    services, client, tokens = api
    fresh_daemon(redis_client)
    put_issue(redis_client, "i1", identifier="ENG-1", status="running")
    put_issue(redis_client, "i2", identifier="ENG-2", status="running")
    put_issue(redis_client, "i3", identifier="ENG-3", status="claimed")
    put_issue(redis_client, "i4", identifier="ENG-4", status="retry_queued")
    put_issue(redis_client, "i5", identifier="ENG-5", status="parked", tracker_state="In Review")
    put_issue(redis_client, "i6", identifier="ENG-6", status="parked", tracker_state="Merging")
    services.sessions.open("i2:1", kind="implement")
    services.sessions.request_input("i2:1", "q1", "Which?")
    services.sessions.open("i1:1", kind="implement")
    services.sessions.update("i1:1", tokens={"input": 10, "output": 5, "total": 15, "cost": 0.25})
    services.sessions.update("i2:1", tokens={"total": 5, "cost": 0.5})
    headers = bearer(tokens["viewer"])
    response = client.get("/v1/overview", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["daemon"]["alive"] is True and body["daemon"]["paused"] is False
    assert body["daemon"]["heartbeat_at"].endswith("Z")
    assert body["counts"] == {"running": 1, "waiting_input": 1, "queued": 1, "retrying": 1,
                              "in_review": 1, "merging": 1, "held": 0, "blocked_on_person": 0}
    assert body["held"] == []
    assert body["tokens_today"] == 20 and body["cost_today"] == 0.75
    assert body["merger"] == {"reviewing": 1, "train": 0, "parked": 2}
    assert body["config_generation"] == 0
    etag = response.headers["ETag"]
    cached = client.get("/v1/overview", headers=headers | {"If-None-Match": etag})
    assert cached.status_code == 304
    put_issue(redis_client, "i7", identifier="ENG-7", status="claimed")
    fresh = client.get("/v1/overview", headers=headers | {"If-None-Match": etag})
    assert fresh.status_code == 200 and fresh.json()["counts"]["queued"] == 2


def test_overview_reports_a_dead_daemon(api):
    _, client, tokens = api
    body = client.get("/v1/overview", headers=bearer(tokens["viewer"])).json()
    assert body["daemon"] == {
        "alive": False, "paused": False, "heartbeat_at": None, "setup": None}


def test_issues_join_linear_state_and_session_without_leaking_failures(api, redis_client):
    services, client, tokens = api
    put_issue(redis_client, "i1", identifier="ENG-1", title="Login", status="running",
              tracker_state="In Progress", failure_summary="token=sk-secret",
              pr_url="https://github.com/acme/app/pull/3")
    services.sessions.open("i1:1", kind="implement")
    services.sessions.request_input("i1:1", "q1", "Which?")
    items = client.get("/v1/issues", headers=bearer(tokens["viewer"])).json()["items"]
    [item] = items
    assert item["identifier"] == "ENG-1" and item["tracker_state"] == "In Progress"
    assert item["phase"] == "waiting_input" and item["session_id"] == "i1:1"
    assert item["input_request"]["question"] == "Which?"
    assert item["pr_url"] == "https://github.com/acme/app/pull/3"
    assert "sk-secret" not in json.dumps(items)


def test_commands_are_durable_intents_by_the_token_name_and_wake_the_daemon(api, redis_client):
    services, client, tokens = api
    fresh_daemon(redis_client)
    headers = bearer(tokens["operator"]) | {"Idempotency-Key": "pause-key-1"}
    body = {"action": "pause", "reason": "maintenance window"}
    response = client.post("/v1/commands", headers=headers, json=body)
    assert response.status_code == 202
    command = response.json()
    assert command["action"] == "pause" and command["state"] == "accepted"
    record = services.store.get(command["id"])
    assert record["actor"] == "operator-person" and record["role"] == "operator"
    assert record["credentialFingerprint"] == tokens["operator"].split("_")[1]
    assert wake.wait(redis_client, "test", wake.DAEMON, 0.01) == ["command:pause"]
    replay = client.post("/v1/commands", headers=headers, json=body)
    assert replay.json()["id"] == command["id"]
    fetched = client.get(f"/v1/commands/{command['id']}", headers=bearer(tokens["viewer"]))
    assert fetched.json()["id"] == command["id"]
    audit = client.get("/v1/audit", headers=bearer(tokens["viewer"])).json()["items"]
    assert audit[0]["actor"] == "operator-person" and audit[0]["action"] == "pause"


def test_reevaluate_resolves_an_identifier_and_checks_eligibility(api, redis_client):
    services, client, tokens = api
    fresh_daemon(redis_client)
    put_issue(redis_client, "i1", identifier="ENG-1", status="blocked", dispatch_seq=4)
    put_issue(redis_client, "i2", identifier="ENG-2", status="running")
    headers = bearer(tokens["operator"])
    ok = client.post("/v1/commands", headers=headers,
                     json={"action": "reevaluate", "target": "eng-1", "reason": "fixed the repo"})
    assert ok.status_code == 202
    assert ok.json()["target"] == {"issueId": "i1", "dispatchSeq": 4}
    busy = client.post("/v1/commands", headers=headers,
                       json={"action": "reevaluate", "target": "ENG-2", "reason": "please retry"})
    assert busy.status_code == 409
    missing = client.post("/v1/commands", headers=headers,
                          json={"action": "reevaluate", "target": "ENG-9", "reason": "please retry"})
    assert missing.status_code == 404
    stale = client.post("/v1/commands", headers=headers, json={
        "action": "reevaluate", "target": {"issueId": "i1", "dispatchSeq": 3},
        "reason": "please retry"})
    assert stale.status_code == 409


def test_commands_need_a_live_daemon_and_a_reason(api):
    _, client, tokens = api
    headers = bearer(tokens["operator"])
    dead = client.post("/v1/commands", headers=headers,
                       json={"action": "resume", "reason": "maintenance done"})
    assert dead.status_code == 503 and dead.json()["error"]["code"] == "daemon_unavailable"
    short = client.post("/v1/commands", headers=headers, json={"action": "resume", "reason": "x"})
    assert short.status_code == 422
    unknown = client.post("/v1/commands", headers=headers,
                          json={"action": "explode", "reason": "maintenance done"})
    assert unknown.status_code == 422


class FakeWorkflow:
    def __init__(self):
        self.revision = "7"

    def read(self):
        return {"revision": self.revision, "documentHash": "a" * 64, "values": {"x": 1},
                "redactedKeys": [], "observed": []}

    def validate(self, base_revision, patch):
        if base_revision != self.revision:
            raise WorkflowError("workflow_conflict", "The workflow changed.", 412)
        if "bad" in patch:
            raise WorkflowError("invalid_workflow", "The proposed workflow is invalid.", 422,
                                [{"key": "bad", "message": "nope"}])
        return {"baseRevision": base_revision, "documentHash": "b" * 64, "diff": []}


def test_workflow_read_and_publish_through_the_daemon(redis_client):
    services = make_services(redis_client, workflow=FakeWorkflow())
    client, tokens = client_for(services), tokens_for(services)
    fresh_daemon(redis_client)
    read = client.get("/v1/workflow", headers=bearer(tokens["viewer"]))
    assert read.status_code == 200 and read.headers["ETag"] == '"7"'
    admin = bearer(tokens["admin"])
    body = {"patch": {"polling.interval_ms": 5000}, "reason": "faster polling"}
    assert client.put("/v1/workflow", headers=admin, json=body).status_code == 428
    stale = client.put("/v1/workflow", headers=admin | {"If-Match": '"6"'}, json=body)
    assert stale.status_code == 412
    invalid = client.put("/v1/workflow", headers=admin | {"If-Match": '"7"'},
                         json={"patch": {"bad": 1}, "reason": "faster polling"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["diagnostics"] == [{"key": "bad", "message": "nope"}]
    mismatch = client.put("/v1/workflow", headers=admin | {"If-Match": '"7"'},
                          json=body | {"documentHash": "c" * 64})
    assert mismatch.status_code == 409
    accepted = client.put("/v1/workflow", headers=admin | {"If-Match": '"7"'}, json=body)
    assert accepted.status_code == 202
    record = services.store.get(accepted.json()["id"])
    assert record["action"] == "workflow" and record["actor"] == "admin-person"
    assert record["payload"]["documentHash"] == "b" * 64
    operator = client.put("/v1/workflow", headers=bearer(tokens["operator"]) | {"If-Match": '"7"'},
                          json=body)
    assert operator.status_code == 403


def test_workflow_unavailable_without_kubernetes(api):
    _, client, tokens = api
    response = client.get("/v1/workflow", headers=bearer(tokens["viewer"]))
    assert response.status_code == 503


def test_idle_daemon_heartbeat_age_matters(api, redis_client):
    _, client, tokens = api
    redis_client.set(state.HEARTBEAT_KEY, json.dumps(int(time.time() * 1000) - 120_000))
    body = client.get("/v1/overview", headers=bearer(tokens["viewer"])).json()
    assert body["daemon"]["alive"] is False and body["daemon"]["heartbeat_at"]
