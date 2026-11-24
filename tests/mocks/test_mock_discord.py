"""Discord mock: spec-validated Execute Webhook + Create Message, and Symphony's
`notify.post` against it."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from scripts.mocks.discord import gateway as mock_gateway
from scripts.mocks.discord import server as discord
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from symphony import notify

ROOT = Path(__file__).resolve().parents[2]
TOKEN = discord.DEFAULT_TOKEN
CHANNEL = discord.DEFAULT_CHANNEL["id"]
ALICE = "1500000000000000001"
BOB = "1500000000000000002"
SECRETS = ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_WEBHOOK_URL",
           "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL")


def _check(status: int, body) -> None:
    """Validate an answer against the spec's schema for that status."""
    name = {200: "MessageResponse", 429: "RatelimitedResponse"}.get(status, "Error")
    discord.validator(f"#/components/schemas/{name}").validate(body)


@pytest.fixture
def mock(tmp_path):
    srv = discord.serve(0, tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    for user_id, name in ((ALICE, "alice"), (BOB, "bob")):
        httpx.post(f"{base}/_mock/users", json={"id": user_id, "username": name})
    yield base
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env(mock, monkeypatch, tmp_path):
    notify.reset()
    for var in SECRETS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_MOUNT", str(tmp_path / "no-workflow"))
    monkeypatch.setenv("DISCORD_API_URL", f"{mock}/api/v10")
    yield mock
    notify.reset()


@pytest.fixture
def bot(env, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL)
    return env


def _messages(base: str) -> list[dict]:
    return httpx.get(f"{base}/_mock/messages").json()["messages"]


def _api(base: str, path: str, **kw) -> httpx.Response:
    return httpx.post(f"{base}/api/v10{path}", **kw)


# notify.post --------------------------------------------------------------------------


def test_notify_via_bot_mentions_only_the_mapped_user(bot):
    notify.configure({"slack": {"alice": "U0ALICE"}, "discord": {"Alice": ALICE}})
    text = f"PR #12 needs you <@{BOB}> @everyone"
    assert notify.post("back_to_author", text, link="https://github.com/o/r/pull/12",
                       github_logins=["alice"])
    [msg] = _messages(bot)
    assert msg["via"] == "bot" and msg["channel_id"] == CHANNEL
    assert msg["content"] == (
        f"\N{LEFTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} **PR returned to its author** "
        f"<@{ALICE}>\n{text}\n[Open](<https://github.com/o/r/pull/12>)"
    )
    assert msg["allowed_mentions"] == {"parse": [], "users": [ALICE]}
    # Bob is in the text and @everyone too, but only Alice is pinged.
    assert msg["mentions"] == [ALICE] and msg["mention_everyone"] is False
    assert msg["user_agent"].startswith("DiscordBot (")


def test_notify_discord_mentions_several_mapped_users(bot):
    """Multiple github_logins produces mentions for all mapped Discord users."""
    notify.configure({"discord": {"alice": ALICE, "bob": BOB}})
    assert notify.post("needs_approval", "waiting for review", github_logins=["alice", "bob"])
    [msg] = _messages(bot)
    assert f"<@{ALICE}>" in msg["content"] and f"<@{BOB}>" in msg["content"]
    assert set(msg["allowed_mentions"]["users"]) == {ALICE, BOB}


def test_notify_via_webhook_without_mapping_pings_nobody(env, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", env + discord.DEFAULT_WEBHOOK_PATH)
    assert notify.post("main_green", f"fixed by <@{ALICE}>", github_logins=["carol"])
    [msg] = _messages(env)
    assert msg["via"] == "webhook"
    assert msg["content"] == f"\N{LARGE GREEN CIRCLE} **main is green again**\nfixed by <@{ALICE}>"
    assert msg["allowed_mentions"] == {"parse": []} and msg["mentions"] == []


def test_notify_truncates_to_discord_limit(bot):
    link = "https://ci.example/run/1"
    assert notify.post("run_error", "x" * 5000, link=link)
    [msg] = _messages(bot)
    content = msg["content"]
    assert len(content) == notify.DISCORD_MAX_CHARS
    assert content.startswith("\N{CROSS MARK} **Run failed with an error**\nxxx")
    assert content.endswith(f"x…\n[Open](<{link}>)")


@pytest.mark.parametrize(
    ("token", "channel"),
    [("wrong-token", CHANNEL), (TOKEN, "1100000000000009999"), (TOKEN, "not-a-snowflake")],
)
def test_notify_handles_discord_errors(env, monkeypatch, token, channel):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", channel)
    assert notify.post("run_error", "boom") is False
    assert _messages(env) == []


def test_notify_webhook_with_wrong_token_fails(env, monkeypatch):
    hook = discord.DEFAULT_WEBHOOK
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", f"{env}/api/webhooks/{hook['id']}/nope")
    assert notify.post("run_error", "boom") is False


def test_notify_retries_once_after_retry_after(bot):
    httpx.post(f"{bot}/_mock/ratelimit", json={"times": 1, "retry_after": 0.2})
    started = time.monotonic()
    assert notify.post("main_red", "CI failed") is True
    assert time.monotonic() - started >= 0.2
    assert len(_messages(bot)) == 1


def test_notify_gives_up_after_second_429_and_long_waits(bot):
    httpx.post(f"{bot}/_mock/ratelimit", json={"times": 2, "retry_after": 0.1})
    assert notify.post("main_red", "CI failed") is False
    httpx.post(f"{bot}/_mock/ratelimit", json={"times": 1, "retry_after": 60})
    started = time.monotonic()
    assert notify.post("main_red", "CI failed") is False
    assert time.monotonic() - started < 2.0
    assert notify.post("main_red", "CI failed") is True  # failures did not poison dedup
    assert len(_messages(bot)) == 1


def test_both_channels_get_each_event(bot, monkeypatch):
    from scripts.mocks.slack import server as slack

    srv = slack.serve(0, None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    slack_base = f"http://127.0.0.1:{srv.server_port}"
    try:
        monkeypatch.setenv("SLACK_API_URL", f"{slack_base}/api")
        monkeypatch.setenv("SLACK_BOT_TOKEN", slack.DEFAULT_TOKEN)
        monkeypatch.setenv("SLACK_CHANNEL", "symphony")
        notify.configure({"slack": {"alice": "U0ALICE01"}, "discord": {"alice": ALICE}})
        assert notify.post("needs_maintainer", "arbiter undecided", github_logins=["alice"])
        [on_slack] = httpx.get(f"{slack_base}/_mock/messages").json()["messages"]
        [on_discord] = _messages(bot)
        assert on_slack["text"] == ":rotating_light: *A maintainer is needed* <@U0ALICE01>\narbiter undecided"
        assert on_discord["content"] == (
            f"\N{POLICE CARS REVOLVING LIGHT} **A maintainer is needed** <@{ALICE}>\narbiter undecided"
        )
        # A Discord failure does not hide the Slack delivery, and dedup is per channel.
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "wrong-token")
        assert notify.post("run_error", "boom") is True
        monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
        assert notify.post("run_error", "boom") is True
        assert len(httpx.get(f"{slack_base}/_mock/messages").json()["messages"]) == 2
        assert len(_messages(bot)) == 2
    finally:
        srv.shutdown()
        srv.server_close()


def test_notify_dm_to_the_assignee_pings_no_role_or_everyone(bot):
    notify.configure({"discord": {"alice": ALICE, "alice@example.com": ALICE}})
    text = f"PR #12 went back <@{BOB}> @everyone"
    assert notify.post("back_to_author", text, direct_to=["alice@example.com", "alice"])
    on_channel, dm = _messages(bot)
    assert on_channel["channel_id"] == CHANNEL
    assert dm["channel_id"] != CHANNEL and dm["via"] == "bot"
    assert dm["content"] == (
        f"\N{LEFTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} **PR returned to its author**\n{text}")
    assert dm["allowed_mentions"] == {"parse": []}
    assert dm["mentions"] == [] and dm["mention_everyone"] is False
    # The next event reuses the DM channel.
    assert notify.post("run_error", "boom", direct_to=["alice"])
    assert [m["channel_id"] for m in _messages(bot)][2:] == [CHANNEL, dm["channel_id"]]
    dms = [c for c in httpx.get(f"{bot}/_mock/state").json()["channels"] if c.get("recipient")]
    assert [c["recipient"] for c in dms] == [ALICE]


def test_notify_no_dm_for_an_unmapped_assignee_or_a_webhook(env, monkeypatch):
    notify.configure({"discord": {"alice": ALICE}})
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", env + discord.DEFAULT_WEBHOOK_PATH)
    assert notify.post("run_error", "boom", direct_to=["alice"])  # a webhook cannot DM
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL)
    assert notify.post("run_error", "boom again", direct_to=["carol"])
    assert [m["channel_id"] for m in _messages(env)] == [CHANNEL, CHANNEL]


# the mock itself ----------------------------------------------------------------------


def test_create_message_success_matches_spec(mock):
    r = _api(mock, f"/channels/{CHANNEL}/messages", headers={"Authorization": f"Bot {TOKEN}"},
             json={"content": f"hi <@{ALICE}> <@{BOB}>"})
    assert r.status_code == 200
    _check(200, r.json())
    # Bot messages parse every mention type by default.
    assert [u["id"] for u in r.json()["mentions"]] == [ALICE, BOB]
    assert r.json()["author"]["bot"] is True


def test_create_dm_follows_the_docs(mock):
    auth = {"Authorization": f"Bot {TOKEN}"}
    r = _api(mock, "/users/@me/channels", headers=auth, json={"recipient_id": ALICE})
    assert r.status_code == 200
    discord.validator("#/components/schemas/PrivateChannelResponse").validate(r.json())
    assert r.json()["type"] == 1 and r.json()["recipients"][0]["id"] == ALICE
    # "if one already exists, it will be returned instead"
    again = _api(mock, "/users/@me/channels", headers=auth, json={"recipient_id": ALICE})
    assert again.json()["id"] == r.json()["id"]
    sent = _api(mock, f"/channels/{r.json()['id']}/messages", headers=auth, json={"content": "hi"})
    assert sent.status_code == 200
    unknown = _api(mock, "/users/@me/channels", headers=auth, json={"recipient_id": "1500000000000000999"})
    assert (unknown.status_code, unknown.json()["code"]) == (400, 50033)
    assert _api(mock, "/users/@me/channels", json={"recipient_id": ALICE}).status_code == 401


def test_execute_webhook_wait_and_no_wait(mock):
    hook = mock + discord.DEFAULT_WEBHOOK_PATH
    r = httpx.post(hook, json={"content": "no wait"})
    assert (r.status_code, r.content) == (204, b"")
    r = httpx.post(hook, params={"wait": "True"}, json={"content": "@everyone hi"})
    assert r.status_code == 200
    _check(200, r.json())
    assert r.json()["webhook_id"] == discord.DEFAULT_WEBHOOK["id"]
    assert r.json()["mention_everyone"] is False  # webhooks parse only users by default
    r = httpx.post(f"{mock}/api/v10/webhooks/{discord.DEFAULT_WEBHOOK['id']}/"
                   f"{discord.DEFAULT_WEBHOOK['token']}", params={"wait": "maybe"},
                   json={"content": "x"})
    assert r.status_code == 400 and r.json()["errors"]["wait"]["_errors"][0]["code"] == "BOOLEAN_TYPE_CONVERT"
    assert [m["content"] for m in _messages(mock)] == ["no wait", "@everyone hi"]


def test_errors_follow_the_docs(mock):
    messages = f"/channels/{CHANNEL}/messages"
    auth = {"Authorization": f"Bot {TOKEN}"}
    httpx.post(f"{mock}/_mock/channels", json={"id": "1100000000000000002", "name": "private"})
    cases = [
        (messages, {}, {"content": "x"}, 401, 0),
        (messages, {"Authorization": "Bot nope"}, {"content": "x"}, 401, 0),
        (messages, {"Authorization": f"Bearer {TOKEN}"}, {"content": "x"}, 401, 0),
        ("/channels/1100000000000009999/messages", auth, {"content": "x"}, 404, 10003),
        ("/channels/1100000000000000002/messages", auth, {"content": "x"}, 403, 50001),
        (messages, auth, {}, 400, 50006),
        (messages, auth, {"content": "x" * 2001}, 400, 50035),
        (messages, auth, {"content": 7}, 400, 50035),
        (messages, auth, {"content": "x", "allowed_mentions": {"parse": ["users"], "users": [ALICE]}},
         400, 50035),
        ("/webhooks/1200000000000009999/t", {}, {"content": "x"}, 404, 10015),
        (f"/webhooks/{discord.DEFAULT_WEBHOOK['id']}/wrong", {}, {"content": "x"}, 401, 50027),
    ]
    for path, headers, body, status, code in cases:
        r = _api(mock, path, headers=headers, json=body)
        assert (r.status_code, r.json()["code"]) == (status, code), (path, body)
        _check(status, r.json())
    r = _api(mock, messages, headers={**auth, "Content-Type": "application/json"}, content=b"{nope")
    assert (r.status_code, r.json()["code"]) == (400, 50109)
    r = _api(mock, messages, headers=auth, json={"content": "x" * 2001})
    assert r.json()["errors"] == {"content": {"_errors": [
        {"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 2000 or fewer in length."}]}}
    assert httpx.get(f"{mock}/api/v10/nothing").status_code == 404
    assert _messages(mock) == []


def test_rate_limit_answers_like_the_docs(mock):
    httpx.post(f"{mock}/_mock/ratelimit", json={"times": 1, "retry_after": 64.57})
    r = _api(mock, f"/channels/{CHANNEL}/messages", headers={"Authorization": f"Bot {TOKEN}"},
             json={"content": "x"})
    assert r.status_code == 429
    _check(429, r.json())
    assert r.json()["retry_after"] == 64.57 and r.json()["global"] is False
    assert r.headers["Retry-After"] == "65" and r.headers["X-RateLimit-Remaining"] == "0"
    r = _api(mock, f"/channels/{CHANNEL}/messages", headers={"Authorization": f"Bot {TOKEN}"},
             json={"content": "x"})
    assert r.status_code == 200


def test_vendored_specs_match_sources():
    specs = ROOT / "scripts" / "mocks" / "specs"
    rows = re.findall(r"^\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|$", (specs / "SOURCES.md").read_text(), re.M)
    mine = {name: sha for name, sha in rows if name.startswith("discord")}
    assert {"discord-openapi.json.gz", "discord-rate-limits.md"} <= set(mine)
    for name, sha in mine.items():
        data = (specs / name).read_bytes()
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        assert hashlib.sha256(data).hexdigest() == sha, name


def test_state_persists_and_cli_starts_fast(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.mocks.discord", "--port", "0", "--state", str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, text=True,
    )
    try:
        start = time.monotonic()
        line = proc.stdout.readline()
        assert time.monotonic() - start < 2.0
        base = line.strip().rsplit(" ", 1)[-1]
        httpx.post(base + discord.DEFAULT_WEBHOOK_PATH, json={"content": "persist"})
    finally:
        proc.terminate()
        proc.wait()
    reloaded = discord.DiscordMock(tmp_path)
    assert [m["content"] for m in reloaded.state["messages"]] == ["persist"]


# Gateway, interactions and edits ----------------------------------------------------------

BOT_AUTH = {"Authorization": f"Bot {TOKEN}"}
IDENTIFY = {"op": 2, "d": {"token": TOKEN, "intents": (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15),
                           "properties": {"os": "linux", "browser": "t", "device": "t"}}}


def _gateway_url(base: str) -> str:
    info = httpx.get(f"{base}/api/v10/gateway/bot", headers=BOT_AUTH).json()
    return info["url"].rstrip("/") + "/?v=10&encoding=json"


def _recv(ws) -> dict:
    return json.loads(ws.recv(timeout=3))


def _ready(ws, identify: dict = IDENTIFY) -> dict:
    assert _recv(ws)["op"] == 10
    ws.send(json.dumps(identify))
    ready = _recv(ws)
    assert (ready["op"], ready["t"], ready["s"]) == (0, "READY", 1)
    return ready["d"]


def _closed_with(ws, payload: dict | str | None = None) -> int:
    if payload is not None:
        ws.send(payload if isinstance(payload, str) else json.dumps(payload))
    with pytest.raises(ConnectionClosed) as exc:
        while True:
            ws.recv(timeout=3)
    return exc.value.rcvd.code


def test_gateway_bot_and_application_follow_spec(mock):
    r = httpx.get(f"{mock}/api/v10/gateway/bot", headers=BOT_AUTH)
    assert r.status_code == 200 and r.headers["X-RateLimit-Bucket"]
    discord.validator("#/components/schemas/GatewayBotResponse").validate(r.json())
    assert r.json()["url"] == f"ws://{mock.removeprefix('http://')}"
    app = httpx.get(f"{mock}/api/v10/applications/@me", headers=BOT_AUTH).json()
    discord.validator("#/components/schemas/PrivateApplicationResponse").validate(app)
    assert app["flags"] == discord.DEFAULT_APP_FLAGS == 1 << 18
    httpx.post(f"{mock}/_mock/application", json={"flags": 1 << 19})
    assert httpx.get(f"{mock}/api/v10/applications/@me", headers=BOT_AUTH).json()["flags"] == 1 << 19
    for path in ("/gateway/bot", "/applications/@me"):
        r = httpx.get(f"{mock}/api/v10{path}")
        assert r.status_code == 401
        _check(401, r.json())


def test_gateway_hello_identify_heartbeat_and_ready(mock):
    with ws_connect(_gateway_url(mock)) as ws:
        hello = _recv(ws)
        assert hello == {"op": 10, "d": {"heartbeat_interval": mock_gateway.DEFAULT_HEARTBEAT_MS},
                         "s": None, "t": None}
        ws.send(json.dumps({"op": 1, "d": None}))
        assert _recv(ws) == {"op": 11}
        ws.send(json.dumps(IDENTIFY))
        ready = _recv(ws)["d"]
        assert ready["v"] == 10 and ready["user"]["id"] == discord.BOT_USER["id"]
        assert ready["session_id"] and ready["resume_gateway_url"].startswith("ws://")
        assert ready["application"]["flags"] == 1 << 18
    state = httpx.get(f"{mock}/_control/gateway").json()
    assert state["identifies"][0]["intents"] == IDENTIFY["d"]["intents"]
    assert [h["d"] for h in state["heartbeats"]] == [None]


@pytest.mark.parametrize(("payload", "code"), [
    ({"op": 2, "d": dict(IDENTIFY["d"], token="wrong")}, 4004),
    ({"op": 2, "d": dict(IDENTIFY["d"], intents=1 << 30)}, 4013),
    ({"op": 3, "d": {}}, 4003),
    ({"op": 99, "d": None}, 4001),
    ("not json", 4002),
    (json.dumps({"op": 1, "d": "x" * 5000}), 4002),
])
def test_gateway_close_codes(mock, payload, code):
    with ws_connect(_gateway_url(mock)) as ws:
        _recv(ws)
        assert _closed_with(ws, payload) == code


def test_gateway_version_and_privileged_intents(mock):
    base = _gateway_url(mock).split("?")[0]
    with ws_connect(base + "?v=9&encoding=json") as ws:
        assert _closed_with(ws) == 4012
    with ws_connect(_gateway_url(mock)) as ws:
        _ready(ws)
        assert _closed_with(ws, IDENTIFY) == 4005
    httpx.post(f"{mock}/_mock/application", json={"flags": 0})
    with ws_connect(_gateway_url(mock)) as ws:
        _recv(ws)
        assert _closed_with(ws, IDENTIFY) == 4014


def test_gateway_resume_replays_and_invalid_session(mock):
    with ws_connect(_gateway_url(mock)) as ws:
        ready = _ready(ws)
    message = httpx.post(f"{mock}/_control/gateway/message",
                         json={"channel_id": CHANNEL, "author_id": ALICE, "guild_id": "1",
                               "content": "while away"}).json()
    resume = {"op": 6, "d": {"token": TOKEN, "session_id": ready["session_id"], "seq": 1}}
    with ws_connect(ready["resume_gateway_url"] + "/?v=10&encoding=json") as ws:
        _recv(ws)
        ws.send(json.dumps(resume))
        missed = _recv(ws)
        assert (missed["t"], missed["s"], missed["d"]["id"]) == ("MESSAGE_CREATE", 2, message["id"])
        # Without MESSAGE_CONTENT... it was asked for, so the content is there.
        assert missed["d"]["content"] == "while away"
        assert _recv(ws)["t"] == "RESUMED"
    with ws_connect(_gateway_url(mock)) as ws:
        _recv(ws)
        assert _closed_with(ws, dict(resume, d=dict(resume["d"], seq=99))) == 4007
    with ws_connect(_gateway_url(mock)) as ws:
        _recv(ws)
        ws.send(json.dumps(dict(resume, d=dict(resume["d"], session_id="unknown"))))
        assert _recv(ws) == {"op": 9, "d": False, "s": None, "t": None}


def test_gateway_control_ops_and_message_content_intent(mock):
    with ws_connect(_gateway_url(mock)) as ws:
        _ready(ws, {"op": 2, "d": dict(IDENTIFY["d"], intents=(1 << 0) | (1 << 9))})
        httpx.post(f"{mock}/_control/gateway/message",
                   json={"channel_id": CHANNEL, "author_id": ALICE, "guild_id": "1",
                         "content": "no mention"})
        assert _recv(ws)["d"]["content"] == ""  # discord-gateway.md, Message Content Intent
        httpx.post(f"{mock}/_control/gateway/message",
                   json={"channel_id": CHANNEL, "author_id": ALICE, "guild_id": "1",
                         "content": f"<@{discord.BOT_USER['id']}> hi"})
        event = _recv(ws)["d"]
        assert event["content"].endswith("hi") and event["mentions"][0]["bot"] is True
        for name, op in (("heartbeat_request", 1), ("reconnect", 7)):
            httpx.post(f"{mock}/_control/gateway/{name}")
            assert _recv(ws)["op"] == op
        httpx.post(f"{mock}/_control/gateway/heartbeat_ack", json={"enabled": False})
        ws.send(json.dumps({"op": 1, "d": 3}))
        httpx.post(f"{mock}/_control/gateway/invalid_session", json={"resumable": True})
        assert _recv(ws) == {"op": 9, "d": True, "s": None, "t": None}  # no ACK came first
        httpx.post(f"{mock}/_control/gateway/close", json={"code": 4009})
        assert _closed_with(ws) == 4009


def _bot_message_with_button(base: str) -> dict:
    body = {"content": "Two tickets.", "allowed_mentions": {"parse": []},
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 1, "label": "Create", "custom_id": "proposal:p1:create"}]}]}
    r = httpx.post(f"{base}/api/v10/channels/{CHANNEL}/messages", headers=BOT_AUTH, json=body)
    assert r.status_code == 200, r.text
    _check(200, r.json())
    return r.json()


