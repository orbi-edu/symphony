"""`symphony acp` over a real stdio subprocess against the in-process fake API, plus the event
mapper the bridge and `symphony watch` share."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from symphony.cli import acp_map

pytest_plugins = ["test_cli_fake_api"]


class AcpClient:
    def __init__(self, env: dict) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "symphony.cli", "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, bufsize=1,
        )
        self._next = 0
        self.responses: dict[int, dict] = {}
        self.updates: queue.Queue = queue.Queue()
        self.log: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            msg = json.loads(line)
            with self._cv:
                self.log.append(msg)
                if "id" in msg and "method" not in msg:
                    self.responses[msg["id"]] = msg
                    self._cv.notify_all()
            if msg.get("method") == "session/update":
                self.updates.put(msg["params"])

    def send(self, method: str, params: dict | None = None) -> int:
        self._next += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return self._next

    def notify(self, method: str, params: dict) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 5) -> dict:
        rid = self.send(method, params)
        with self._cv:
            assert self._cv.wait_for(lambda: rid in self.responses, timeout), f"no reply to {method}"
            return self.responses[rid]

    def updates_until(self, pred, timeout: float = 5) -> list[dict]:
        """Drain updates until one satisfies `pred`; returns every update seen, in order."""
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"update not seen; got {seen}")
            try:
                upd = self.updates.get(timeout=left)["update"]
            except queue.Empty:
                continue
            seen.append(upd)
            if pred(upd):
                return seen

    def drain(self, wait: float = 0.3) -> list[dict]:
        out = []
        deadline = time.monotonic() + wait
        while (left := deadline - time.monotonic()) > 0:
            try:
                out.append(self.updates.get(timeout=left)["update"])
            except queue.Empty:
                break
        return out

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def text_of(upd: dict) -> str:
    content = upd.get("content")
    return content.get("text", "") if isinstance(content, dict) else ""


@pytest.fixture
def bridge(fake_api, tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / ".config"),
           "SYMPHONY_URL": fake_api.url, "SYMPHONY_TOKEN": "sym_o1_operator",
           "PYTHONPATH": os.pathsep.join(sys.path)}
    client = AcpClient(env)
    assert client.request("initialize", {"protocolVersion": 1})["result"]["agentCapabilities"]
    try:
        yield client
    finally:
        client.close()


TRANSCRIPT = [
    {"type": "session", "id": "h0", "cwd": "/work"},
    {"type": "message", "id": "m1", "message": {"role": "user",
                                                "content": [{"type": "text", "text": "Implement ENG-1"}]}},
    {"type": "model_change", "id": "x1", "model": "m"},
    {"type": "message", "id": "m2", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Need to look at auth."},
        {"type": "text", "text": "Reading the auth module."},
        {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "pytest -q"}},
    ]}},
    {"type": "message", "id": "m3", "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "bash",
                                                "content": [{"type": "text", "text": "3 passed"}],
                                                "isError": False}},
]


def test_initialize_advertises_load_list_resume(fake_api, tmp_path):
    env = {**os.environ, "SYMPHONY_URL": fake_api.url, "SYMPHONY_TOKEN": "sym_v1_viewer"}
    client = AcpClient(env)
    try:
        result = client.request("initialize", {"protocolVersion": 1})["result"]
        assert result["agentCapabilities"] == {"loadSession": True, "promptCapabilities": {},
                                               "mcpCapabilities": {"http": True, "sse": True},
                                               "sessionCapabilities": {"list": {}, "resume": {}}}
        assert result["agentInfo"]["name"] == "symphony"
    finally:
        client.close()


def test_list_load_replays_then_streams_live(bridge, fake_api):
    sid = fake_api.seed_session()
    fake_api.store.append_transcript(sid, TRANSCRIPT)
    listed = bridge.request("session/list", {"cwd": "/tmp"})["result"]["sessions"]
    assert listed == [{"sessionId": sid, "cwd": "/tmp", "title": "ENG-1 · Fix login redirect · running",
                       "updatedAt": listed[0]["updatedAt"]}]
    assert listed[0]["updatedAt"].endswith("Z")

    reply = bridge.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})
    assert reply["result"] == {}
    replayed = bridge.updates_until(lambda u: u["sessionUpdate"] == "available_commands_update")
    kinds = [(u["sessionUpdate"], text_of(u) or u.get("title")) for u in replayed]
    assert kinds == [
        ("user_message_chunk", "Implement ENG-1"),
        ("agent_thought_chunk", "Need to look at auth."),
        ("agent_message_chunk", "Reading the auth module."),
        ("tool_call", "$ pytest -q"),
        ("tool_call_update", None),
        ("available_commands_update", None),
    ]
    call = replayed[3]
    assert call["kind"] == "execute" and call["rawInput"] == {"command": "pytest -q"}
    assert replayed[4]["status"] == "completed"
    assert {"type": "content", "content": {"type": "text", "text": "3 passed"}} in replayed[4]["content"]
    assert [c["name"] for c in replayed[5]["availableCommands"]] == [
        "interrupt", "pause", "resume", "status", "takeover"]

    # Appended after the load: arrives live, without a prompt turn.
    started = time.monotonic()
    fake_api.store.append_event(sid, "message_update", {
        "type": "message_update", "messageId": "a9", "message": {"role": "assistant"},
        "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "LIVE-1"}})
    live = bridge.updates_until(lambda u: text_of(u) == "LIVE-1")
    assert time.monotonic() - started < 1.0
    assert live[-1]["sessionUpdate"] == "agent_message_chunk"
    fake_api.store.append_event(sid, "tool_execution_start", {
        "type": "tool_execution_start", "toolCallId": "tc9", "toolName": "read",
        "args": {"path": "/work/src/auth.py:10-40"}})
    tool = bridge.updates_until(lambda u: u["sessionUpdate"] == "tool_call")[-1]
    assert tool["kind"] == "read" and tool["locations"] == [{"path": "/work/src/auth.py"}]
    assert tool["status"] == "in_progress"
    # One SSE connection, started from the id read before the replay.
    assert len(fake_api.sse_connects) == 1


def test_prompt_posts_auto_intent_and_returns_fast(bridge, fake_api):
    sid = fake_api.seed_session()
    bridge.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})
    bridge.drain()
    started = time.monotonic()
    reply = bridge.request("session/prompt", {"sessionId": sid,
                                              "prompt": [{"type": "text", "text": "add a regression test"}]})
    assert reply["result"] == {"stopReason": "end_turn"}
    assert time.monotonic() - started < 1.0
    intents = fake_api.store.read_intents(sid)
    assert [(k, p) for _, k, _, p in intents] == [("steer", {"text": "add a regression test"})]
    ack = bridge.updates_until(lambda u: "Delivered" in text_of(u))
    assert "steer" in text_of(ack[-1])
    # The agent's echo of that message is not drawn a second time; another person's is.
    for text in ("add a regression test", "from someone else"):
        fake_api.store.append_event(sid, "message_start", {
            "type": "message_start", "messageId": f"u-{text}",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
    users = [u for u in bridge.updates_until(lambda u: text_of(u) == "from someone else")
             if u["sessionUpdate"] == "user_message_chunk"]
    assert [text_of(u) for u in users] == ["from someone else"]


def test_cancel_interrupts_and_slash_commands(bridge, fake_api):
    sid = fake_api.seed_session()
    bridge.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})
    bridge.drain()
    bridge.notify("session/cancel", {"sessionId": sid})
    bridge.updates_until(lambda u: "Interrupt sent" in text_of(u))
    assert [k for _, k, _, _ in fake_api.store.read_intents(sid)] == ["interrupt"]

    bridge.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/pause"}]})
    # The API pauses the whole namespace (admission); it has no per-ticket pause.
    assert fake_api.commands[-1] == {"action": "pause", "reason": "pause from Tern"}
    bridge.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/pause x"}]})
    assert "at least 8" in text_of(bridge.updates_until(lambda u: "Not sent" in text_of(u))[-1])
    assert len(fake_api.commands) == 1
    bridge.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/status"}]})
    status = bridge.updates_until(lambda u: "State: running" in text_of(u))
    assert "Turn: 2" in text_of(status[-1])
    bridge.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/takeover"}]})
    takeover = text_of(bridge.updates_until(lambda u: "takeover" in text_of(u))[-1])
    assert "symphony takeover ENG-1" in takeover and "holds the ticket" in takeover
    assert "symphony release ENG-1" in takeover
    bridge.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/interrupt"}]})
    assert [k for _, k, _, _ in fake_api.store.read_intents(sid)] == ["interrupt", "interrupt"]


def test_input_request_shows_question_and_prompt_answers_it(bridge, fake_api):
    sid = fake_api.seed_session()
    bridge.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})
    bridge.drain()
    fake_api.store.request_input(sid, "q7", "Which database?", ["postgres", "sqlite"])
    block = text_of(bridge.updates_until(lambda u: "The agent asks" in text_of(u))[-1])
    assert "Which database?" in block and "1. postgres" in block and "2. sqlite" in block
    reply = bridge.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "postgres"}]})
    assert reply["result"]["stopReason"] == "end_turn"
    assert [(k, p) for _, k, _, p in fake_api.store.read_intents(sid)] == [
        ("answer", {"request_id": "q7", "text": "postgres"})]
    assert "as the answer" in text_of(bridge.updates_until(lambda u: "Delivered" in text_of(u))[-1])


def test_reconnects_with_last_event_id_without_duplicates(bridge, fake_api):
    sid = fake_api.seed_session()
    fake_api.drop_after = 1
    bridge.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})
    bridge.drain()
    ids = []
    for i in range(3):
        ids.append(fake_api.store.append_event(sid, "message_update", {
            "type": "message_update", "messageId": f"m{i}", "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "delta": f"chunk-{i} "}}))
        bridge.updates_until(lambda u, i=i: text_of(u) == f"chunk-{i} ")
    assert [u for u in bridge.drain(0.5) if text_of(u).startswith("chunk-")] == []
    first = fake_api.sse_connects[0]
    assert fake_api.sse_connects[:4] == [first, ids[0], ids[1], ids[2]]


def test_new_session_explains_how_to_start_work(bridge):
    reply = bridge.request("session/new", {"cwd": "/tmp", "mcpServers": []})
    assert reply["result"]["sessionId"] == "symphony:new"
    msg = bridge.updates_until(lambda u: "Work starts in your tracker" in text_of(u))[-1]
    assert "ticket" in text_of(msg)


def test_not_logged_in_is_reported(fake_api, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    env.update(HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / ".config"))
    client = AcpClient(env)
    try:
        client.request("initialize", {"protocolVersion": 1})
        error = client.request("session/list", {})["error"]
        assert "symphony login" in error["message"]
    finally:
        client.close()


# -- mapper ------------------------------------------------------------------------

def test_mapper_done_does_not_repeat_streamed_text():
    m = acp_map.LiveMapper()
    delta = {"type": "message_update", "messageId": "a", "message": {"role": "assistant"},
             "assistantMessageEvent": {"type": "text_delta", "delta": "hi"}}
    done = {"type": "message_update", "messageId": "a", "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "done", "message": {"content": [{"type": "text", "text": "hi"}]}}}
    assert [text_of(u) for u in m.map("message_update", delta)] == ["hi"]
    assert m.map("message_update", done) == []
    assert m.map("message_end", {"messageId": "a", "message": {"role": "assistant", "content": []}}) == []
    # A message that never streamed text shows its final text once.
    end = {"messageId": "b", "message": {"role": "assistant", "content": [{"type": "text", "text": "final"}]}}
    assert [text_of(u) for u in m.map("message_end", end)] == ["final"]
    assert m.map("message_end", end) == []


def test_mapper_edit_diff_locations_and_todo_plan():
    m = acp_map.LiveMapper()
    start = m.map("tool_execution_start", {"toolCallId": "e1", "toolName": "edit", "args": {"path": "src/a.py"}})
    assert start[0]["kind"] == "edit" and start[0]["title"] == "edit: src/a.py"
    end = m.map("tool_execution_end", {"toolCallId": "e1", "toolName": "edit", "isError": False, "result": {
        "content": [{"type": "text", "text": "ok"}],
        "details": {"perFileResults": [{"path": "src/a.py", "oldText": "a", "newText": "b"}]}}})
    assert end[0]["status"] == "completed" and end[0]["locations"] == [{"path": "src/a.py"}]
    assert {"type": "diff", "path": "src/a.py", "oldText": "a", "newText": "b"} in end[0]["content"]
    todo = m.map("tool_execution_end", {"toolCallId": "t1", "toolName": "todo", "isError": False, "result": {
        "details": {"phases": [{"tasks": [{"content": "write test", "status": "in_progress"},
                                          {"content": "ship", "status": "abandoned"}]}]}}})
    assert todo[1] == {"sessionUpdate": "plan", "entries": [
        {"content": "write test", "priority": "medium", "status": "in_progress"},
        {"content": "ship", "priority": "medium", "status": "completed"}]}
    assert acp_map.tool_title("grep", {"pattern": "TODO"}) == "grep: TODO"
    assert acp_map.tool_title("read", {"path": "xd://github"}) == "xd://github"
