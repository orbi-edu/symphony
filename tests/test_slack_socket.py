"""Slack Socket Mode transport against the Slack mock (scripts/mocks/slack)."""

from __future__ import annotations

import queue
import threading
import time

import httpx
import pytest
from scripts.mocks.slack import server as slack

from symphony.assistant import Action, Button, Inbound
from symphony.assistant.slack_socket import SlackError, SlackSocketTransport

CHANNEL = slack.DEFAULT_CHANNEL["id"]
DM = "D0MOCKDM01"
ALICE = "U0ALICE01"
BOT = slack.BOT_USER_ID


def _wait(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def mock(tmp_path):
    srv = slack.serve(0, tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    httpx.post(f"{base}/_mock/channels", json={"id": DM, "name": "dm-alice"})
    yield base
    srv.shutdown()
    srv.server_close()


class Bot:
    def __init__(self, base: str, handler_delay: float = 0.0):
        self.base = base
        self.messages: queue.Queue[Inbound] = queue.Queue()
        self.actions: queue.Queue[Action] = queue.Queue()
        self.handler_delay = handler_delay
        self.acked_before_handler: list[bool] = []
        self.transport = SlackSocketTransport(
            slack.DEFAULT_APP_TOKEN, slack.DEFAULT_TOKEN, api_url=f"{base}/api",
            backoff_base=0.05, backoff_cap=0.2)

    def on_message(self, message: Inbound) -> None:
        if self.handler_delay:
            acked = {a["envelope_id"] for a in self.socket()["acks"]}
            envelopes = [e["envelope_id"] for e in self.socket()["envelopes"]]
            self.acked_before_handler.append(envelopes[-1] in acked)
            time.sleep(self.handler_delay)
        self.messages.put(message)

    def socket(self) -> dict:
        return httpx.get(f"{self.base}/_control/socket").json()

    def push(self, event: dict) -> dict:
        r = httpx.post(f"{self.base}/_control/socket/event", json={"event": event})
        assert r.status_code == 200, r.text
        return r.json()

    def no_message(self, wait: float = 0.3) -> None:
        with pytest.raises(queue.Empty):
            self.messages.get(timeout=wait)


@pytest.fixture
def bot(mock):
    b = Bot(mock)
    b.transport.start(b.on_message, b.actions.put)
    _wait(lambda: b.transport.connected)
    yield b
    b.transport.stop()


def test_mention_dm_and_thread_reply_become_inbound(bot):
    sent = bot.push({"type": "app_mention", "user": ALICE, "channel": CHANNEL,
                     "text": f"<@{BOT}> what blocks ENG-12?"})
    msg = bot.messages.get(timeout=3)
    assert msg == Inbound(transport="slack", channel=CHANNEL, thread=sent["ts"],
                          message_id=sent["ts"], user_id=ALICE, text="what blocks ENG-12?")
    # The same mention also arrives as a `message` event in a thread: handled once.
    bot.push({"type": "message", "user": ALICE, "channel": CHANNEL, "ts": sent["ts"],
              "thread_ts": sent["ts"], "text": f"<@{BOT}> what blocks ENG-12?"})
    bot.no_message()

    dm = bot.push({"type": "message", "user": ALICE, "channel": DM, "text": "hi there"})
    msg = bot.messages.get(timeout=3)
    assert (msg.is_dm, msg.channel, msg.thread, msg.text) == (True, DM, dm["ts"], "hi there")

    # A plain channel message is not for the bot; a reply in a thread the bot posted in is.
    bot.push({"type": "message", "user": ALICE, "channel": CHANNEL, "text": "lunch?"})
    bot.no_message()
    ref = bot.transport.post(CHANNEL, sent["ts"], "Working on it…")
    assert ref.channel == CHANNEL
    bot.push({"type": "message", "user": ALICE, "channel": CHANNEL, "thread_ts": sent["ts"],
              "text": "and ENG-13?"})
    msg = bot.messages.get(timeout=3)
    assert (msg.thread, msg.text) == (sent["ts"], "and ENG-13?")


def test_labelled_mention_is_stripped(bot):
    bot.push({"type": "app_mention", "user": ALICE, "channel": CHANNEL,
              "text": f"<@{BOT}|symphony> status of ENG-12"})
    msg = bot.messages.get(timeout=3)
    assert (msg.text, msg.is_dm, msg.parent) == ("status of ENG-12", False, "")


def test_own_and_bot_messages_are_ignored(bot):
    bot.push({"type": "message", "user": BOT, "bot_id": slack.BOT_ID, "channel": DM,
              "text": "my own answer"})
    bot.push({"type": "message", "user": "U0OTHERBOT", "bot_id": "B0OTHER01", "channel": DM,
              "text": "another bot"})
    bot.push({"type": "message", "user": ALICE, "channel": DM, "subtype": "message_changed",
              "text": "edited"})
    bot.no_message()
    acks = _wait(lambda: len(bot.socket()["acks"]) == 3 and bot.socket()["acks"])
    assert len(acks) == 3  # ignored envelopes are acked too


def test_ack_comes_within_3s_and_before_handling(mock):
    b = Bot(mock, handler_delay=1.0)
    b.transport.start(b.on_message, b.actions.put)
    try:
        _wait(lambda: b.transport.connected)
        b.push({"type": "app_mention", "user": ALICE, "channel": CHANNEL, "text": f"<@{BOT}> x"})
        b.messages.get(timeout=5)
        assert b.acked_before_handler == [True]
        [ack] = b.socket()["acks"]
        assert ack["delay_s"] < 0.5 < 3.0
        assert b.socket()["errors"] == []
    finally:
        b.transport.stop()


def test_buttons_action_ephemeral_update_and_history(bot):
    root = bot.push({"type": "app_mention", "user": ALICE, "channel": CHANNEL,
                     "text": f"<@{BOT}> plan the export"})
    bot.messages.get(timeout=3)
    buttons = (Button("proposal:p1:create", "Create", "primary"),
               Button("proposal:p1:start", "Create and start"),
               Button("proposal:p1:dismiss", "Dismiss", "danger"))
    ref = bot.transport.post(CHANNEL, root["ts"], "Two tickets.", buttons)
    [posted] = [m for m in httpx.get(f"{bot.base}/_mock/messages").json()["messages"]
                if m["ts"] == ref.message_id]
    actions = posted["blocks"][-1]
    assert actions["type"] == "actions"
    assert [e["action_id"] for e in actions["elements"]] == [b.id for b in buttons]
    assert actions["elements"][0]["style"] == "primary" and "style" not in actions["elements"][1]

    r = httpx.post(f"{bot.base}/_control/socket/interactive",
                   json={"user": ALICE, "channel": CHANNEL, "message_ts": ref.message_id,
                         "action_id": "proposal:p1:create"})
    assert r.status_code == 200
    action = bot.actions.get(timeout=3)
    assert (action.transport, action.channel, action.thread, action.message_id,
            action.user_id, action.action_id, action.value) == (
        "slack", CHANNEL, root["ts"], ref.message_id, ALICE, "proposal:p1:create",
        "proposal:p1:create")

    bot.transport.ack_action(action)  # silent: nothing to send
    bot.transport.ack_action(action, "Only people mapped in notify.users can create tickets.")
    [note] = httpx.get(f"{bot.base}/_mock/ephemerals").json()["ephemerals"]
    assert (note["user"], note["channel"], note["thread_ts"]) == (ALICE, CHANNEL, root["ts"])

    bot.transport.update(ref, "Created ENG-20 and ENG-21.")
    [update] = httpx.get(f"{bot.base}/_mock/updates").json()["updates"]
    assert update["text"] == "Created ENG-20 and ENG-21."
    assert [b["type"] for b in update["blocks"]] == ["section"]  # buttons removed

    history = bot.transport.history(CHANNEL, root["ts"], 30)
    assert [(h.user_id, h.is_bot) for h in history] == [(ALICE, False), (BOT, True)]
    assert history[0].text == f"<@{BOT}> plan the export"


def test_a_click_on_a_channel_message_gets_its_note_in_the_channel(bot):
    # A notification sits at the top of the channel. A note in its (empty) thread would be
    # hidden until someone opened that thread: the clicker would see nothing.
    ref = bot.transport.post(CHANNEL, "", "#7 ready to try.", (Button("preview:o/r:7:abc", "Open"),))
    httpx.post(f"{bot.base}/_control/socket/interactive",
               json={"user": ALICE, "channel": CHANNEL, "message_ts": ref.message_id,
                     "action_id": "preview:o/r:7:abc"})
    action = bot.actions.get(timeout=3)
    bot.transport.ack_action(action, "Starting a simulator.")
    [note] = httpx.get(f"{bot.base}/_mock/ephemerals").json()["ephemerals"]
    assert (note["user"], note["channel"], note["thread_ts"]) == (ALICE, CHANNEL, None)


def test_long_text_is_split_into_sections(bot):
    text = ("word " * 1300).strip()  # 6,499 characters
    ref = bot.transport.post(CHANNEL, "", text)
    [posted] = [m for m in httpx.get(f"{bot.base}/_mock/messages").json()["messages"]
                if m["ts"] == ref.message_id]
    sections = [b["text"]["text"] for b in posted["blocks"]]
    assert len(sections) == 3 and all(len(s) <= 3000 for s in sections)


def test_disconnect_and_drop_reconnect(bot):
    assert bot.socket()["opened"] == 1
    r = httpx.post(f"{bot.base}/_control/socket/disconnect",
                   json={"reason": "refresh_requested", "close_after": 5})
    assert r.json()["connections"] == 1
    # The transport opens a new connection at once, without waiting for the socket to close.
    _wait(lambda: bot.socket()["opened"] == 2 and bot.socket()["connections"] == 1, 3)
    _wait(lambda: bot.transport.connected)
    bot.push({"type": "message", "user": ALICE, "channel": DM, "text": "still there?"})
    assert bot.messages.get(timeout=3).text == "still there?"

    httpx.post(f"{bot.base}/_control/socket/drop")
    _wait(lambda: bot.socket()["opened"] == 3 and bot.socket()["connections"] == 1)
    _wait(lambda: bot.transport.connected)
    bot.push({"type": "message", "user": ALICE, "channel": DM, "text": "after a drop"})
    assert bot.messages.get(timeout=3).text == "after a drop"


def test_reconnect_backs_off_while_slack_is_down(mock):
    b = Bot(mock)
    httpx.post(f"{mock}/_mock/ratelimit", json={"enabled": True, "retry_after": 1})
    b.transport.start(b.on_message, b.actions.put)
    try:
        time.sleep(0.3)
        assert not b.transport.connected  # waiting out Retry-After
        httpx.post(f"{mock}/_mock/ratelimit", json={"enabled": False})
        _wait(lambda: b.transport.connected, 5)
    finally:
        b.transport.stop()


def test_web_api_honors_retry_after(bot):
    httpx.post(f"{bot.base}/_mock/ratelimit", json={"enabled": True, "retry_after": 1})
    threading.Timer(0.5, lambda: httpx.post(f"{bot.base}/_mock/ratelimit",
                                            json={"enabled": False})).start()
    start = time.monotonic()
    ref = bot.transport.post(DM, "", "after the limit")
    assert time.monotonic() - start >= 0.9
    assert ref.message_id


def test_from_env_needs_both_tokens():
    assert SlackSocketTransport.from_env({"SLACK_BOT_TOKEN": "xoxb-1"}) is None
    transport = SlackSocketTransport.from_env({"SLACK_BOT_TOKEN": "xoxb-1",
                                               "SLACK_APP_TOKEN": "xapp-1"})
    assert transport is not None and transport.name == "slack"
    assert transport.message_limit == 4_000


def test_an_answer_at_the_message_limit_can_be_edited_in_place(bot):
    """The engine edits the first part of every answer with chat.update, which rejects a
    `text` over 4,000 characters; a part of `message_limit` characters must still pass."""
    ref = bot.transport.post(CHANNEL, "", "thinking…")
    bot.transport.update(ref, "x" * bot.transport.message_limit)
    with pytest.raises(SlackError, match="msg_too_long"):
        bot.transport.update(ref, "x" * (bot.transport.message_limit + 1))