def _interaction(base: str, message: dict, custom_id: str = "proposal:p1:create") -> dict:
    return httpx.post(f"{base}/_control/gateway/interaction",
                      json={"custom_id": custom_id, "user_id": ALICE, "channel_id": CHANNEL,
                            "message_id": message["id"], "guild_id": "1"})


def test_interaction_dispatch_and_callbacks(mock, monkeypatch):
    message = _bot_message_with_button(mock)
    assert _interaction(mock, message, "proposal:p1:other").status_code == 400
    with ws_connect(_gateway_url(mock)) as ws:
        _ready(ws)
        pushed = _interaction(mock, message).json()
        event = _recv(ws)
    assert event["t"] == "INTERACTION_CREATE"
    interaction = event["d"]
    assert (interaction["type"], interaction["data"]) == (3, {"custom_id": "proposal:p1:create",
                                                             "component_type": 2})
    assert interaction["member"]["user"]["id"] == ALICE and interaction["token"] == pushed["token"]
    assert interaction["message"]["id"] == message["id"]
    url = f"{mock}/api/v10/interactions/{pushed['id']}"
    r = httpx.post(f"{url}/wrong-token/callback", json={"type": 6})
    assert r.status_code == 404 and r.json()["code"] == 10062
    r = httpx.post(f"{url}/{pushed['token']}/callback", json={"type": 3})
    assert r.status_code == 400 and r.json()["code"] == 50035
    r = httpx.post(f"{url}/{pushed['token']}/callback",
                   json={"type": 4, "data": {"content": "Only you", "flags": 64}})
    assert r.status_code == 204
    r = httpx.post(f"{url}/{pushed['token']}/callback", json={"type": 6})
    assert r.status_code == 400 and r.json()["code"] == 40060
    # An update (type 7) edits the message the button is on.
    second = _interaction(mock, message).json()
    r = httpx.post(f"{mock}/api/v10/interactions/{second['id']}/{second['token']}/callback",
                   json={"type": 7, "data": {"content": "Created ENG-1.", "components": []}})
    assert r.status_code == 204
    callbacks = httpx.get(f"{mock}/_mock/callbacks").json()["callbacks"]
    assert [(c["type"], c["ephemeral"]) for c in callbacks] == [(4, True), (7, False)]
    [edit] = httpx.get(f"{mock}/_mock/edits").json()["edits"]
    assert edit["via"] == "interaction" and edit["body"]["content"] == "Created ENG-1."
    # The 3 s window.
    monkeypatch.setattr(discord, "INTERACTION_WINDOW_S", 0.0)
    assert _interaction(mock, message).status_code == 400  # the update removed the button
    late = _interaction(mock, _bot_message_with_button(mock)).json()
    r = httpx.post(f"{mock}/api/v10/interactions/{late['id']}/{late['token']}/callback",
                   json={"type": 6})
    assert r.status_code == 404 and r.json()["code"] == 10062


