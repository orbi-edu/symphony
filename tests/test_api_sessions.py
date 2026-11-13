"""Session reads, human messages and interrupts over `/v1/sessions`."""

from __future__ import annotations

import json

import pytest
from tests.api_support import bearer, client_for, make_services, tokens_for


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client)
    return services, client_for(services), tokens_for(services)


def open_session(services, sid="iss-1:1", state="running", **meta):
    services.sessions.open(sid, kind="implement", identifier="ENG-1", title="Fix login",
                           issue_id="iss-1", seq=1, repo="acme/app", model="m", **meta)
    if state != "starting":
        services.sessions.update(sid, state=state)
    return sid


def intents(services, sid):
    return services.sessions.read_intents(sid)


def test_list_filters_by_state_and_detail_returns_cursor_and_transcript(api):
    services, client, tokens = api
    open_session(services, "a:1", "running")
    open_session(services, "b:1", "idle")
    services.sessions.append_transcript("a:1", [{"type": "message", "id": "e1"}])
    headers = bearer(tokens["viewer"])
    listed = client.get("/v1/sessions?state=running", headers=headers).json()["items"]
    assert [item["sid"] for item in listed] == ["a:1"]
    both = client.get("/v1/sessions?state=running,idle&limit=10", headers=headers).json()["items"]
    assert {item["sid"] for item in both} == {"a:1", "b:1"}
    detail = client.get("/v1/sessions/a:1", headers=headers).json()
    assert detail["meta"]["state"] == "running"
    assert detail["transcript"] == [{"type": "message", "id": "e1"}]
    assert detail["last_event_id"] == services.sessions.last_event_id("a:1")
    assert client.get("/v1/sessions/zzz:9", headers=headers).status_code == 404
    assert client.get("/v1/sessions?state=bogus", headers=headers).status_code == 422


def test_session_list_etag_answers_304_until_something_changes(api):
    services, client, tokens = api
    open_session(services)
    headers = bearer(tokens["viewer"])
    first = client.get("/v1/sessions", headers=headers)
    etag = first.headers["ETag"]
    again = client.get("/v1/sessions", headers=headers | {"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    services.sessions.update("iss-1:1", turn=2)
    changed = client.get("/v1/sessions", headers=headers | {"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag


@pytest.mark.parametrize(
    "state,pending,expected",
    [("running", False, "steer"), ("idle", False, "follow_up"), ("waiting_input", True, "answer")],
)
def test_auto_mode_picks_the_intent_and_records_the_actor(api, state, pending, expected):
    services, client, tokens = api
    sid = open_session(services, state="running")
    if pending:
        services.sessions.request_input(sid, "req-1", "Which DB?", ["pg", "mysql"])
    else:
        services.sessions.update(sid, state=state)
    response = client.post(f"/v1/sessions/{sid}/messages", headers=bearer(tokens["operator"]),
                           json={"text": "use pg"})
    assert response.status_code == 202
    assert response.json()["kind"] == expected
    [(intent_id, kind, actor, payload)] = intents(services, sid)
    assert intent_id == response.json()["intent_id"]
    assert (kind, actor) == (expected, "operator-person")
    assert payload["text"] == "use pg"
    if pending:
        assert payload["request_id"] == "req-1"


def test_explicit_mode_interrupt_and_ended_session(api):
    services, client, tokens = api
    sid = open_session(services, state="running")
    headers = bearer(tokens["operator"])
    follow = client.post(f"/v1/sessions/{sid}/messages", headers=headers,
                         json={"text": "later", "mode": "follow_up"})
    assert follow.json()["kind"] == "follow_up"
    stop = client.post(f"/v1/sessions/{sid}/interrupt", headers=headers, json={"reason": "wrong"})
    assert stop.status_code == 202
    kinds = [(kind, payload) for _, kind, _, payload in intents(services, sid)]
    assert kinds[-1] == ("interrupt", {"reason": "wrong"})
    bad_mode = client.post(f"/v1/sessions/{sid}/messages", headers=headers,
                           json={"text": "x", "mode": "shout"})
    assert bad_mode.status_code == 422
    services.sessions.close(sid, "done")
    ended = client.post(f"/v1/sessions/{sid}/messages", headers=headers, json={"text": "hi"})
    assert ended.status_code == 409 and ended.json()["error"]["code"] == "session_ended"
    missing = client.post("/v1/sessions/nope:1/messages", headers=headers, json={"text": "hi"})
    assert missing.status_code == 404


def test_export_is_an_omp_session_file(api):
    services, client, tokens = api
    sid = open_session(services)
    entries = [{"type": "message", "id": "e1", "message": {"role": "user"}},
               {"type": "message", "id": "e2", "parentId": "e1"}]
    services.sessions.append_transcript(sid, entries)
    response = client.get(f"/v1/sessions/{sid}/export.jsonl", headers=bearer(tokens["viewer"]))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert lines[0]["type"] == "session" and lines[0]["version"] == 3 and lines[0]["cwd"]
    assert lines[1:] == entries
