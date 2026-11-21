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
