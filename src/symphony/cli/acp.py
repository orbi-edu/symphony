"""`symphony acp`: the ACP agent (JSON-RPC over stdio) Tern opens as a native chat.

Validated shape: spike S1 (`spikes/acp_agent.py`). Tern lists Symphony sessions with
`session/list`, opens one with `session/load` (transcript replay, then the live stream keeps
arriving as `session/update` outside any prompt turn) and sends people's messages with
`session/prompt`, which only posts an intent and returns at once. Esc is `session/cancel`, an
interrupt. Each loaded session holds exactly one SSE connection, resumed by `Last-Event-ID`.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import IO

from symphony import __version__
from symphony.cli import acp_map, creds, render
from symphony.cli.client import Api, ApiError, EventTail, reason_problem

PROTOCOL_VERSION = 1
NEW_SESSION_ID = "symphony:new"
VERSION = __version__

NEW_SESSION_HELP = (
    "**Symphony does not start sessions here.**\n\n"
    "Work starts in your tracker (Linear or GitHub Projects). Create a ticket, or move one, "
    "to the state your team uses for Symphony. An agent starts within seconds.\n\n"
    "To follow or talk to an agent that is already running, open the session list "
    "(`tern acp symphony --sessions`) or run `symphony chat ENG-123`."
)

TAKEOVER_HELP = (
    "To take over this session on your machine, run this in a terminal, inside the "
    "repository:\n\n"
    "```\nsymphony takeover {ticket}\n```\n\n"
    "This **holds the ticket**: Symphony stops dispatching, retrying and continuing work on it. "
    "Then it interrupts the agent's turn, downloads the transcript and the work branch, and "
    "opens `omp --resume` with the whole conversation. When you finish, return the ticket with "
    "`symphony release {ticket}`."
)


class _Loaded:
    def __init__(self, sid: str, meta: dict) -> None:
        self.sid = sid
        self.mapper = acp_map.LiveMapper()
        self.mapper.meta = dict(meta)
        self.tail: EventTail | None = None


class Bridge:
    def __init__(self, stdin: IO[str], stdout: IO[str]) -> None:
        self._in = stdin
        self._out = stdout
        self._out_lock = threading.Lock()
        self._sessions: dict[str, _Loaded] = {}
        self._sessions_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="acp")
        self._api: Api | None = None
        self._cred: creds.Credential | None = None

    # -- wire ----------------------------------------------------------------
    def _send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._out_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _reply(self, req_id, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def _update(self, sid: str, upd: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": sid, "update": upd}})

    def _say(self, sid: str, text: str) -> None:
        self._update(sid, acp_map.note(text))

    def api(self) -> Api:
        if self._api is None:
            self._cred = creds.current()
            self._api = Api(self._cred)
        return self._api

    # -- loop ----------------------------------------------------------------
    def run(self) -> int:
        try:
            for line in self._in:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._send({"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "parse error"}})
                    continue
                if "method" in msg:
                    self._pool.submit(self._dispatch, msg)
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        with self._sessions_lock:
            loaded = list(self._sessions.values())
        for s in loaded:
            if s.tail is not None:
                s.tail.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _dispatch(self, msg: dict) -> None:
        method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        handler = {
            "initialize": self._initialize,
            "session/list": self._list,
            "session/new": self._new,
            "session/load": self._load,
            "session/resume": self._resume,
            "session/prompt": self._prompt,
            "session/cancel": self._cancel,
        }.get(method)
        try:
            if handler is None:
                if req_id is not None:
                    self._error(req_id, -32601, f"Method not supported: {method}")
                return
            result = handler(params)
            after = None
            if isinstance(result, tuple):
                result, after = result
            if req_id is not None and result is not None:
                self._reply(req_id, result)
            if after is not None:
                after()
        except creds.NotLoggedIn as exc:
            if req_id is not None:
                self._error(req_id, -32000, f"{exc}")
        except ApiError as exc:
            if req_id is not None:
                self._error(req_id, -32000 if exc.status != 404 else -32002, _api_message(exc))
        except Exception:  # noqa: BLE001 - a bridge bug must answer, not hang Tern
            # The traceback goes to stderr (Tern's agent log); the chat gets a short message.
            traceback.print_exc(file=sys.stderr)
            if req_id is not None:
                self._error(req_id, -32603, "Internal error in the Symphony bridge. See the agent log.")

    # -- methods -------------------------------------------------------------
    def _initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentInfo": {"name": "symphony", "title": "Symphony", "version": VERSION},
            "authMethods": [],
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {},
                # Tern hands every agent the person's `agents.mcp` servers and warns (in English)
                # about transports an agent lacks. The agent behind this bridge runs remotely
                # with its own tools, so the servers have nothing to attach to here.
                "mcpCapabilities": {"http": True, "sse": True},
                "sessionCapabilities": {"list": {}, "resume": {}},
            },
        }

    def _list(self, params: dict) -> dict:
        cwd = params.get("cwd") or "/"
        items = self.api().sessions(limit=100)
        return {"sessions": [{
            "sessionId": m["sid"],
            "cwd": cwd,
            "title": render.session_title(m),
            "updatedAt": _iso(m.get("updated_at")),
        } for m in items if m.get("sid")]}

    def _new(self, params: dict):
        # The reply goes first: Tern must know the session before updates for it arrive.
        return {"sessionId": NEW_SESSION_ID}, lambda: self._say(NEW_SESSION_ID, NEW_SESSION_HELP)

    def _open(self, sid: str, *, replay: bool) -> None:
        doc = self.api().session(sid)
        meta = doc.get("meta") or {}
        with self._sessions_lock:
            loaded = self._sessions.get(sid)
            if loaded is None:
                loaded = self._sessions[sid] = _Loaded(sid, meta)
            else:
                loaded.mapper.meta.update(meta)
        if replay:
            for upd in acp_map.replay(doc.get("transcript") or []):
                self._update(sid, upd)
            if meta.get("input_request"):
                self._say(sid, acp_map.question_block(meta["input_request"]))
        self._update(sid, {"sessionUpdate": "available_commands_update",
                           "availableCommands": acp_map.COMMANDS})
        if loaded.tail is None:
            loaded.tail = EventTail(self._cred, sid, doc.get("last_event_id"))
            threading.Thread(target=self._follow, args=(loaded,), daemon=True,
                             name=f"tail-{sid}").start()

    def _load(self, params: dict):
        sid = params["sessionId"]
        if sid == NEW_SESSION_ID:
            return {}, lambda: self._say(sid, NEW_SESSION_HELP)
        self._open(sid, replay=True)
        return {}

    def _resume(self, params: dict) -> dict:
        sid = params["sessionId"]
        if sid != NEW_SESSION_ID:
            self._open(sid, replay=False)
        return {}

    def _follow(self, loaded: _Loaded) -> None:
        tail = loaded.tail
        try:
            for ev in tail:
                for upd in loaded.mapper.map(ev.kind, ev.data):
                    self._update(loaded.sid, upd)
        except ApiError as exc:
            self._say(loaded.sid, f"\n_Acompanhamento ao vivo parou: {_api_message(exc)}_\n")

    def _prompt(self, params: dict) -> dict:
        sid = params["sessionId"]
        text = "".join(b.get("text", "") for b in params.get("prompt") or []
                       if b.get("type") == "text").strip()
        if sid == NEW_SESSION_ID:
            self._say(sid, NEW_SESSION_HELP)
            return {"stopReason": "end_turn"}
        if not text:
            return {"stopReason": "end_turn"}
        with self._sessions_lock:
            loaded = self._sessions.get(sid)
        if loaded is None:
            self._open(sid, replay=False)
            with self._sessions_lock:
                loaded = self._sessions[sid]
        try:
            if text.startswith("/"):
                self._command(loaded, text)
            else:
                self._message(loaded, text)
        except ApiError as exc:
            self._say(sid, f"_That did not work: {_api_message(exc)}_")
        return {"stopReason": "end_turn"}

    def _message(self, loaded: _Loaded, text: str) -> None:
        meta = loaded.mapper.meta
        if meta.get("input_request"):
            label = "as the answer to the question"
        elif meta.get("state") == "running":
            label = "as a steer, in the current turn"
        else:
            label = "as a follow-up, after the turn"
        loaded.mapper.echo(text)
        self.api().send_message(loaded.sid, text, "auto")
        self._say(loaded.sid, f"_Delivered to the agent {label}._")

    def _command(self, loaded: _Loaded, text: str) -> None:
        name, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        meta = loaded.mapper.meta
        ticket = meta.get("identifier")
        # The API records who acted from the token; reasons only say where it came from.
        if name == "interrupt":
            reason = rest or "interrupted in Tern"
            if problem := reason_problem("interrupt", reason):
                self._say(loaded.sid, f"_Not sent: {problem}_")
                return
            self.api().interrupt(loaded.sid, reason)
            self._say(loaded.sid, "_Interrupt sent. The agent stops its current turn._")
        elif name in ("pause", "resume"):
            # The API pauses admission for the whole namespace; it has no per-ticket pause.
            reason = rest or f"{name} from Tern"
            if problem := reason_problem("command", reason):
                self._say(loaded.sid, f"_Not sent: {problem}_")
                return
            self.api().command(name, None, reason)
            done = ("Symphony is paused. It takes no new tickets. Running tickets continue."
                    if name == "pause" else "Symphony is resumed. It takes tickets again.")
            self._say(loaded.sid, f"_{done}_")
        elif name == "status":
            doc = self.api().session(loaded.sid)
            meta.update(doc.get("meta") or {})
            self._say(loaded.sid, "```\n" + render.session_detail(meta) + "\n```")
        elif name == "takeover":
            self._say(loaded.sid, TAKEOVER_HELP.format(ticket=ticket or loaded.sid))
        else:
            names = ", ".join("/" + c["name"] for c in acp_map.COMMANDS)
            self._say(loaded.sid, f"_Unknown command /{name}. Available: {names}._")

    def _cancel(self, params: dict) -> None:
        sid = params.get("sessionId")
        if not sid or sid == NEW_SESSION_ID:
            return None
        try:
            self.api().interrupt(sid, "Esc in Tern")
            self._say(sid, "_Interrupt sent._")
        except ApiError as exc:
            self._say(sid, f"_The interrupt did not work: {_api_message(exc)}_")
        return None


def _api_message(exc: ApiError) -> str:
    return render.error_text(exc.code, exc.status, exc.message)


def _iso(value) -> str | None:
    ms = render.to_ms(value)
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def serve() -> int:
    sys.stdout.reconfigure(line_buffering=False)
    return Bridge(sys.stdin, sys.stdout).run()
