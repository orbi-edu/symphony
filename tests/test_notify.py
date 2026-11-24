"""symphony.notify against a fake Slack HTTP server: transport choice, mentions, dedup,
timeouts and failures that never reach the caller. Discord runs against its mock in
tests/mocks/test_mock_discord.py."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from symphony import notify


class FakeSlack:
    """Records every request; answers from a queue of (status, body, delay_s)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[tuple[int, dict | str, float]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server's contract
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status, payload, delay = (
                    fake.responses.pop(0) if fake.responses else (200, {"ok": True}, 0.0)
                )
                if delay:
                    time.sleep(delay)
                raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header(
                        "Content-Type",
                        "text/plain" if isinstance(payload, str) else "application/json; charset=utf-8",
                    )
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up first (timeout test)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def slack(monkeypatch):
    for name in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL",
                 "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    notify.reset()
    fake = FakeSlack()
    monkeypatch.setenv("SLACK_API_URL", fake.url + "/api/")  # trailing `/` is stripped
    yield fake
    fake.close()
    notify.reset()


@pytest.fixture
def bot(slack, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C42")
    return slack


def test_without_config_nothing_is_sent(slack, monkeypatch, tmp_path):
    monkeypatch.setenv("SYMPHONY_WORKFLOW_MOUNT", str(tmp_path))
    assert notify.post("main_red", "main is red") is False
    assert slack.requests == []


def test_bot_posts_chat_post_message_with_mention_and_link(bot):
    notify.configure({"slack": {"Alice-Dev": "U123"}, "discord": {"alice-dev": "42"}})
    sent = notify.post(
        "back_to_author",
        "PR #12 has 2 blocking findings <see report> & fix",
        link="https://github.com/o/r/pull/12",
        github_logins=["alice-dev"],
    )
    assert sent is True
    [request] = bot.requests
    assert request["path"] == "/api/chat.postMessage"
    assert request["headers"]["Authorization"] == "Bearer xoxb-test"
    body = request["body"]
    assert body["channel"] == "C42"
    assert body["text"] == (
        ":leftwards_arrow_with_hook: *PR returned to its author* <@U123>\n"
        "PR #12 has 2 blocking findings &lt;see report&gt; &amp; fix\n"
        "<https://github.com/o/r/pull/12|Open>"
    )


def test_every_contract_event_has_its_own_emoji_and_title():
    contract = {
        "input_requested", "back_to_author", "needs_maintainer", "batch_split",
        "main_red", "main_green", "run_error", "needs_approval", "preview_ready",
        "retries_exhausted", "not_queued", "triage_opened",
    }
    assert set(notify.EVENTS) == contract
    assert len({emoji for emoji, _ in notify.EVENTS.values()}) == len(contract)
    assert set(notify.DISCORD_EMOJI) == contract
    assert len(set(notify.DISCORD_EMOJI.values())) == len(contract)


def test_discord_message_keeps_title_and_link_when_cut():
    link = "https://github.com/o/r/pull/1"
    message = notify.format_discord_message("main_red", "y" * 3000, link=link, mention="<@42>")
    assert len(message) == notify.DISCORD_MAX_CHARS
    assert message.startswith("\N{LARGE RED CIRCLE} **main is broken** <@42>\ny")
    assert message.endswith(f"y…\n[Open](<{link}>)")
    assert notify.format_discord_message("main_red", "short") == "\N{LARGE RED CIRCLE} **main is broken**\nshort"


def test_unmapped_login_posts_without_mention(bot):
    notify.configure({"slack": {"alice": "U1"}})
    assert notify.post("needs_maintainer", "arbiter could not decide", github_logins=["bob"])
    assert "<@" not in bot.requests[0]["body"]["text"]


def test_webhook_used_when_no_bot_token(slack, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", slack.url + "/hooks/T/B/x")
    assert notify.post("main_green", "main is green again") is True
    [request] = slack.requests
    assert request["path"] == "/hooks/T/B/x"
    assert request["body"] == {"text": ":large_green_circle: *main is green again*\nmain is green again"}


def test_bot_wins_over_webhook(bot, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", bot.url + "/hooks/unused")
    assert notify.post("run_error", "boom")
    assert [r["path"] for r in bot.requests] == ["/api/chat.postMessage"]


def test_identical_message_within_window_is_sent_once(bot):
    assert notify.post("main_red", "CI failed on abc123", link="https://ci/1") is True
    assert notify.post("main_red", "CI failed on abc123", link="https://ci/1") is False
    assert notify.post("main_red", "CI failed on def456", link="https://ci/2") is True
    assert notify.post("run_error", "CI failed on abc123", link="https://ci/1") is True
    assert len(bot.requests) == 3


def test_dedup_expires_after_window(bot, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(notify.time, "monotonic", lambda: clock[0])
    assert notify.post("batch_split", "batch of 3 split") is True
    clock[0] += notify.DEDUP_WINDOW_S - 1
    assert notify.post("batch_split", "batch of 3 split") is False
    clock[0] += 2
    assert notify.post("batch_split", "batch of 3 split") is True
    assert len(bot.requests) == 2


def test_rejected_send_is_not_deduplicated(bot):
    bot.responses.append((200, {"ok": False, "error": "channel_not_found"}, 0.0))
    assert notify.post("run_error", "review of PR #7 failed") is False
    assert notify.post("run_error", "review of PR #7 failed") is True
    assert len(bot.requests) == 2


def test_http_error_and_garbage_never_raise(bot, monkeypatch):
    bot.responses.append((500, "internal error", 0.0))
    assert notify.post("run_error", "first") is False
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://127.0.0.1:1/unreachable")
    assert notify.post("run_error", "second") is False


def test_timeout_returns_quickly_without_raising(bot, monkeypatch):
    assert notify.TIMEOUT_S == 2.0
    monkeypatch.setattr(notify, "TIMEOUT_S", 0.2)
    # Slack answers only after 5 s: returning well before that proves the client gave up
    # (the margin absorbs client setup on a loaded machine).
    bot.responses.append((200, {"ok": True}, 5.0))
    started = time.monotonic()
    assert notify.post("input_requested", "Which database?") is False
    assert time.monotonic() - started < 3.0


def test_mentions_read_from_mounted_workflow_and_reloaded(bot, monkeypatch, tmp_path):
    monkeypatch.setenv("SYMPHONY_WORKFLOW_MOUNT", str(tmp_path))
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "---\ntracker:\n  kind: memory\nnotify:\n  users:\n    slack:\n      Carol: U777\n"
        "---\nprompt body\n---\n"
    )
    assert notify.post("input_requested", "Which database?", github_logins=["carol"])
    assert bot.requests[-1]["body"]["text"].startswith(
        ":raising_hand: *Agent is waiting for an answer* <@U777>"
    )
    workflow.write_text("---\nnotify:\n  users:\n    slack:\n      carol: U888\n---\n")
    stat = workflow.stat()
    import os

    os.utime(workflow, (stat.st_atime, stat.st_mtime + 5))
    assert notify.post("input_requested", "Second question", github_logins=["carol"])
    assert "<@U888>" in bot.requests[-1]["body"]["text"]


def test_configured_map_wins_over_workflow(bot, monkeypatch, tmp_path):
    monkeypatch.setenv("SYMPHONY_WORKFLOW_MOUNT", str(tmp_path))
    (tmp_path / "WORKFLOW.md").write_text("---\nnotify:\n  users:\n    slack:\n      dave: U1\n---\n")
    notify.configure({"slack": {"dave": "U2"}})
    assert notify.post("needs_maintainer", "help", github_logins=["dave"])
    assert "<@U2>" in bot.requests[-1]["body"]["text"]


def test_concurrent_identical_posts_send_once(bot):
    bot.responses.append((200, {"ok": True}, 0.3))
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(notify.post("main_red", "same"))) for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, False, True]
    assert len(bot.requests) == 1


def test_multiple_logins_mentioned_in_slack(bot):
    """github_logins with two mapped users produces both mentions in the header."""
    notify.configure({"slack": {"alice": "U1", "bob": "U2"}})
    assert notify.post("needs_approval", "waiting", github_logins=["alice", "bob"])
    text = bot.requests[0]["body"]["text"]
    assert "<@U1>" in text and "<@U2>" in text


def test_multiple_logins_only_mapped_appear(bot):
    """An unmapped login in the list does not produce a mention."""
    notify.configure({"slack": {"alice": "U1"}})
    assert notify.post("needs_approval", "waiting", github_logins=["alice", "charlie"])
    text = bot.requests[0]["body"]["text"]
    assert "<@U1>" in text
    assert "charlie" not in text or "<@" not in text.split("charlie")[0][-10:]


def test_a_question_has_a_button_per_option_mentions_by_email_and_can_be_edited(bot):
    notify.configure({"slack": {"victor@example.com": "U7"}})
    where = notify.post_question("ENG-1 needs an answer: Which?", qid="q1",
                                 options=["Keep", "x" * 100], mention_keys=["victor@example.com"])
    body = bot.requests[0]["body"]
    assert where is None  # Slack's answer named no message: nothing to come back to
    assert "<@U7>" in body["text"]
    [buttons] = [b["elements"] for b in body["blocks"] if b["type"] == "actions"]
    assert [b["action_id"] for b in buttons] == ["ask:q1:0", "ask:q1:1"]
    assert buttons[0]["text"]["text"] == "1. Keep" and len(buttons[1]["text"]["text"]) == 75
    assert notify.parse_question_action("ask:q1:1") == ("q1", 1)
    bot.responses.append((200, {"ok": True, "channel": "C42", "ts": "5.0"}, 0.0))
    again = notify.post_question("reminder", qid="q1", options=["Keep"],
                                 thread={"channel": "C42", "ts": "5.0"})
    assert again == {"channel": "C42", "ts": "5.0"}
    assert bot.requests[1]["body"]["thread_ts"] == "5.0"
    assert bot.requests[1]["body"]["reply_broadcast"] is True
    assert notify.edit_question(again, ":white_check_mark: *Answered*", "<@here> took 1")
    edit = bot.requests[2]
    assert edit["path"] == "/api/chat.update" and "&lt;@here&gt; took 1" in edit["body"]["text"]
    assert "blocks" in edit["body"] and all(b["type"] != "actions" for b in edit["body"]["blocks"])


# Direct messages -------------------------------------------------------------------------


def _posted(fake) -> list[tuple[str, str]]:
    return [(r["body"]["channel"], r["body"]["text"]) for r in fake.requests
            if r["path"] == "/api/chat.postMessage"]


def test_the_assignee_gets_one_dm_besides_the_channel_message(bot):
    # Two keys (e-mail and name, as Linear gives them) name one person: one DM.
    notify.configure({"slack": {"alice@example.com": "U1", "Alice": "U1", "bob": "U2"}})
    assert notify.post("back_to_author", "PR #12 went back", link="https://example.com/pr/12",
                       github_logins=["bob"], direct_to=["alice@example.com", "Alice"])
    [(channel, text), (dm, dm_text)] = _posted(bot)
    assert channel == "C42" and "<@U2>" in text
    assert dm == "U1"  # chat.postMessage with a member ID posts in the bot's DM
    assert dm_text == (":leftwards_arrow_with_hook: *PR returned to its author*\n"
                       "PR #12 went back\n<https://example.com/pr/12|Open>")


def test_an_unmapped_assignee_gets_no_dm(bot):
    notify.configure({"slack": {"bob": "U2"}})
    assert notify.post("retries_exhausted", "ENG-1 stopped", direct_to=["carol@example.com"])
    assert [c for c, _ in _posted(bot)] == ["C42"]


def test_direct_messages_off_keeps_the_channel_only(bot, monkeypatch, tmp_path):
    notify.configure({"slack": {"alice": "U1"}}, direct_messages=False)
    assert notify.post("not_queued", "ENG-1 is not queued", direct_to=["alice"])
    assert [c for c, _ in _posted(bot)] == ["C42"]
    # The same switch, read from the mounted WORKFLOW.md.
    notify.reset()
    monkeypatch.setenv("SYMPHONY_WORKFLOW_MOUNT", str(tmp_path))
    (tmp_path / "WORKFLOW.md").write_text(
        "---\nnotify:\n  users: {slack: {alice: U1}}\n  direct_messages: false\n---\n")
    assert notify.post("not_queued", "ENG-2 is not queued", direct_to=["alice"])
    assert [c for c, _ in _posted(bot)] == ["C42", "C42"]


def test_direct_events_limit_which_events_are_sent_as_dms(bot, monkeypatch, tmp_path):
    monkeypatch.setenv("SYMPHONY_WORKFLOW_MOUNT", str(tmp_path))
    (tmp_path / "WORKFLOW.md").write_text(
        "---\nnotify:\n  users: {slack: {alice: U1}}\n  direct_events: [back_to_author]\n---\n")
    assert notify.post("run_error", "boom", direct_to=["alice"])
    assert notify.post("back_to_author", "PR #3", direct_to=["alice"])
    assert [c for c, _ in _posted(bot)] == ["C42", "C42", "U1"]


def test_dm_dedup_is_per_destination_and_a_failed_dm_never_raises(bot):
    notify.configure({"slack": {"alice": "U1", "bob": "U2"}})
    assert notify.post("main_red", "CI failed", direct_to=["alice"])
    assert notify.post("main_red", "CI failed", direct_to=["alice", "bob"])  # only bob is new
    assert [c for c, _ in _posted(bot)] == ["C42", "U1", "U2"]
    bot.responses.append((200, {"ok": True}, 0.0))
    bot.responses.append((200, {"ok": False, "error": "channel_not_found"}, 0.0))
    assert notify.post("main_green", "fixed", direct_to=["alice"]) is True  # the channel took it
    bot.responses.append((200, {"ok": True}, 0.0))
    assert notify.post("main_green", "fixed", direct_to=["alice"]) is True  # the failed DM is retried
    assert [c for c, _ in _posted(bot)][-3:] == ["C42", "U1", "U1"]


def test_a_webhook_alone_sends_no_dm(slack, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", slack.url + "/hook")
    notify.configure({"slack": {"alice": "U1"}})
    assert notify.post("run_error", "boom", direct_to=["alice"])
    assert [r["path"] for r in slack.requests] == ["/hook"]


def test_a_question_dm_carries_the_buttons_and_is_edited_with_the_channel_message(bot):
    notify.configure({"slack": {"victor@example.com": "U7", "alice": "U1"}})
    bot.responses += [(200, {"ok": True, "channel": "C42", "ts": "1.0"}, 0.0),
                      (200, {"ok": True, "channel": "D7", "ts": "2.0"}, 0.0),
                      (200, {"ok": True, "channel": "D1", "ts": "3.0"}, 0.0)]
    where = notify.post_question("ENG-1 needs an answer: Which?", qid="q1", options=["A", "B"],
                                 mention_keys=["victor@example.com"],
                                 direct_keys=["victor@example.com", "alice", "Victor"])
    assert where == {"channel": "C42", "ts": "1.0",
                     "dms": [{"channel": "D7", "ts": "2.0"}, {"channel": "D1", "ts": "3.0"}]}
    channel, *dms = [r["body"] for r in bot.requests]
    assert [d["channel"] for d in dms] == ["U7", "U1"]
    for body in (channel, *dms):
        [buttons] = [b["elements"] for b in body["blocks"] if b["type"] == "actions"]
        assert [b["action_id"] for b in buttons] == ["ask:q1:0", "ask:q1:1"]
        assert "thread_ts" not in body
    assert "<@U7>" in channel["text"] and "<@" not in dms[0]["text"]
    assert notify.edit_question(where, ":white_check_mark: *Answered*", "A")
    edits = [r["body"] for r in bot.requests if r["path"] == "/api/chat.update"]
    assert [(e["channel"], e["ts"]) for e in edits] == [("C42", "1.0"), ("D7", "2.0"), ("D1", "3.0")]


def test_a_button_message_goes_to_the_assignee_with_its_button(bot):
    notify.configure({"slack": {"alice": "U1"}})
    assert notify.post_with_button("preview_ready", "#7 ready", button_label="Open",
                                   button_action_id="preview:acme/app:7:abc", direct_to=["alice"])
    channel, dm = [r["body"] for r in bot.requests]
    assert (channel["channel"], dm["channel"]) == ("C42", "U1")
    assert dm["blocks"][1]["elements"][0]["action_id"] == "preview:acme/app:7:abc"