def test_edit_message_and_message_reference(mock):
    message = _bot_message_with_button(mock)
    url = f"{mock}/api/v10/channels/{CHANNEL}/messages"
    r = httpx.patch(f"{url}/{message['id']}", headers=BOT_AUTH,
                    json={"content": "Created ENG-2.", "components": []})
    assert r.status_code == 200
    _check(200, r.json())
    assert r.json()["content"] == "Created ENG-2." and r.json()["components"] == []
    assert r.json()["edited_timestamp"]
    user_message = httpx.post(f"{mock}/_control/gateway/message",
                              json={"channel_id": CHANNEL, "author_id": ALICE,
                                    "content": "mine"}).json()
    r = httpx.patch(f"{url}/{user_message['id']}", headers=BOT_AUTH, json={"content": "x"})
    assert r.status_code == 403 and r.json()["code"] == 50005
    r = httpx.patch(f"{url}/1400000000000009999", headers=BOT_AUTH, json={"content": "x"})
    assert r.status_code == 404 and r.json()["code"] == 10008
    r = httpx.patch(f"{url}/{message['id']}", headers=BOT_AUTH, json={"content": "x" * 2001})
    assert r.status_code == 400 and r.json()["code"] == 50035
    reply = httpx.post(url, headers=BOT_AUTH, json={
        "content": "answer", "message_reference": {"message_id": user_message["id"]}})
    assert reply.status_code == 200
    _check(200, reply.json())
    assert reply.json()["message_reference"]["message_id"] == user_message["id"]
    r = httpx.post(url, headers=BOT_AUTH, json={
        "content": "answer", "message_reference": {"message_id": "1400000000000009999"}})
    assert r.status_code == 400 and "message_reference" in r.json()["errors"]
    r = httpx.post(url, headers=BOT_AUTH, json={
        "content": "answer", "message_reference": {"message_id": "1400000000000009999",
                                                   "fail_if_not_exists": False}})
    assert r.status_code == 200


def test_unserved_operations_are_501(mock):
    r = httpx.get(f"{mock}/api/v10/channels/{CHANNEL}/messages", headers=BOT_AUTH)
    assert r.status_code == 501
    r = httpx.get(f"{mock}/api/v10/users/@me", headers=BOT_AUTH)
    assert r.status_code == 501
    assert httpx.get(f"{mock}/api/v10/nope", headers=BOT_AUTH).status_code == 404
