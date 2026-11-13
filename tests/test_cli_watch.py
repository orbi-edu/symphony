"""`symphony watch`: plain streaming text and `--json` event lines outside Tern, and the native
view it draws in Tern (checked through `tern_sdk.plain`, the SDK's own text rendering)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import tern_sdk

from symphony.cli import acp_map
from symphony.cli.watch import WatchModel

pytest_plugins = ["test_cli_fake_api"]


class Caps:
    def __init__(self, *kinds: str) -> None:
        self.kinds = set(kinds)

    def draws(self, kind: str) -> bool:
        return kind in self.kinds


def test_native_view_splits_messages_and_shows_question_card():
    meta = {"sid": "i:1", "identifier": "ENG-1", "title": "Fix", "state": "waiting_input", "turn": 3,
            "tokens": {"total": 4200, "cost": 1.5},
            "input_request": {"id": "q", "question": "Which DB?", "options": ["pg", "sqlite"]}}
    model = WatchModel(meta)
    mapper = acp_map.LiveMapper()
    for mid, text in (("a", "First."), ("b", "Second.")):
        for upd in mapper.map("message_update", {"messageId": mid, "message": {"role": "assistant"},
                                                 "assistantMessageEvent": {"type": "text_delta", "delta": text}}):
            model.apply(upd)
    for upd in mapper.map("tool_execution_start", {"toolCallId": "t", "toolName": "bash",
                                                   "args": {"command": "make"}}):
        model.apply(upd)
    assert [i.get("text") for i in model.items if i["type"] == "text"] == ["First.", "Second."]

    # Tern 0.8.3 lacks `stats` (spike finding): the header falls back to a key/value list.
    old = tern_sdk.plain(model.view(Caps("card", "md", "tool", "kv", "text")))
    assert "ENG-1 · Fix · waiting for an answer" in old and "4,200" in old and "$1.50" in old
    assert "The agent asks" in old and "Which DB?" in old and "symphony answer ENG-1" in old
    assert "$ make" in old and "Model" in old
    new = tern_sdk.plain(model.view(Caps("stats", "card", "md", "tool", "kv", "text")))
    assert "Turn" in new and "Model" not in new


def _spawn(fake_api, tmp_path, *args):
    env = {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / ".config"),
           "SYMPHONY_URL": fake_api.url, "SYMPHONY_TOKEN": "sym_v1_viewer", "TERN_TSP": "0"}
    return subprocess.Popen([sys.executable, "-m", "symphony.cli", "watch", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, env=env)


def _read_until(proc, needle: str, timeout: float = 5) -> str:
    buf: list[str] = []
    done = threading.Event()

    def pump():
        while not done.is_set():
            ch = proc.stdout.read(1)
            if not ch:
                break
            buf.append(ch)
            if "".join(buf[-len(needle):]) == needle:
                done.set()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    t.join(timeout)
    text = "".join(buf)
    assert needle in text, text
    return text


def _wait_connected(api, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not api.sse_connects:
        assert time.monotonic() < deadline, "watch never opened the event stream"
        time.sleep(0.01)


def test_watch_plain_replays_history_then_streams(fake_api, tmp_path):
    sid = fake_api.seed_session()
    fake_api.store.append_transcript(sid, [
        {"type": "message", "id": "m1", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Earlier reply."},
            {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "make test"}}]}},
        {"type": "message", "id": "m2", "message": {"role": "toolResult", "toolCallId": "t1",
                                                    "toolName": "bash", "content": [], "isError": True}},
    ])
    proc = _spawn(fake_api, tmp_path, "ENG-1")
    try:
        head = _read_until(proc, "live (Ctrl-C")
        assert head.startswith("ENG-1 · Fix login redirect · running")
        assert "Earlier reply." in head and "▸ $ make test" in head and "✗ $ make test" in head
        _wait_connected(fake_api)
        fake_api.store.append_event(sid, "message_update", {
            "type": "message_update", "messageId": "x", "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "delta": "LIVE-TEXT"}})
        _read_until(proc, "LIVE-TEXT")
    finally:
        proc.kill()
        proc.wait()


def test_watch_json_prints_one_event_per_line(fake_api, tmp_path):
    sid = fake_api.seed_session()
    proc = _spawn(fake_api, tmp_path, sid, "--json")
    try:
        _wait_connected(fake_api)
        eid = fake_api.store.append_event(sid, "turn_end", {"type": "turn_end"})
        line = _read_until(proc, "\n")
        assert json.loads(line.splitlines()[0]) == {"id": eid, "kind": "turn_end", "data": {"type": "turn_end"}}
    finally:
        proc.kill()
        proc.wait()
