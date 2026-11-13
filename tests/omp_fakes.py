"""A responsive Oh My Pi RPC double for runner tests.

Unlike the bare line fakes in conftest, this engine answers every command the way omp does
(id-correlated `response` frames) and releases scripted session events only once a prompt
has been sent — so a test scripts the agent's behavior, not the runner's command order.
Scripts may contain gates (`callable(engine) -> bool`): the stream pauses there until the
gate opens, which is how tests prove something reached the engine mid-turn.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable

# Real-time cap on one read: a broken test fails as engine silence instead of hanging.
MAX_BLOCK_S = 5.0

DEFAULT_DATA: dict[str, dict | None] = {
    "negotiate_protocol": {"protocolVersion": 2},
    "set_event_filter": {"events": None, "messageUpdates": "delta"},
    "set_ask_dialog": {"enabled": False},
    "set_auto_retry": None,
    "get_state": {"sessionFile": "/s.jsonl", "sessionId": "s-1"},
}


def assistant(text="done", stop_reason="stop", error=None, tokens=(10, 5)):
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": stop_reason,
        "usage": {
            "input": tokens[0],
            "output": tokens[1],
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": sum(tokens),
            "cost": {"total": 0.01},
        },
    }
    if error:
        msg["errorMessage"] = error
    return msg


class FakeEngine:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.arrived_at: list[float] = []  # time.monotonic() of each sent frame
        self.responses: dict[str, dict] = {}  # command type -> {"success": False, "error": ...}
        self.entries: list[dict] = []
        self.script: deque = deque()
        self.prompted = 0
        self.terminated: list[float] = []
        self.on_abort: Callable[[FakeEngine], None] | None = None
        self._out: deque[str] = deque()
        self._cv = threading.Condition()

    # -- scripting --------------------------------------------------------------
    def push(self, frame) -> None:
        """Append a session frame (or a gate) to the script."""
        with self._cv:
            self.script.append(frame)
            self._cv.notify_all()

    def run(self, text="done", stop_reason="stop", error=None, tokens=(10, 5)) -> None:
        """One complete run: usage-bearing message_end, then terminal agent_end."""
        message = assistant(text, stop_reason, error, tokens)
        self.push({"type": "message_end", "message": message})
        self.push({"type": "agent_end", "isTerminal": True, "messages": [message]})

    def emit(self, frame: dict) -> None:
        """Out-of-band frame, readable immediately (responses, abort fallout)."""
        with self._cv:
            self._out.append(json.dumps(frame))
            self._cv.notify_all()

    def sent_types(self) -> list[str]:
        with self._cv:
            return [m.get("type") for m in self.sent]

    def commands(self, kind: str) -> list[dict]:
        with self._cv:
            return [m for m in self.sent if m.get("type") == kind]

    # -- LineWriter ---------------------------------------------------------------
    def write_line(self, line: str) -> None:
        cmd = json.loads(line)
        kind = cmd.get("type")
        with self._cv:
            self.sent.append(cmd)
            self.arrived_at.append(time.monotonic())
            if kind == "prompt":
                self.prompted += 1
            self._cv.notify_all()
        if kind == "host_tool_result" or "id" not in cmd:
            return
        override = self.responses.get(kind)
        if override is not None:
            self.emit({"id": cmd["id"], "type": "response", "command": kind, **override})
        else:
            frame = {"id": cmd["id"], "type": "response", "command": kind, "success": True}
            data = self._data(kind, cmd)
            if data is not None:
                frame["data"] = data
            self.emit(frame)
        if kind == "abort" and self.on_abort is not None:
            self.on_abort(self)

    def _data(self, kind: str, cmd: dict) -> dict | None:
        if kind == "get_entries":
            since = cmd.get("since")
            if since is None:
                return {"entries": list(self.entries), "leafId": None}
            ids = [e["id"] for e in self.entries]
            return {"entries": self.entries[ids.index(since) + 1 :], "leafId": None}
        if kind == "set_host_tools":
            return {"toolNames": [t["name"] for t in cmd.get("tools", [])]}
        return DEFAULT_DATA.get(kind)

    # -- LineReader ---------------------------------------------------------------
    def read_line(self, timeout_s: float) -> str | None:
        deadline = time.monotonic() + min(timeout_s, MAX_BLOCK_S)
        with self._cv:
            while True:
                if self._out:
                    return self._out.popleft()
                if self.prompted and self.script:
                    head = self.script[0]
                    if not callable(head):
                        self.script.popleft()
                        return json.dumps(head)
                    if head(self):
                        self.script.popleft()
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # gates may watch Redis, which never notifies us: re-check them briskly
                gated = bool(self.prompted and self.script and callable(self.script[0]))
                self._cv.wait(min(remaining, 0.02) if gated else remaining)

    # -- EngineProcess ------------------------------------------------------------
    def terminate(self, grace_s: float) -> bool:
        self.terminated.append(grace_s)
        return True


def aborted_run(engine: FakeEngine) -> None:
    """omp's answer to `abort`: the rest of the turn is dropped and the run ends aborted."""
    with engine._cv:
        while engine.script and not (
            isinstance(engine.script[0], dict) and engine.script[0].get("type") == "agent_end"
        ):
            engine.script.popleft()
        if engine.script:
            engine.script.popleft()
    message = assistant("", stop_reason="aborted", tokens=(1, 0))
    engine.emit({"type": "message_end", "message": message})
    engine.emit({"type": "agent_end", "isTerminal": True, "messages": [message]})


class FakeSandbox:
    name = "ws-ENG-1"
    id = "sb-1"

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def exec_script(self, script, timeout_s=None):
        self.scripts.append(script)
        return 0, ""


def runner_env(redis_client, *, kv, tracker, cfg, clock, engine, sandbox=None, **overrides):
    """A RunnerEnv over the fake engine, with real Redis-backed sessions and team config."""
    from symphony.omp_rpc import OmpClient
    from symphony.runner import RunnerEnv
    from symphony.sessions import SessionStore
    from symphony.teamconfig import TeamConfig

    sandbox = sandbox or FakeSandbox()
    fields = dict(
        kv=kv,
        tracker=tracker,
        cfg=cfg,
        now_ms=clock.now_ms,
        call_id="fc-1",
        ensure_sandbox=lambda issue, bound: sandbox,
        open_client=lambda sb, token, issue, bound: OmpClient(engine, engine, engine),
        kill_agent=lambda sb: None,
        release_sandbox=lambda sb: None,
        wall_deadline_ms=clock.now_ms() + 3_600_000,
        sessions=SessionStore(redis_client, "test"),
        team=TeamConfig(redis_client, "test"),
        refresh_credentials=lambda sb: True,
        intent_block_ms=50,
    )
    fields.update(overrides)
    return RunnerEnv(**fields)
