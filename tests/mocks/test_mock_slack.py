"""Slack mock: spec-validated Web API + webhooks, and Symphony's `notify.post` against it."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import jsonschema
import pytest
from scripts.mocks.slack import server as slack
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from symphony import notify

ROOT = Path(__file__).resolve().parents[2]
SPEC = slack.load_spec()
TOKEN = slack.DEFAULT_TOKEN


def _check(method: str, body: dict) -> None:
    """Validate a Web API answer against the spec's success or error response schema."""
    verb, op = next(iter(SPEC["paths"][f"/{method}"].items()))
    key = "200" if body.get("ok") else "default"
    schema = dict(op["responses"][key]["schema"], definitions=SPEC["definitions"])
    # Swagger 2.0 schemas are JSON Schema draft 4.
    jsonschema.Draft4Validator(schema).validate(body)


@pytest.fixture
def mock(tmp_path):
    srv = slack.serve(0, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield base
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def slack_env(mock, monkeypatch):
    notify.reset()
    for var in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLACK_API_URL", f"{mock}/api")
    monkeypatch.setattr(notify, "SLACK_API_BASE", f"{mock}/api", raising=False)
    yield mock
    notify.reset()


def _messages(base: str) -> list[dict]:
    return httpx.get(f"{base}/_mock/messages").json()["messages"]


def test_notify_via_bot_token(slack_env, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("SLACK_CHANNEL", "#symphony")
    notify.configure({"slack": {"alice": "U0ALICE01"}})
    assert notify.post("main_red", "CI failed", link="https://x/1", github_logins=["alice"])
    [msg] = _messages(slack_env)
    assert msg["via"] == "chat.postMessage"
    assert msg["channel"] == slack.DEFAULT_CHANNEL["id"]
    assert "<@U0ALICE01>" in msg["text"] and "main is broken" in msg["text"]
    assert msg["args"]["mrkdwn"] is True and msg["args"]["unfurl_links"] is False


def test_notify_via_webhook(slack_env, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", slack_env + slack.DEFAULT_WEBHOOK)
    assert notify.post("main_green", "back to green")
    [msg] = _messages(slack_env)
    assert msg["via"] == "webhook" and "main is green again" in msg["text"]


@pytest.mark.parametrize(
    ("token", "channel"),
    [("xoxb-wrong", "symphony"), (TOKEN, "C0NOPE0000")],
)
def test_notify_handles_slack_errors(slack_env, monkeypatch, token, channel):
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("SLACK_CHANNEL", channel)
    assert notify.post("run_error", "boom") is False
    assert _messages(slack_env) == []


def test_notify_handles_rate_limit(slack_env, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("SLACK_CHANNEL", "symphony")
    httpx.post(f"{slack_env}/_mock/ratelimit", json={"enabled": True, "retry_after": 7})
    assert notify.post("run_error", "boom") is False
    httpx.post(f"{slack_env}/_mock/ratelimit", json={"enabled": False})
    assert notify.post("run_error", "boom") is True  # failure did not poison dedup


def test_web_api_errors_follow_spec(mock):
    url = f"{mock}/api/chat.postMessage"
    cases = [
        ({}, {"channel": "symphony", "text": "x"}, "not_authed"),
        ({"Authorization": "Bearer xoxb-nope"}, {"channel": "symphony", "text": "x"},
         "invalid_auth"),
        ({"Authorization": f"Bearer {TOKEN}"}, {"channel": "C0NOPE0000", "text": "x"},
         "channel_not_found"),
        ({"Authorization": f"Bearer {TOKEN}"}, {"channel": "symphony"}, "no_text"),
        ({"Authorization": f"Bearer {TOKEN}"}, {"channel": "symphony", "text": "x", "bogus": 1},
         "invalid_arg_name"),
        ({"Authorization": f"Bearer {TOKEN}"}, {"channel": "symphony", "text": "x",
                                                "mrkdwn": "maybe"}, "invalid_form_data"),
    ]
    for headers, payload, error in cases:
        r = httpx.post(url, headers=headers, json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body == {"ok": False, "error": error}
        _check("chat.postMessage", body)
    assert _messages(mock) == []


def test_post_message_success_form_and_json(mock):
    url = f"{mock}/api/chat.postMessage"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*oi*"}}]
    r1 = httpx.post(url, data={"token": TOKEN, "channel": "symphony", "text": "form",
                               "blocks": json.dumps(blocks), "mrkdwn": "true"})
    b1 = r1.json()
    assert b1["ok"] and b1["channel"] == slack.DEFAULT_CHANNEL["id"]
    _check("chat.postMessage", b1)
    r2 = httpx.post(url, headers={"Authorization": f"Bearer {TOKEN}"},
                    json={"channel": "C0SYMPHONY", "text": "reply", "thread_ts": b1["ts"],
                          "blocks": blocks})
    b2 = r2.json()
    _check("chat.postMessage", b2)
    assert b2["message"]["thread_ts"] == b1["ts"]
    msgs = _messages(mock)
    assert [m["text"] for m in msgs] == ["form", "reply"]
    assert msgs[0]["blocks"] == blocks and msgs[1]["thread_ts"] == b1["ts"]
    assert msgs[0]["ts"] == "1700000001.000001"  # deterministic


def test_post_message_to_a_user_id_opens_the_bots_dm(mock):
    url = f"{mock}/api/chat.postMessage"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    httpx.post(f"{mock}/_mock/users", json={"id": "U0ALICE01", "name": "alice", "email": "a@x.io"})
    first = httpx.post(url, headers=auth, json={"channel": "U0ALICE01", "text": "for you"}).json()
    _check("chat.postMessage", first)
    assert first["ok"] and first["channel"].startswith("D")
    again = httpx.post(url, headers=auth, json={"channel": "U0ALICE01", "text": "again"}).json()
    by_id = httpx.post(url, headers=auth, json={"channel": first["channel"], "text": "by id"}).json()
    assert again["channel"] == by_id["channel"] == first["channel"]
    unknown = httpx.post(url, headers=auth, json={"channel": "U0NOBODY01", "text": "x"}).json()
    assert unknown == {"ok": False, "error": "channel_not_found"}
    update = httpx.post(f"{mock}/api/chat.update", headers=auth,
                        json={"channel": first["channel"], "ts": first["ts"], "text": "edited"}).json()
    assert update["ok"]


def test_auth_test_and_lookup_by_email(mock):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = httpx.post(f"{mock}/api/auth.test", headers=auth).json()
    _check("auth.test", body)
    assert body["ok"]
    httpx.post(f"{mock}/_mock/users",
               json={"id": "U0ALICE01", "name": "alice", "email": "a@x.io"})
    found = httpx.get(f"{mock}/api/users.lookupByEmail", params={"email": "A@x.io"},
                      headers=auth).json()
    _check("users.lookupByEmail", found)
    assert found["user"]["id"] == "U0ALICE01"
    missing = httpx.get(f"{mock}/api/users.lookupByEmail", params={"email": "z@x.io"},
                        headers=auth).json()
    _check("users.lookupByEmail", missing)
    assert missing["error"] == "users_not_found"


def test_webhook_errors_and_rate_limit(mock):
    hook = mock + slack.DEFAULT_WEBHOOK
    r = httpx.post(hook, content=b"{nope", headers={"Content-Type": "application/json"})
    assert (r.status_code, r.text) == (400, "invalid_payload")
    r = httpx.post(hook, json={"username": "x"})
    assert (r.status_code, r.text) == (400, "no_text")
    r = httpx.post(f"{mock}/services/T0X/B0X/unknown", json={"text": "x"})
    assert (r.status_code, r.text) == (404, "no_service")
    httpx.post(f"{mock}/_mock/ratelimit", json={"enabled": True, "retry_after": 3})
    r = httpx.post(hook, json={"text": "x"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "3"
    r = httpx.post(f"{mock}/api/auth.test", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "3"
    assert r.json() == {"ok": False, "error": "ratelimited"}
    httpx.post(f"{mock}/_mock/reset")
    r = httpx.post(hook, json={"text": "ok now"})
    assert (r.status_code, r.text) == (200, "ok")
    assert [m["text"] for m in _messages(mock)] == ["ok now"]


def test_state_persists_and_cli_starts_fast(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.mocks.slack", "--port", "0", "--state", str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, text=True,
    )
    try:
        start = time.monotonic()
        line = proc.stdout.readline()
        assert time.monotonic() - start < 2.0
        base = line.strip().rsplit(" ", 1)[-1]
        httpx.post(f"{base}/api/chat.postMessage", data={"token": TOKEN,
                   "channel": "symphony", "text": "persist"})
    finally:
        proc.terminate()
        proc.wait()
    reloaded = slack.SlackMock(tmp_path)
    assert [m["text"] for m in reloaded.state["messages"]] == ["persist"]


# Socket Mode and the assistant's methods --------------------------------------------------

APP_TOKEN = slack.DEFAULT_APP_TOKEN


def _open(base: str, token: str | None = APP_TOKEN, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(f"{base}/api/apps.connections.open", headers=headers, **kwargs).json()


def _api(base: str, method: str, body: dict, *, get: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if get:
        out = httpx.get(f"{base}/api/{method}", params=body, headers=headers).json()
    else:
        out = httpx.post(f"{base}/api/{method}", json=body, headers=headers).json()
    _check(method, out)
    return out


def test_apps_connections_open_auth_follows_the_docs(mock):
    assert _open(mock, None) == {"ok": False, "error": "not_authed"}
    # The app token as a POST argument is an error: the header only.
    assert _open(mock, None, data={"token": APP_TOKEN}) == {"ok": False, "error": "not_authed"}
    assert _open(mock, "xapp-wrong") == {"ok": False, "error": "invalid_auth"}
    assert _open(mock, TOKEN) == {"ok": False, "error": "not_allowed_token_type"}
    httpx.post(f"{mock}/_mock/app_tokens", json={"token": "xapp-second"})
    out = _open(mock, "xapp-second")
    assert out["ok"] and out["url"].startswith(f"ws://{mock.removeprefix('http://')}/link/?ticket=")


def test_socket_hello_envelopes_and_acks(mock):
    url = _open(mock)["url"]
    with ws_connect(url) as ws:
        hello = json.loads(ws.recv(timeout=3))
        assert hello["type"] == "hello" and hello["num_connections"] == 1
        assert hello["connection_info"]["app_id"] == slack.APP_ID
        assert "approximate_connection_time" in hello["debug_info"]

        sent = httpx.post(f"{mock}/_control/socket/event", json={"event": {
            "type": "app_mention", "user": "U0ALICE01", "channel": "symphony",
            "text": f"<@{slack.BOT_USER_ID}> hi"}}).json()
        envelope = json.loads(ws.recv(timeout=3))
        assert set(envelope) == {"payload", "envelope_id", "type", "accepts_response_payload"}
        assert envelope["type"] == "events_api" and envelope["envelope_id"] == sent["envelope_id"]
        payload = envelope["payload"]
        assert payload["type"] == "event_callback" and payload["api_app_id"] == slack.APP_ID
        event = payload["event"]
        assert set(slack.EVENT_REQUIRED["app_mention"]) <= set(event)
        assert event["channel"] == slack.DEFAULT_CHANNEL["id"] and event["ts"] == sent["ts"]

        ws.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
        ws.send(json.dumps({"envelope_id": envelope["envelope_id"]}))  # a duplicate
        ws.send(json.dumps({"envelope_id": "nope", "extra": 1}))
        ws.send("not json")
        socket = _wait_for(lambda: (s := httpx.get(f"{mock}/_control/socket").json())
                           and len(s["errors"]) == 3 and s)
    [ack] = socket["acks"]
    assert ack["envelope_id"] == envelope["envelope_id"] and ack["delay_s"] < 3
    assert [e["error"] for e in socket["errors"]] == ["duplicate_ack", "unknown_fields",
                                                      "not_an_ack"]


def test_socket_ticket_is_single_use_and_no_connection_is_409(mock):
    url = _open(mock)["url"]
    r = httpx.post(f"{mock}/_control/socket/event",
                   json={"event": {"type": "message", "user": "U1", "channel": "symphony",
                                   "text": "x"}})
    assert r.status_code == 409 and r.json() == {"error": "no_connection"}
    with ws_connect(url) as ws:
        ws.recv(timeout=3)
    with pytest.raises(InvalidStatus) as exc:
        ws_connect(url)
    assert exc.value.response.status_code == 401


def test_socket_events_are_checked_against_the_docs(mock):
    with ws_connect(_open(mock)["url"]) as ws:
        ws.recv(timeout=3)
        httpx.post(f"{mock}/_mock/channels", json={"id": "D0MOCKDM01", "name": "dm"})
        r = httpx.post(f"{mock}/_control/socket/event", json={"event": {
            "type": "app_mention", "user": "U1", "channel": "D0MOCKDM01", "text": "x"}})
        assert r.json() == {"error": "app_mention_not_sent_in_dm"}
        r = httpx.post(f"{mock}/_control/socket/event", json={"event": {
            "type": "app_mention", "channel": "symphony", "text": "x"}})
        assert r.json() == {"error": "missing_fields", "fields": ["user"]}
        r = httpx.post(f"{mock}/_control/socket/event", json={"event": {
            "type": "message", "user": "U1", "channel": "D0MOCKDM01", "text": "hi"}})
        event = json.loads(ws.recv(timeout=3))["payload"]["event"]
        assert event["channel_type"] == "im"
        assert set(slack.EVENT_REQUIRED["message"]) <= set(event)


def test_block_actions_need_a_posted_button(mock):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Two tickets"}},
              {"type": "actions", "block_id": "b1", "elements": [
                  {"type": "button", "action_id": "proposal:p1:create", "value": "v",
                   "text": {"type": "plain_text", "text": "Create"}, "style": "primary"}]}]
    posted = _api(mock, "chat.postMessage", {"channel": "symphony", "text": "Two tickets",
                                              "blocks": blocks})
    with ws_connect(_open(mock)["url"]) as ws:
        ws.recv(timeout=3)
        click = {"user": "U0ALICE01", "channel": "symphony", "message_ts": posted["ts"]}
        r = httpx.post(f"{mock}/_control/socket/interactive",
                       json=dict(click, action_id="proposal:p1:other"))
        assert r.status_code == 400
        r = httpx.post(f"{mock}/_control/socket/interactive",
                       json=dict(click, action_id="proposal:p1:create"))
        envelope = json.loads(ws.recv(timeout=3))
    assert envelope["type"] == "interactive" and envelope["accepts_response_payload"] is True
    payload = envelope["payload"]
    for field in ("type", "trigger_id", "user", "team", "container", "api_app_id", "actions"):
        assert field in payload  # slack-block_actions-payload.md, required properties
    assert payload["type"] == "block_actions"
    assert payload["container"] == {"type": "message", "message_ts": posted["ts"],
                                    "channel_id": slack.DEFAULT_CHANNEL["id"],
                                    "is_ephemeral": False}
    [action] = payload["actions"]
    assert (action["action_id"], action["block_id"], action["value"], action["type"]) == (
        "proposal:p1:create", "b1", "v", "button")


def test_disconnect_message_then_close(mock):
    with ws_connect(_open(mock)["url"]) as ws:
        ws.recv(timeout=3)
        httpx.post(f"{mock}/_control/socket/disconnect",
                   json={"reason": "warning", "close_after": 0.1})
        message = json.loads(ws.recv(timeout=3))
        assert message == {"type": "disconnect", "reason": "warning",
                           "debug_info": {"host": "applink-mock"}}
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=3)


def test_chat_update_ephemeral_and_replies_follow_spec(mock):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "a"}}]
    root = _api(mock, "chat.postMessage", {"channel": "symphony", "text": "root",
                                           "blocks": blocks})
    reply = _api(mock, "chat.postMessage", {"channel": "symphony", "text": "reply",
                                            "thread_ts": root["ts"]})
    updated = _api(mock, "chat.update", {"channel": "symphony", "ts": root["ts"],
                                         "text": "edited"})
    assert updated["ok"] and updated["text"] == "edited"
    [update] = httpx.get(f"{mock}/_mock/updates").json()["updates"]
    assert update["blocks"] is None  # text without blocks removes them (chat.update docs)
    assert _api(mock, "chat.update", {"channel": "symphony", "ts": "1.000001",
                                      "text": "x"})["error"] == "message_not_found"
    assert _api(mock, "chat.update", {"channel": "symphony", "ts": root["ts"]})["error"] == \
        "no_text"

    note = _api(mock, "chat.postEphemeral", {"channel": "symphony", "user": "U0ALICE01",
                                             "text": "only you", "thread_ts": root["ts"]})
    assert note["ok"] and note["message_ts"]
    assert _api(mock, "chat.postEphemeral", {"channel": "symphony",
                                             "user": "U0ALICE01"})["error"] == "no_text"

    replies = _api(mock, "conversations.replies", {"channel": slack.DEFAULT_CHANNEL["id"],
                                                   "ts": root["ts"]}, get=True)
    assert [m["text"] for m in replies["messages"]] == ["edited", "reply"]
    assert replies["messages"][0]["reply_count"] == 1
    assert replies["messages"][1]["ts"] == reply["ts"]
    missing = _api(mock, "conversations.replies", {"channel": "symphony", "ts": "1.000009"},
                   get=True)
    assert missing["error"] == "thread_not_found"
    bad = _api(mock, "conversations.replies", {"channel": "symphony", "ts": "abc"}, get=True)
    assert bad["error"] == "invalid_form_data"


def _wait_for(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_vendored_slack_docs_match_sources():
    specs = ROOT / "scripts" / "mocks" / "specs"
    rows = re.findall(r"^\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|$",
                      (specs / "SOURCES.md").read_text(), re.M)
    mine = {name: sha for name, sha in rows if name.startswith("slack-")}
    assert {"slack-socket-mode.md", "slack-apps.connections.open.md", "slack-app_mention.md",
            "slack-message.im.md", "slack-block_actions-payload.md", "slack-chat.update.md",
            "slack-chat.postEphemeral.md", "slack-conversations.replies.md",
            "slack-auth.test.md", "slack-files.getUploadURLExternal.md",
            "slack-files.completeUploadExternal.md", "slack-working-with-files.md"} <= set(mine)
    for name, sha in mine.items():
        assert hashlib.sha256((specs / name).read_bytes()).hexdigest() == sha, name


def test_unserved_methods_are_501(mock):
    r = httpx.post(f"{mock}/api/conversations.history", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 501
    assert httpx.post(f"{mock}/api/no.such.method").json()["error"] == "unknown_method"


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _upload_url(base: str, length: int, name: str = "a.mp4") -> dict:
    r = httpx.post(f"{base}/api/files.getUploadURLExternal", headers=_auth(),
                   data={"filename": name, "length": str(length)})
    return r.json()


def test_external_upload_shares_the_file_in_a_thread(mock):
    ticket = _upload_url(mock, 5)
    assert ticket["ok"] and ticket["upload_url"].startswith(mock + "/upload/v1/")
    r = httpx.post(ticket["upload_url"], content=b"hello")
    assert r.status_code == 200 and r.text == "OK - 5"
    done = httpx.post(f"{mock}/api/files.completeUploadExternal", headers=_auth(), json={
        "files": [{"id": ticket["file_id"], "title": "Hi"}],
        "channel_id": slack.DEFAULT_CHANNEL["id"], "thread_ts": "1700000001.000001",
        "initial_comment": "here"}).json()
    assert done["ok"] and done["files"][0]["id"] == ticket["file_id"]
    assert done["files"][0]["title"] == "Hi" and done["files"][0]["permalink"]
    again = httpx.post(f"{mock}/api/files.completeUploadExternal", headers=_auth(), json={
        "files": [{"id": ticket["file_id"]}], "channel_id": slack.DEFAULT_CHANNEL["id"]}).json()
    assert again == {"ok": False, "error": "file_not_found"}  # "can only be called once"


def test_external_upload_errors(mock):
    # a form-only method ignores a JSON body's arguments
    r = httpx.post(f"{mock}/api/files.getUploadURLExternal", headers=_auth(),
                   json={"filename": "a.mp4", "length": 5})
    assert r.json() == {"ok": False, "error": "missing_argument"}
    assert _upload_url(mock, 0)["error"] == "missing_argument"
    assert httpx.post(f"{mock}/api/files.getUploadURLExternal",
                      data={"filename": "a", "length": "1"}).json()["error"] == "not_authed"
    ticket = _upload_url(mock, 5)
    r = httpx.post(ticket["upload_url"], content=b"too long")
    assert r.status_code == 400 and "length mismatch" in r.text
    complete = {"files": json.dumps([{"id": ticket["file_id"]}]),
                "channel_id": slack.DEFAULT_CHANNEL["id"]}
    assert httpx.post(f"{mock}/api/files.completeUploadExternal", headers=_auth(),
                      data=complete).json()["error"] == "file_not_found"  # nothing uploaded
    assert httpx.post(ticket["upload_url"], content=b"hello").status_code == 200
    assert httpx.post(f"{mock}/api/files.completeUploadExternal", headers=_auth(),
                      data={**complete, "channel_id": "C0NOSUCH"}).json()["error"] == "channel_not_found"
    assert httpx.post(f"{mock}/api/files.completeUploadExternal", headers=_auth(), data={
        "files": complete["files"], "thread_ts": "1700000001.000001"}).json()["error"] == "invalid_arguments"
    assert httpx.post(f"{mock}/api/files.completeUploadExternal", headers=_auth(),
                      data={**complete, "bogus": "1"}).json()["error"] == "invalid_arg_name"
    assert httpx.post(f"{mock}/upload/v1/unknown", content=b"x").status_code == 404
