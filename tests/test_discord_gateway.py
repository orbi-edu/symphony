"""Discord Gateway transport against the Discord mock (scripts/mocks/discord)."""

from __future__ import annotations

import queue
import threading
import time

import httpx
import pytest
from scripts.mocks.discord import server as discord

from symphony.assistant import Action, Button, Inbound
from symphony.assistant import discord_gateway as gw
from symphony.assistant.discord_gateway import DiscordGatewayTransport

CHANNEL = discord.DEFAULT_CHANNEL["id"]
BOT = discord.BOT_USER["id"]
GUILD = "1600000000000000001"
DM = "1100000000000000099"
THREAD = "1100000000000000077"
ALICE = "1500000000000000001"


def _wait(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def mock(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "IDENTIFY_GAP_S", 0.05)
    monkeypatch.setattr(gw, "INVALID_SESSION_WAIT_S", (0.0, 0.05))
    srv = discord.serve(0, tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    httpx.post(f"{base}/_mock/users", json={"id": ALICE, "username": "alice"})
    httpx.post(f"{base}/_control/gateway/hello", json={"heartbeat_interval": 300})
    httpx.post(f"{base}/_mock/channels", json={"id": DM, "name": "dm-alice"})
    httpx.post(f"{base}/_mock/tokens", json={"token": discord.DEFAULT_TOKEN,
                                             "channels": [CHANNEL, DM]})
    yield base
    srv.shutdown()
    srv.server_close()


class Bot:
    def __init__(self, base: str):
        self.base = base
        self.messages: queue.Queue[Inbound] = queue.Queue()
        self.actions: queue.Queue[Action] = queue.Queue()
        self.transport = DiscordGatewayTransport(
            discord.DEFAULT_TOKEN, api_url=f"{base}/api/v10", backoff_base=0.05,
            backoff_cap=0.2)

    def gateway(self) -> dict:
        return httpx.get(f"{self.base}/_control/gateway").json()

    def control(self, name: str, body: dict | None = None) -> dict:
        r = httpx.post(f"{self.base}/_control/gateway/{name}", json=body or {})
        assert r.status_code == 200, r.text
        return r.json()

    def say(self, **fields) -> str:
        return self.control("message", {"author_id": ALICE, **fields})["id"]

    def no_message(self, wait: float = 0.3) -> None:
        with pytest.raises(queue.Empty):
            self.messages.get(timeout=wait)


@pytest.fixture
def bot(mock):
    b = Bot(mock)
    b.transport.start(b.messages.put, b.actions.put)
    _wait(lambda: b.transport.connected)
    yield b
    b.transport.stop()


def test_identify_with_the_right_intents(bot):
    [identify] = bot.gateway()["identifies"]
    assert identify["intents"] == (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15) == gw.INTENTS
    assert set(identify["properties"]) == {"os", "browser", "device"}
    assert bot.transport.bot_id == BOT


def test_heartbeats_carry_the_last_sequence(bot):
    beats = _wait(lambda: len(bot.gateway()["heartbeats"]) >= 3 and bot.gateway()["heartbeats"],
                  3)
    assert [b["d"] for b in beats[-2:]] == [1, 1]  # READY was s=1


def test_heartbeat_request_is_answered_at_once(mock):
    httpx.post(f"{mock}/_control/gateway/hello", json={"heartbeat_interval": 60_000})
    b = Bot(mock)
    b.transport.start(b.messages.put, b.actions.put)
    try:
        _wait(lambda: b.transport.connected)
        count = len(b.gateway()["heartbeats"])
        b.control("heartbeat_request")
        beats = _wait(lambda: len(b.gateway()["heartbeats"]) > count
                      and b.gateway()["heartbeats"], 1.0)
        assert beats[-1]["d"] == 1
    finally:
        b.transport.stop()


def test_missing_heartbeat_ack_means_zombie_then_resume(bot):
    session = bot.gateway()["identifies"][0]["session_id"]
    bot.control("heartbeat_ack", {"enabled": False})
    closes = _wait(lambda: bot.gateway()["closes"], 3)
    assert closes[0]["code"] == 4000  # not 1000/1001: the session stays resumable
    bot.control("heartbeat_ack", {"enabled": True})
    resumes = _wait(lambda: bot.gateway()["resumes"], 3)
    assert resumes[0]["session_id"] == session
    _wait(lambda: bot.transport.connected)
    assert len(bot.gateway()["identifies"]) == 1


def test_op7_resumes_and_replays_missed_events(bot):
    session = bot.gateway()["identifies"][0]["session_id"]
    bot.control("reconnect")
    resume = _wait(lambda: bot.gateway()["resumes"], 3)[0]
    assert resume == {"session_id": session, "seq": 1}
    _wait(lambda: bot.transport.connected)
    # A message sent while the connection is down arrives after the resume.
    bot.control("drop")
    bot.say(channel_id=DM, content="sent while you were away")
    assert bot.messages.get(timeout=5).text == "sent while you were away"
    assert len(bot.gateway()["resumes"]) == 2
    assert len(bot.gateway()["identifies"]) == 1


def test_op9_not_resumable_identifies_again(bot):
    first = bot.gateway()["identifies"][0]["session_id"]
    bot.control("invalid_session", {"resumable": False})
    identifies = _wait(lambda: len(bot.gateway()["identifies"]) == 2
                       and bot.gateway()["identifies"], 5)
    assert identifies[1]["session_id"] != first
    assert bot.gateway()["resumes"] == []
    _wait(lambda: bot.transport.connected)


def test_mention_dm_thread_and_reply_become_inbound(bot):
    first = bot.say(channel_id=CHANNEL, guild_id=GUILD, content=f"<@{BOT}> what is ENG-12?")
    msg = bot.messages.get(timeout=3)
    assert msg == Inbound(transport="discord", channel=CHANNEL, thread=first,
                          message_id=first, user_id=ALICE, text="what is ENG-12?")
    bot.say(channel_id=CHANNEL, guild_id=GUILD, content="lunch?")  # not for the bot
    bot.no_message()

    bot.say(channel_id=DM, content="hi")
    msg = bot.messages.get(timeout=3)
    assert (msg.is_dm, msg.channel, msg.thread, msg.text) == (True, DM, DM, "hi")

    # A reply to the bot's answer continues the conversation without a mention.
    ref = bot.transport.post(CHANNEL, first, "ENG-12 is the export.")
    [answer] = [m for m in httpx.get(f"{bot.base}/_mock/messages").json()["messages"]
                if m["id"] == ref.message_id]
    assert answer["body"]["message_reference"]["message_id"] == first
    assert answer["allowed_mentions"] == {"parse": []}
    bot.say(channel_id=CHANNEL, guild_id=GUILD, content="and ENG-13?", reference=ref.message_id)
    msg = bot.messages.get(timeout=3)
    assert (msg.thread, msg.text) == (first, "and ENG-13?")

    # A thread the bot started: every message in it is for the bot.
    bot.control("thread", {"id": THREAD, "parent_id": CHANNEL, "owner_id": BOT,
                           "guild_id": GUILD, "name": "ENG-12"})
    bot.say(channel_id=THREAD, guild_id=GUILD, channel_type=11, content="more detail")
    msg = bot.messages.get(timeout=3)
    assert (msg.channel, msg.thread, msg.parent) == (THREAD, THREAD, CHANNEL)


def test_both_mention_forms_are_stripped(bot):
    bot.say(channel_id=CHANNEL, guild_id=GUILD, content=f"<@!{BOT}>  status of ENG-12 <@{BOT}>")
    msg = bot.messages.get(timeout=3)
    assert (msg.text, msg.is_dm, msg.parent) == ("status of ENG-12", False, "")


def test_own_messages_are_ignored(bot):
    bot.control("message", {"author_id": BOT, "channel_id": DM, "content": "my answer"})
    bot.no_message()


def test_button_click_callback_and_ephemeral_refusal(bot):
    first = bot.say(channel_id=CHANNEL, guild_id=GUILD, content=f"<@{BOT}> plan it")
    bot.messages.get(timeout=3)
    buttons = (Button("proposal:p1:create", "Create", "primary"),
               Button("proposal:p1:dismiss", "Dismiss", "danger"))
    ref = bot.transport.post(CHANNEL, first, "Two tickets.", buttons)

    def click() -> Action:
        bot.control("interaction", {"custom_id": "proposal:p1:create", "user_id": ALICE,
                                    "channel_id": CHANNEL, "message_id": ref.message_id,
                                    "guild_id": GUILD})
        return bot.actions.get(timeout=3)

    action = click()
    assert (action.transport, action.channel, action.thread, action.message_id,
            action.user_id, action.action_id) == (
        "discord", CHANNEL, first, ref.message_id, ALICE, "proposal:p1:create")
    bot.transport.ack_action(action)
    refused = click()
    bot.transport.ack_action(refused, "Only people mapped in notify.users can create tickets.")
    silent, note = httpx.get(f"{bot.base}/_mock/callbacks").json()["callbacks"]
    assert (silent["type"], silent["ephemeral"]) == (6, False)
    assert (note["type"], note["ephemeral"]) == (4, True)
    assert note["data"]["flags"] == 64 and "message_id" not in note  # nothing posted publicly


def test_message_edit_replaces_text_and_removes_buttons(bot):
    ref = bot.transport.post(DM, DM, "Working on it…", (Button("proposal:p2:create", "Create"),))
    bot.transport.update(ref, "Created ENG-30.")
    [edit] = httpx.get(f"{bot.base}/_mock/edits").json()["edits"]
    assert edit["id"] == ref.message_id
    assert edit["body"]["content"] == "Created ENG-30." and edit["body"]["components"] == []
    bot.transport.ephemeral(DM, DM, ALICE, "a note")  # a normal reply on Discord
    assert httpx.get(f"{bot.base}/_mock/messages").json()["messages"][-1]["content"] == "a note"
    assert bot.transport.history(DM, DM, 30) == []


def test_rest_rate_limit_is_waited_out(bot):
    httpx.post(f"{bot.base}/_mock/ratelimit", json={"times": 1, "retry_after": 0.4})
    start = time.monotonic()
    ref = bot.transport.post(DM, DM, "after the limit")
    assert time.monotonic() - start >= 0.35
    assert ref.message_id


def test_disallowed_intent_stops_the_transport(mock):
    httpx.post(f"{mock}/_mock/application", json={"flags": 0})
    b = Bot(mock)
    b.transport.start(b.messages.put, b.actions.put)
    try:
        _wait(lambda: b.transport.fatal_code == 4014)
        time.sleep(0.3)
        assert len(b.gateway()["identifies"]) == 0 and b.gateway()["opened"] == 1
        assert not b.transport.connected
    finally:
        b.transport.stop()
