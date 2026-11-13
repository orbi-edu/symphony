"""Human takeover (internals §12): hold/release commands and how held tickets are shown."""

from __future__ import annotations

import json

import pytest
from tests.api_support import bearer, client_for, fresh_daemon, make_services, tokens_for

from symphony import state, wake
from symphony.admin_auth import authorize_command


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client)
    fresh_daemon(redis_client)
    redis_client.set(state.issue_key("i1"), json.dumps(
        {"dispatch_seq": 2, "attempt": 1, "identifier": "ENG-1", "status": "running"}))
    services.sessions.open("i1:2", kind="implement")
    return services, client_for(services), tokens_for(services)


def command(client, token, action, target, reason="taking this one myself"):
    return client.post("/v1/commands", headers=bearer(token),
                       json={"action": action, "target": target, "reason": reason})


def test_hold_is_a_durable_command_by_identifier(api, redis_client):
    services, client, tokens = api
    response = command(client, tokens["operator"], "hold", "eng-1")
    assert response.status_code == 202
    body = response.json()
    assert body["action"] == "hold" and body["target"] == {"issueId": "i1"}
    record = services.store.get(body["id"])
    assert record["actor"] == "operator-person" and record["reason"] == "taking this one myself"
    assert authorize_command(record, "test", services.tokens)
    assert wake.wait(redis_client, "test", wake.DAEMON, 0.01) == ["command:hold"]
    audit = client.get("/v1/audit", headers=bearer(tokens["viewer"])).json()["items"]
    assert audit[0]["action"] == "hold" and audit[0]["target"] == "i1"


def test_untracked_ticket_can_be_held_by_issue_id(api):
    _, client, tokens = api
    response = command(client, tokens["operator"], "hold", {"issueId": "never-dispatched"})
    assert response.status_code == 202
    assert response.json()["target"] == {"issueId": "never-dispatched"}
    assert command(client, tokens["operator"], "hold", "ENG-404").status_code == 404
    bad = command(client, tokens["operator"], "hold", {"issueId": "i1", "dispatchSeq": 2})
    assert bad.status_code == 422
    assert command(client, tokens["operator"], "release", None).status_code == 422


def test_hold_twice_and_release_unheld_are_conflicts(api, redis_client):
    _, client, tokens = api
    not_held = command(client, tokens["operator"], "release", "ENG-1", "back to the robots")
    assert not_held.status_code == 409 and not_held.json()["error"]["code"] == "not_held"
    redis_client.set(state.hold_key("i1"), json.dumps(
        {"by": "alice", "reason": "debugging", "at": 1_700_000_000_000}))
    twice = command(client, tokens["operator"], "hold", "ENG-1")
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "already_held"
    release = command(client, tokens["operator"], "release", "ENG-1", "back to the robots")
    assert release.status_code == 202 and release.json()["action"] == "release"


def test_viewers_cannot_hold(api):
    _, client, tokens = api
    assert command(client, tokens["viewer"], "hold", "ENG-1").status_code == 403


def test_overview_and_issues_show_who_holds_what(api, redis_client):
    _, client, tokens = api
    hold = {"by": "alice", "reason": "debugging the flaky test", "at": 1_700_000_000_000}
    redis_client.set(state.hold_key("i1"), json.dumps(hold))
    redis_client.set(state.hold_key("untracked"), json.dumps({**hold, "by": "bo"}))
    headers = bearer(tokens["viewer"])
    overview = client.get("/v1/overview", headers=headers).json()
    assert overview["counts"]["held"] == 2 and overview["counts"]["running"] == 0
    by_issue = {item["issue_id"]: item for item in overview["held"]}
    assert by_issue["i1"] == {"issue_id": "i1", "identifier": "ENG-1", "by": "alice",
                              "reason": "debugging the flaky test",
                              "at": "2023-11-14T22:13:20Z"}
    assert by_issue["untracked"]["identifier"] is None
    [item] = client.get("/v1/issues", headers=headers).json()["items"]
    assert item["phase"] == "held"
    assert item["held"] == {"by": "alice", "reason": "debugging the flaky test",
                            "at": "2023-11-14T22:13:20Z"}
    # A plain read: the hold must not gain a TTL from being displayed.
    assert redis_client.ttl(state.hold_key("i1")) == -1


def test_mcp_hold_and_release_tools(api, redis_client):
    services, client, tokens = api

    def call(token, name, arguments):
        response = client.post("/mcp", headers=bearer(token), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        return response.json()["result"]

    refused = call(tokens["viewer"], "hold", {"issue": "ENG-1", "reason": "taking it over"})
    assert refused["isError"] is True
    held = call(tokens["operator"], "hold", {"issue": "ENG-1", "reason": "taking it over"})
    assert held["structuredContent"]["action"] == "hold"
    assert services.store.get(held["structuredContent"]["id"])["actor"] == "operator-person"
    not_held = call(tokens["operator"], "release", {"issue": "ENG-1", "reason": "giving it back"})
    assert not_held["isError"] is True and "not_held" in not_held["content"][0]["text"]
