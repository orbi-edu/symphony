"""The Discord mock's Gateway: a websocket session per `discord-gateway.md`.

`DiscordGateway` keeps the sessions and the live connections in memory. `server.py` hands it
each upgraded socket (`serve_connection`) and pushes dispatches through `dispatch`.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from scripts.mocks.wsserver import WebSocketPeer

VERSION = 10
DEFAULT_HEARTBEAT_MS = 41_250
MAX_PAYLOAD = 4096  # "Must not exceed 4096 bytes" (close 4002)
RATE_LIMIT = (120, 60.0)  # 120 events per connection every 60 s (close 4008)

# discord-gateway.md, List of Intents
INTENTS = {
    "GUILDS": 1 << 0, "GUILD_MEMBERS": 1 << 1, "GUILD_MODERATION": 1 << 2,
    "GUILD_EXPRESSIONS": 1 << 3, "GUILD_INTEGRATIONS": 1 << 4, "GUILD_WEBHOOKS": 1 << 5,
    "GUILD_INVITES": 1 << 6, "GUILD_VOICE_STATES": 1 << 7, "GUILD_PRESENCES": 1 << 8,
    "GUILD_MESSAGES": 1 << 9, "GUILD_MESSAGE_REACTIONS": 1 << 10,
    "GUILD_MESSAGE_TYPING": 1 << 11, "DIRECT_MESSAGES": 1 << 12,
    "DIRECT_MESSAGE_REACTIONS": 1 << 13, "DIRECT_MESSAGE_TYPING": 1 << 14,
    "MESSAGE_CONTENT": 1 << 15, "GUILD_SCHEDULED_EVENTS": 1 << 16,
    "AUTO_MODERATION_CONFIGURATION": 1 << 20, "AUTO_MODERATION_EXECUTION": 1 << 21,
    "GUILD_MESSAGE_POLLS": 1 << 24, "DIRECT_MESSAGE_POLLS": 1 << 25,
}
ALL_INTENTS = sum(INTENTS.values())
# discord-application.md, Application Flags
FLAG_MESSAGE_CONTENT = (1 << 18) | (1 << 19)
FLAG_GUILD_MEMBERS = (1 << 14) | (1 << 15)
FLAG_PRESENCE = (1 << 12) | (1 << 13)


class _Conn:
    def __init__(self, peer: WebSocketPeer, host: str):
        self.peer = peer
        self.host = host
        self.session: dict | None = None
        self.sent_times: deque[float] = deque()


class DiscordGateway:
    def __init__(self, *, token_ok: Callable[[str], bool], app_flags: Callable[[], int],
                 bot_user: dict, application_id: str):
        self.token_ok = token_ok
        self.app_flags = app_flags
        self.bot_user = bot_user
        self.application_id = application_id
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.heartbeat_ms = DEFAULT_HEARTBEAT_MS
        self.ack_heartbeats = True
        self.conns: list[_Conn] = []
        self.sessions: dict[str, dict] = {}
        self.identifies: list[dict] = []
        self.resumes: list[dict] = []
        self.heartbeats: list[dict] = []
        self.closes: list[dict] = []
        self.errors: list[dict] = []
        self.opened = 0

    # connection ---------------------------------------------------------------------

    def check_query(self, query: dict[str, str]) -> int | None:
        """The close code for bad `v`/`encoding` query params, None when they are fine."""
        if query.get("v") != str(VERSION):
            return 4012
        if query.get("encoding", "json") != "json" or "compress" in query:
            return 4002
        return None

    def serve_connection(self, peer: WebSocketPeer, host: str, close_code: int | None) -> None:
        conn = _Conn(peer, host)
        with self.lock:
            self.opened += 1
            if close_code is None:
                self.conns.append(conn)
        if close_code is not None:
            peer.close(close_code, "invalid query")
            peer.run(lambda _text: None)
            return
        self._send(conn, {"op": 10, "d": {"heartbeat_interval": self.heartbeat_ms},
                          "s": None, "t": None})
        try:
            peer.run(lambda text: self._receive(conn, text))
        finally:
            with self.lock:
                if conn in self.conns:
                    self.conns.remove(conn)
                self.closes.append({"code": peer.close_code,
                                    "session_id": (conn.session or {}).get("session_id")})

    def _send(self, conn: _Conn, payload: dict) -> None:
        conn.peer.send(json.dumps(payload))

    def _close(self, conn: _Conn, code: int, error: str) -> None:
        self.errors.append({"code": code, "error": error})
        conn.peer.close(code, error)

    # client payloads ------------------------------------------------------------------

    def _receive(self, conn: _Conn, text: str) -> None:
        now = time.monotonic()
        with self.lock:
            conn.sent_times.append(now)
            while conn.sent_times and conn.sent_times[0] < now - RATE_LIMIT[1]:
                conn.sent_times.popleft()
            if len(conn.sent_times) > RATE_LIMIT[0]:
                return self._close(conn, 4008, "Rate limited.")
            if len(text.encode()) > MAX_PAYLOAD:
                return self._close(conn, 4002, "Payload too large.")
            try:
                payload = json.loads(text)
            except ValueError:
                return self._close(conn, 4002, "Error while decoding payload.")
            if not isinstance(payload, dict) or not isinstance(payload.get("op"), int):
                return self._close(conn, 4002, "Error while decoding payload.")
            op, d = payload["op"], payload.get("d")
            if op == 1:
                if d is not None and not isinstance(d, int):
                    return self._close(conn, 4002, "Invalid heartbeat sequence.")
                self.heartbeats.append({"d": d, "at": now,
                                        "session_id": (conn.session or {}).get("session_id")})
                if self.ack_heartbeats:
                    self._send(conn, {"op": 11})
                return None
            if op == 2:
                return self._identify(conn, d)
            if op == 6:
                return self._resume(conn, d)
            if op in (3, 4, 8, 31):  # presence, voice state, request members, soundboard
                if conn.session is None:
                    return self._close(conn, 4003, "Not authenticated.")
                return None
            return self._close(conn, 4001, "Unknown opcode.")

    def _identify(self, conn: _Conn, d: Any) -> None:
        if conn.session is not None:
            return self._close(conn, 4005, "Already authenticated.")
        if not isinstance(d, dict) or not isinstance(d.get("token"), str):
            return self._close(conn, 4001, "Invalid identify payload.")
        props = d.get("properties")
        if not isinstance(props, dict) or not all(
                isinstance(props.get(k), str) for k in ("os", "browser", "device")):
            return self._close(conn, 4001, "Invalid identify payload.")
        if not self.token_ok(d["token"]):
            return self._close(conn, 4004, "Authentication failed.")
        intents = d.get("intents")
        if not isinstance(intents, int) or isinstance(intents, bool) or intents < 0 \
                or intents & ~ALL_INTENTS:
            return self._close(conn, 4013, "Invalid intent(s).")
        flags = self.app_flags()
        for intent, flag in ((INTENTS["MESSAGE_CONTENT"], FLAG_MESSAGE_CONTENT),
                             (INTENTS["GUILD_MEMBERS"], FLAG_GUILD_MEMBERS),
                             (INTENTS["GUILD_PRESENCES"], FLAG_PRESENCE)):
            if intents & intent and not flags & flag:
                return self._close(conn, 4014, "Disallowed intent(s).")
        session_id = uuid.uuid4().hex
        session = {"session_id": session_id, "seq": 0, "events": [], "intents": intents,
                   "valid": True}
        self.sessions[session_id] = session
        conn.session = session
        self.identifies.append({"intents": intents, "properties": props,
                                "shard": d.get("shard"), "session_id": session_id})
        self._dispatch(session, "READY", {
            "v": VERSION,
            "user": self.bot_user,
            "guilds": [],
            "session_id": session_id,
            "resume_gateway_url": f"ws://{conn.host}",
            "application": {"id": self.application_id, "flags": flags},
        })
        return None

    def _resume(self, conn: _Conn, d: Any) -> None:
        if conn.session is not None:
            return self._close(conn, 4005, "Already authenticated.")
        if not isinstance(d, dict) or not isinstance(d.get("token"), str) \
                or not isinstance(d.get("session_id"), str) or not isinstance(d.get("seq"), int):
            return self._close(conn, 4001, "Invalid resume payload.")
        self.resumes.append({"session_id": d["session_id"], "seq": d["seq"]})
        if not self.token_ok(d["token"]):
            return self._close(conn, 4004, "Authentication failed.")
        session = self.sessions.get(d["session_id"])
        if session is None or not session["valid"]:
            self._send(conn, {"op": 9, "d": False, "s": None, "t": None})
            return None
        if d["seq"] > session["seq"]:
            return self._close(conn, 4007, "Invalid seq.")
        for conn_other in self.conns:
            if conn_other is not conn and conn_other.session is session:
                conn_other.session = None
                conn_other.peer.close(4000, "Session resumed elsewhere.")
        conn.session = session
        for event in session["events"]:
            if event["s"] > d["seq"]:
                self._send(conn, event)
        self._dispatch(session, "RESUMED", None)
        return None

    # server pushes --------------------------------------------------------------------

    def _dispatch(self, session: dict, name: str, data: Any) -> dict:
        # caller holds self.lock
        session["seq"] += 1
        event = {"op": 0, "t": name, "s": session["seq"], "d": data}
        session["events"].append(event)
        for conn in self.conns:
            if conn.session is session:
                self._send(conn, event)
        return event

    def live_session(self) -> dict | None:
        """The newest valid session (live or waiting for a resume)."""
        valid = [s for s in self.sessions.values() if s["valid"]]
        return valid[-1] if valid else None

    def dispatch(self, name: str, data: Any) -> dict | None:
        with self.lock:
            session = self.live_session()
            return None if session is None else self._dispatch(session, name, data)

    def intents(self) -> int:
        with self.lock:
            session = self.live_session()
            return 0 if session is None else session["intents"]

    def control(self, verb: str, name: str, body: dict) -> tuple[int, Any]:
        with self.lock:
            if verb == "GET" and name == "":
                return 200, {
                    "connections": len(self.conns),
                    "opened": self.opened,
                    "identifies": list(self.identifies),
                    "resumes": list(self.resumes),
                    "heartbeats": [{k: v for k, v in h.items() if k != "at"}
                                   for h in self.heartbeats],
                    "closes": list(self.closes),
                    "errors": list(self.errors),
                    "sessions": [{"session_id": s["session_id"], "seq": s["seq"],
                                  "valid": s["valid"]} for s in self.sessions.values()],
                }
            if verb != "POST":
                return 404, {"error": "unknown control route"}
            conns = list(self.conns)
            if name == "hello":
                self.heartbeat_ms = int(body.get("heartbeat_interval", DEFAULT_HEARTBEAT_MS))
            elif name == "heartbeat_ack":
                self.ack_heartbeats = bool(body.get("enabled", True))
            elif name == "heartbeat_request":
                for conn in conns:
                    self._send(conn, {"op": 1, "d": None, "s": None, "t": None})
            elif name == "reconnect":
                for conn in conns:
                    self._send(conn, {"op": 7, "d": None, "s": None, "t": None})
            elif name == "invalid_session":
                resumable = bool(body.get("resumable", False))
                for conn in conns:
                    if not resumable and conn.session is not None:
                        conn.session["valid"] = False
                        conn.session = None
                    self._send(conn, {"op": 9, "d": resumable, "s": None, "t": None})
            elif name == "close":
                for conn in conns:
                    conn.peer.close(int(body.get("code", 4000)), "closed by control")
            elif name == "drop":
                for conn in conns:
                    conn.peer.drop()
            else:
                return 404, {"error": "unknown control route"}
            return 200, {"ok": True, "connections": len(conns)}
