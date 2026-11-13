"""Slack mock (internals §14): Web API + incoming webhooks, validated against
`scripts/mocks/specs/slack_web_openapi_v2.json` (slackapi/slack-api-specs).

Served Web API methods (POST or GET, `application/x-www-form-urlencoded` or
`application/json` bodies, query string too, as Slack accepts):

- `chat.postMessage` (scope `chat:write`)
- `chat.update` (only the bot's own messages; else `cant_update_message`)
- `chat.postEphemeral`
- `conversations.replies` (the bot's messages and the user messages pushed over the socket)
- `auth.test`
- `users.lookupByEmail` (scope `users:read.email`)
- `apps.connections.open` (Socket Mode, below)

Another method of the spec → 501 `not_served_by_mock`; a name not in the spec → 404.

Auth as documented: `Authorization: Bearer xoxb-…` (or a `token` argument). No token →
`not_authed`; an unknown token → `invalid_auth`. Arguments are checked against the
operation's parameters in the spec: an unknown argument → `invalid_arg_name`, a wrong
type → `invalid_form_data`. Every Web API answer is HTTP 200 with `ok` (Slack's
convention), except rate limiting: 429 + `Retry-After` and `{"ok":false,"error":"ratelimited"}`.

Incoming webhooks: `POST /services/T…/B…/…` with a JSON body (or form `payload=`) →
`200 ok` as text/html; unparseable body → `400 invalid_payload`; neither `text` nor
`blocks` → `400 no_text`; unknown hook → `404 no_service`.

Socket Mode (`slack-socket-mode.md`, `slack-apps.connections.open.md`):
`apps.connections.open` takes the app-level token in the `Authorization` header only (a
`token` argument alone → `not_authed`; a bot token → `not_allowed_token_type`; unknown →
`invalid_auth`) and returns a one-use `ws://<host>/link/?ticket=…&app_id=…` served by this
mock. On connect the mock sends `hello`. Envelopes carry `payload`, `envelope_id`, `type` and
`accepts_response_payload`, as documented. The client's acks (`{"envelope_id"[, "payload"]}`)
are recorded with their delay; a bad ack is recorded under `errors`. Events: `app_mention`,
`message` (`channel_type` `im` or `channel`); interactive: `block_actions` for a button that is
in a message the bot posted.

Socket control (`/_control/socket*`, JSON):
- `POST /_control/socket/event` `{"event": {...}}` — wrap an event (`type`, `user`, `text`,
  `channel`, optional `ts`/`thread_ts`/`channel_type`/`bot_id`/`subtype`/`files`) in an
  `event_callback` and an `events_api` envelope, and push it → `{"envelope_id", "ts"}`
- `POST /_control/socket/interactive` `{"user", "channel", "message_ts", "action_id"}` —
  push a `block_actions` click on that button → `{"envelope_id"}`
- `POST /_control/socket/disconnect` `{"reason": "refresh_requested", "close_after": 1.0}` —
  send `disconnect`, then close the socket after `close_after` seconds if the app has not
- `POST /_control/socket/drop` — cut the TCP connections without a close frame
- `GET /_control/socket` — `{connections, opened, envelopes, acks, errors}`
Pushing with no open connection → 409 `{"error": "no_connection"}`. Limits: one workspace;
no event retries for unacked envelopes; the `chat.update` answer leaves out `message.blocks`
(the spec types it as an object, Slack returns an array).

Control (`/_mock/*`, JSON):
- `GET /_mock/messages` — everything posted (channel, text, blocks, ts, thread_ts, via)
- `POST /_mock/reset` — drop messages, rate limit and extra fixtures back to the seed
- `POST /_mock/ratelimit` `{"enabled": bool, "retry_after": int}`
- `POST /_mock/channels` `{"id": "C…", "name": "…"}`; `POST /_mock/users` `{"id","name","email"}`;
  `POST /_mock/tokens` `{"token": "xoxb-…"}`; `POST /_mock/app_tokens` `{"token": "xapp-…"}`;
  `POST /_mock/webhooks` `{"path": "/services/…", "channel": "C…"}`
- `GET /_mock/ephemerals`, `GET /_mock/updates` — `chat.postEphemeral` and `chat.update` calls
- `GET /_mock/state` — fixtures.

State (messages + fixtures) is persisted to `<state>/slack.json` after every change and loaded at start.
Socket state is in memory only.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jsonschema

from scripts.mocks.wsserver import WebSocketPeer

SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "slack_web_openapi_v2.json"
METHODS = ("chat.postMessage", "chat.update", "chat.postEphemeral", "conversations.replies",
           "auth.test", "users.lookupByEmail")
SOCKET_METHOD = "apps.connections.open"

TEAM_ID = "T0MOCKTEAM"
BOT_ID = "B0MOCKBOT1"
BOT_USER_ID = "U0MOCKBOT1"
DEFAULT_TOKEN = "xoxb-mock-symphony"
DEFAULT_APP_TOKEN = "xapp-mock-symphony"
APP_ID = "A0MOCKAPP1"
DEFAULT_CHANNEL = {"id": "C0SYMPHONY", "name": "symphony"}
DEFAULT_WEBHOOK = "/services/T0MOCKTEAM/B0MOCKHOOK/mockwebhooksecret"
TS_BASE = 1_700_000_000
# The documented event fields (slack-app_mention.md, slack-message.im.md, slack-message.channels.md).
EVENT_REQUIRED = {
    "app_mention": ("type", "user", "text", "ts", "channel", "event_ts"),
    "message": ("type", "channel", "user", "text", "ts", "event_ts", "channel_type"),
}


def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text())


def _seed() -> dict:
    return {
        "tokens": [DEFAULT_TOKEN],
        "channels": [dict(DEFAULT_CHANNEL)],
        "users": [],
        "webhooks": {DEFAULT_WEBHOOK: DEFAULT_CHANNEL["id"]},
        "messages": [],
        "app_tokens": [DEFAULT_APP_TOKEN],
        "inbound": [],  # user messages pushed over the socket (for conversations.replies)
        "ephemerals": [],
        "updates": [],
        "counter": 0,
        "ratelimit": {"enabled": False, "retry_after": 30},
    }


class _Reject(Exception):
    def __init__(self, error: str):
        self.error = error


class SlackMock:
    """Transport-independent state + Slack semantics."""

    def __init__(self, state_dir: Path | None = None):
        self.spec = load_spec()
        self.state_file = state_dir / "slack.json" if state_dir else None
        self.lock = threading.Lock()
        self.state = _seed()
        if self.state_file and self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))
        self.ops = {m: self._operation(m) for m in METHODS}
        # Socket Mode (in memory)
        self.tickets: set[str] = set()
        self.peers: list[WebSocketPeer] = []
        self.opened = 0
        self.envelopes: dict[str, dict] = {}  # envelope_id → {type, sent_at, accepts}
        self.acks: list[dict] = []
        self.socket_errors: list[dict] = []

    # spec -------------------------------------------------------------------------

    def _operation(self, method: str) -> dict:
        path = self.spec["paths"][f"/{method}"]
        verb, op = next(iter(path.items()))
        params = {p["name"]: p for p in op["parameters"] if p["name"] != "token"}
        return {"verb": verb, "params": params}

    def _args(self, method: str, raw: dict[str, Any], *, form: bool) -> dict[str, Any]:
        params = self.ops[method]["params"]
        args: dict[str, Any] = {}
        for name, value in raw.items():
            if name == "token":
                continue
            spec = params.get(name)
            if spec is None:
                raise _Reject("invalid_arg_name")
            kind = spec.get("type", "string")
            if kind == "boolean":
                if isinstance(value, str):
                    if value.lower() not in ("true", "false", "1", "0"):
                        raise _Reject("invalid_form_data")
                    value = value.lower() in ("true", "1")
            elif kind == "integer" and isinstance(value, str) and value.lstrip("-").isdigit():
                value = int(value)
            elif kind == "number" and isinstance(value, str):
                try:  # a `ts` is a number in the spec; keep the string, it is an id
                    float(value)
                except ValueError:
                    raise _Reject("invalid_form_data") from None
                args[name] = value
                continue
            elif kind == "string" and not form and isinstance(value, (list, dict)):
                value = json.dumps(value)  # JSON bodies carry blocks/attachments as arrays
            try:
                jsonschema.validate(value, {"type": kind})
            except jsonschema.ValidationError:
                raise _Reject("invalid_form_data") from None
            args[name] = value
        return args

    # persistence ------------------------------------------------------------------

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.state, indent=2))

    def _next_ts(self) -> str:
        self.state["counter"] += 1
        n = self.state["counter"]
        return f"{TS_BASE + n}.{n:06d}"

    def _channel(self, ref: str) -> dict | None:
        ref = ref.lstrip("#")
        for ch in self.state["channels"]:
            if ref in (ch["id"], ch["name"]):
                return ch
        return None

    def rate_limited(self) -> int | None:
        rl = self.state["ratelimit"]
        return int(rl["retry_after"]) if rl["enabled"] else None

    # Web API ----------------------------------------------------------------------

    def web_api(self, method: str, token: str | None, raw: dict, *, form: bool) -> dict:
        try:
            if not token:
                raise _Reject("not_authed")
            if token not in self.state["tokens"]:
                raise _Reject("invalid_auth")
            args = self._args(method, raw, form=form)
            with self.lock:
                return getattr(self, "_" + method.replace(".", "_"))(args)
        except _Reject as exc:
            return {"ok": False, "error": exc.error}

    def _auth_test(self, args: dict) -> dict:
        return {
            "ok": True,
            "url": "https://mock-workspace.slack.com/",
            "team": "Symphony Mock",
            "user": "symphony",
            "team_id": TEAM_ID,
            "user_id": BOT_USER_ID,
            "bot_id": BOT_ID,
            "is_enterprise_install": False,
        }

    def _users_lookupByEmail(self, args: dict) -> dict:  # noqa: N802 — Slack method name
        email = str(args.get("email", "")).casefold()
        for user in self.state["users"]:
            if user["email"].casefold() == email:
                return {
                    "ok": True,
                    "user": {
                        "id": user["id"],
                        "name": user["name"],
                        "team_id": TEAM_ID,
                        "deleted": False,
                        "is_bot": False,
                    },
                }
        raise _Reject("users_not_found")

    def _chat_postMessage(self, args: dict) -> dict:  # noqa: N802 — Slack method name
        channel = self._channel(str(args.get("channel", "")))
        if channel is None:
            raise _Reject("channel_not_found")
        text = args.get("text")
        blocks = self._blocks(args.get("blocks"))
        if not text and not blocks and not args.get("attachments"):
            raise _Reject("no_text")
        if text and len(text) > 40_000:
            raise _Reject("msg_too_long")
        thread_ts = args.get("thread_ts")
        if thread_ts is not None:
            try:
                jsonschema.validate(thread_ts, self.spec["definitions"]["defs_ts"])
            except jsonschema.ValidationError:
                raise _Reject("invalid_form_data") from None
        ts = self._next_ts()
        record = {
            "via": "chat.postMessage",
            "channel": channel["id"],
            "text": text or "",
            "blocks": blocks,
            "ts": ts,
            "thread_ts": thread_ts,
            "args": args,
        }
        self.state["messages"].append(record)
        self._save()
        message: dict[str, Any] = {
            "type": "message",
            "subtype": "bot_message",
            "text": text or "",
            "ts": ts,
            "bot_id": BOT_ID,
            "user": BOT_USER_ID,
            "team": TEAM_ID,
        }
        if blocks:
            message["blocks"] = blocks
        if thread_ts:
            message["thread_ts"] = thread_ts
        return {"ok": True, "channel": channel["id"], "ts": ts, "message": message}

    def _blocks(self, raw: Any) -> list | None:
        if raw in (None, ""):
            return None
        try:
            blocks = json.loads(raw) if isinstance(raw, str) else raw
            if blocks == []:
                return []
            jsonschema.validate(blocks, self.spec["definitions"]["blocks"])
        except (ValueError, jsonschema.ValidationError):
            raise _Reject("invalid_form_data") from None
        return blocks

    def _bot_message(self, channel: str, ts: str) -> dict | None:
        for record in self.state["messages"]:
            if (record["via"] == "chat.postMessage" and record["channel"] == channel
                    and record["ts"] == ts):
                return record
        return None

    def _chat_update(self, args: dict) -> dict:
        channel = self._channel(str(args.get("channel", "")))
        if channel is None:
            raise _Reject("channel_not_found")
        ts = str(args.get("ts", ""))
        record = self._bot_message(channel["id"], ts)
        if record is None:
            if any(m["channel"] == channel["id"] and m["ts"] == ts
                   for m in self.state["inbound"] + self.state["messages"]):
                raise _Reject("cant_update_message")
            raise _Reject("message_not_found")
        text = args.get("text")
        blocks = self._blocks(args.get("blocks"))
        if not text and not blocks and not args.get("attachments"):
            raise _Reject("no_text")
        if text and len(text) > 40_000:
            raise _Reject("msg_too_long")
        record["text"] = text or ""
        # slack-chat.update.md: with `text` and no `blocks`, the blocks are removed; an empty
        # array removes them too.
        if blocks is not None or text:
            record["blocks"] = blocks or None
        self.state["updates"].append({"channel": channel["id"], "ts": ts, "text": record["text"],
                                      "blocks": record["blocks"], "args": args})
        self._save()
        return {"ok": True, "channel": channel["id"], "ts": ts, "text": record["text"],
                "message": {"text": record["text"]}}

    def _chat_postEphemeral(self, args: dict) -> dict:  # noqa: N802 — Slack method name
        channel = self._channel(str(args.get("channel", "")))
        if channel is None:
            raise _Reject("channel_not_found")
        user = str(args.get("user", ""))
        if user[:1] not in ("U", "W"):
            raise _Reject("user_not_in_channel")
        text = args.get("text")
        blocks = self._blocks(args.get("blocks"))
        if not text and not blocks and not args.get("attachments"):
            raise _Reject("no_text")
        if text and len(text) > 40_000:
            raise _Reject("msg_too_long")
        ts = self._next_ts()
        self.state["ephemerals"].append({"channel": channel["id"], "user": user,
                                         "text": text or "", "blocks": blocks,
                                         "thread_ts": args.get("thread_ts"), "message_ts": ts})
        self._save()
        return {"ok": True, "message_ts": ts}

    def _conversations_replies(self, args: dict) -> dict:
        channel = self._channel(str(args.get("channel", "")))
        if channel is None:
            raise _Reject("channel_not_found")
        root = str(args.get("ts", ""))
        found = [m for m in self.state["messages"] + self.state["inbound"]
                 if m["channel"] == channel["id"] and root in (m["ts"], m.get("thread_ts"))
                 and m["via"] in ("chat.postMessage", "user")]
        if not any(m["ts"] == root for m in found):
            raise _Reject("thread_not_found")
        found.sort(key=lambda m: float(m["ts"]))
        parent = next(m for m in found if m["ts"] == root)
        replies = [m for m in found if m["ts"] != root]
        limit = int(args.get("limit") or 1000)
        out = [{"type": "message", "user": parent.get("user", BOT_USER_ID), "text": parent["text"],
                "thread_ts": root, "reply_count": len(replies), "subscribed": False, "ts": root}]
        for m in replies:
            out.append({"type": "message", "user": m.get("user", BOT_USER_ID), "text": m["text"],
                        "thread_ts": root, "parent_user_id": out[0]["user"], "ts": m["ts"]})
        return {"ok": True, "messages": out[:limit], "has_more": len(out) > limit}

    # Socket Mode ------------------------------------------------------------------

    def connections_open(self, token: str | None, host: str) -> dict:
        """`apps.connections.open`: the app-level token comes in the header only."""
        if not token:
            return {"ok": False, "error": "not_authed"}
        if token in self.state["tokens"]:
            return {"ok": False, "error": "not_allowed_token_type"}
        if token not in self.state["app_tokens"]:
            return {"ok": False, "error": "invalid_auth"}
        ticket = uuid.uuid4().hex
        with self.lock:
            self.tickets.add(ticket)
        return {"ok": True, "url": f"ws://{host}/link/?ticket={ticket}&app_id={APP_ID}"}

    def claim_ticket(self, ticket: str) -> bool:
        with self.lock:
            if ticket in self.tickets:
                self.tickets.discard(ticket)
                return True
            return False

    def socket_opened(self, peer: WebSocketPeer) -> None:
        with self.lock:
            self.peers.append(peer)
            self.opened += 1
            count = len(self.peers)
        peer.send(json.dumps({
            "type": "hello",
            "num_connections": count,
            "debug_info": {"host": "applink-mock", "build_number": 1,
                           "approximate_connection_time": 18060},
            "connection_info": {"app_id": APP_ID},
        }))

    def socket_closed(self, peer: WebSocketPeer) -> None:
        with self.lock:
            if peer in self.peers:
                self.peers.remove(peer)

    def socket_message(self, text: str) -> None:
        """An ack from the app: `{"envelope_id"[, "payload"]}` (slack-socket-mode.md)."""
        now = time.monotonic()
        with self.lock:
            try:
                ack = json.loads(text)
            except ValueError:
                ack = None
            error = None
            if not isinstance(ack, dict) or not isinstance(ack.get("envelope_id"), str):
                error = "not_an_ack"
            elif set(ack) - {"envelope_id", "payload"}:
                error = "unknown_fields"
            else:
                envelope = self.envelopes.get(ack["envelope_id"])
                if envelope is None:
                    error = "unknown_envelope"
                elif envelope["acked"]:
                    error = "duplicate_ack"
                elif "payload" in ack and not envelope["accepts_response_payload"]:
                    error = "payload_not_accepted"
            if error:
                self.socket_errors.append({"error": error, "message": text[:500]})
                return
            envelope["acked"] = True
            entry = {"envelope_id": ack["envelope_id"], "type": envelope["type"],
                     "delay_s": round(now - envelope["sent_at"], 4)}
            if "payload" in ack:
                entry["payload"] = ack["payload"]
            self.acks.append(entry)

    def _push(self, kind: str, payload: dict) -> tuple[int, dict]:
        # caller holds self.lock
        if not self.peers:
            return 409, {"error": "no_connection"}
        envelope_id = str(uuid.uuid4())
        accepts = kind != "events_api"
        self.envelopes[envelope_id] = {"envelope_id": envelope_id, "type": kind,
                                       "sent_at": time.monotonic(), "acked": False,
                                       "accepts_response_payload": accepts, "payload": payload}
        envelope = {"payload": payload, "envelope_id": envelope_id, "type": kind,
                    "accepts_response_payload": accepts}
        self.peers[-1].send(json.dumps(envelope))
        return 200, {"envelope_id": envelope_id}

    def _socket_event(self, body: dict) -> tuple[int, dict]:
        event = dict(body.get("event") or {})
        kind = event.get("type")
        if kind not in EVENT_REQUIRED:
            return 400, {"error": "unsupported_event_type"}
        channel = self._channel(str(event.get("channel", "")))
        if channel is None:
            return 400, {"error": "channel_not_found"}
        event["channel"] = channel["id"]
        ts = event.get("ts") or self._next_ts()
        event.setdefault("ts", ts)
        event.setdefault("event_ts", ts)
        is_dm = channel["id"].startswith("D")
        if kind == "message":
            event.setdefault("channel_type", "im" if is_dm else "channel")
        elif is_dm:  # slack-app_mention.md: DMs are not dispatched via app_mention
            return 400, {"error": "app_mention_not_sent_in_dm"}
        missing = [k for k in EVENT_REQUIRED[kind] if k not in event]
        if missing:
            return 400, {"error": "missing_fields", "fields": missing}
        if not event.get("bot_id"):
            self.state["inbound"].append({"via": "user", "channel": channel["id"], "ts": ts,
                                          "thread_ts": event.get("thread_ts"),
                                          "user": event["user"], "text": event["text"]})
            self._save()
        payload = {
            "token": "mockverificationtoken",
            "team_id": TEAM_ID,
            "api_app_id": APP_ID,
            "event": event,
            "type": "event_callback",
            "event_id": f"Ev{self.state['counter']:08d}",
            "event_time": int(time.time()),
            "authorizations": [{"enterprise_id": None, "team_id": TEAM_ID,
                                "user_id": BOT_USER_ID, "is_bot": True,
                                "is_enterprise_install": False}],
        }
        status, out = self._push("events_api", payload)
        return status, dict(out, ts=ts) if status == 200 else out

    def _socket_interactive(self, body: dict) -> tuple[int, dict]:
        channel = self._channel(str(body.get("channel", "")))
        if channel is None:
            return 400, {"error": "channel_not_found"}
        ts = str(body.get("message_ts", ""))
        record = self._bot_message(channel["id"], ts)
        if record is None:
            return 404, {"error": "message_not_found"}
        found = None
        for block in record.get("blocks") or []:
            for element in block.get("elements") or [] if block.get("type") == "actions" else []:
                if element.get("action_id") == body.get("action_id"):
                    found = (block, element)
        if found is None:
            return 400, {"error": "action_not_found"}
        block, button = found
        user = str(body.get("user", ""))
        message = {"type": "message", "user": BOT_USER_ID, "bot_id": BOT_ID, "text": record["text"],
                   "ts": ts, "blocks": record.get("blocks") or []}
        if record.get("thread_ts"):
            message["thread_ts"] = record["thread_ts"]
        action = {"type": "button", "action_id": button["action_id"],
                  "block_id": block.get("block_id", "mockblock"), "text": button["text"],
                  "action_ts": f"{time.time():.6f}"}
        for key in ("value", "style"):
            if key in button:
                action[key] = button[key]
        payload = {
            "type": "block_actions",
            "user": {"id": user, "username": user.lower(), "name": user.lower(), "team_id": TEAM_ID},
            "api_app_id": APP_ID,
            "token": "mockverificationtoken",
            "container": {"type": "message", "message_ts": ts, "channel_id": channel["id"],
                          "is_ephemeral": False},
            "trigger_id": f"{self.state['counter']}.{TS_BASE}.mocktrigger",
            "team": {"id": TEAM_ID, "domain": "mock-workspace"},
            "enterprise": None,
            "is_enterprise_install": False,
            "channel": {"id": channel["id"], "name": channel["name"]},
            "message": message,
            "state": {"values": {}},
            "response_url": f"https://hooks.slack.com/actions/{TEAM_ID}/0/mock",
            "actions": [action],
        }
        return self._push("interactive", payload)

    def socket_control(self, verb: str, name: str, body: dict) -> tuple[int, Any]:
        with self.lock:
            if verb == "GET" and name == "":
                return 200, {
                    "connections": len(self.peers),
                    "opened": self.opened,
                    "envelopes": [{k: v for k, v in e.items() if k != "sent_at"}
                                  for e in self.envelopes.values()],
                    "acks": copy.deepcopy(self.acks),
                    "errors": copy.deepcopy(self.socket_errors),
                }
            if verb != "POST":
                return 404, {"error": "unknown control route"}
            if name == "event":
                return self._socket_event(body)
            if name == "interactive":
                return self._socket_interactive(body)
            peers = list(self.peers)
        if name == "disconnect":
            message = json.dumps({"type": "disconnect",
                                  "reason": body.get("reason", "refresh_requested"),
                                  "debug_info": {"host": "applink-mock"}})
            for peer in peers:
                peer.send(message)
                timer = threading.Timer(float(body.get("close_after", 1.0)), peer.close)
                timer.daemon = True
                timer.start()
            return 200, {"ok": True, "connections": len(peers)}
        if name == "drop":
            for peer in peers:
                peer.drop()
            return 200, {"ok": True, "connections": len(peers)}
        return 404, {"error": "unknown control route"}

    # incoming webhooks ------------------------------------------------------------

    def webhook(self, path: str, body: bytes, content_type: str) -> tuple[int, str]:
        with self.lock:
            channel = self.state["webhooks"].get(path)
            if channel is None:
                return 404, "no_service"
            try:
                if content_type.startswith("application/x-www-form-urlencoded"):
                    payload = json.loads(parse_qs(body.decode())["payload"][0])
                else:
                    payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, KeyError, UnicodeDecodeError):
                return 400, "invalid_payload"
            text = payload.get("text")
            blocks = payload.get("blocks")
            if not text and not blocks:
                return 400, "no_text"
            if blocks is not None:
                try:
                    jsonschema.validate(blocks, self.spec["definitions"]["blocks"])
                except jsonschema.ValidationError:
                    return 400, "invalid_blocks"
            self.state["messages"].append({
                "via": "webhook",
                "channel": channel,
                "text": text or "",
                "blocks": blocks,
                "ts": self._next_ts(),
                "thread_ts": payload.get("thread_ts"),
                "args": payload,
            })
            self._save()
            return 200, "ok"

    # control ----------------------------------------------------------------------

    def control(self, verb: str, name: str, body: dict) -> tuple[int, Any]:
        with self.lock:
            st = self.state
            if verb == "GET" and name == "messages":
                return 200, {"messages": copy.deepcopy(st["messages"])}
            if verb == "GET" and name in ("ephemerals", "updates"):
                return 200, {name: copy.deepcopy(st[name])}
            if verb == "GET" and name == "state":
                return 200, {k: v for k, v in st.items() if k != "messages"}
            if verb != "POST":
                return 404, {"error": "unknown control route"}
            if name == "reset":
                self.state = _seed()
                self.tickets.clear()
                self.envelopes.clear()
                self.acks.clear()
                self.socket_errors.clear()
            elif name == "ratelimit":
                st["ratelimit"] = {
                    "enabled": bool(body.get("enabled", True)),
                    "retry_after": int(body.get("retry_after", 30)),
                }
            elif name == "channels":
                st["channels"].append({"id": body["id"], "name": body["name"]})
            elif name == "users":
                st["users"].append({k: body[k] for k in ("id", "name", "email")})
            elif name == "tokens":
                st["tokens"].append(body["token"])
            elif name == "app_tokens":
                st["app_tokens"].append(body["token"])
            elif name == "webhooks":
                st["webhooks"][body["path"]] = body["channel"]
            else:
                return 404, {"error": "unknown control route"}
            self._save()
            return 200, {"ok": True}


def make_handler(mock: SlackMock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any, headers: dict | None = None):
            self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8", headers)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, verb: str):
            url = urlsplit(self.path)
            if (verb == "GET" and url.path.rstrip("/") == "/link"
                    and self.headers.get("Upgrade", "").lower() == "websocket"):
                return self._socket(url.query)
            body = self._body()
            ctype = self.headers.get("Content-Type", "")
            if url.path.startswith("/_mock/"):
                data = json.loads(body) if body else {}
                status, out = mock.control(verb, url.path[len("/_mock/"):], data)
                return self._json(status, out)
            if url.path == "/_control/socket" or url.path.startswith("/_control/socket/"):
                data = json.loads(body) if body else {}
                name = url.path[len("/_control/socket"):].strip("/")
                status, out = mock.socket_control(verb, name, data)
                return self._json(status, out)
            if url.path.startswith("/services/"):
                retry = mock.rate_limited()
                if retry is not None:
                    return self._send(429, b"rate_limited", "text/html",
                                      {"Retry-After": str(retry)})
                status, text = mock.webhook(url.path, body, ctype)
                return self._send(status, text.encode(), "text/html")
            method = url.path.removeprefix("/api/").lstrip("/")
            if method not in METHODS and method != SOCKET_METHOD:
                if f"/{method}" in mock.spec["paths"]:  # a real method this mock does not serve
                    return self._json(501, {"ok": False, "error": "not_served_by_mock"})
                return self._json(404, {"ok": False, "error": "unknown_method"})
            retry = mock.rate_limited()
            if retry is not None:
                return self._json(429, {"ok": False, "error": "ratelimited"},
                                  {"Retry-After": str(retry)})
            raw: dict[str, Any] = {k: v[-1] for k, v in parse_qs(url.query).items()}
            form = True
            if body:
                if ctype.startswith("application/json"):
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        return self._json(200, {"ok": False, "error": "invalid_form_data"})
                    if not isinstance(parsed, dict):
                        return self._json(200, {"ok": False, "error": "invalid_form_data"})
                    raw.update(parsed)
                    form = False
                elif ctype.startswith("application/x-www-form-urlencoded"):
                    raw.update({k: v[-1] for k, v in parse_qs(body.decode()).items()})
                else:
                    return self._json(200, {"ok": False, "error": "invalid_post_type"})
            token = None
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[len("Bearer "):].strip() or None
            elif method == SOCKET_METHOD:
                token = None  # slack-apps.connections.open.md: the header only
            elif isinstance(raw.get("token"), str) and form:
                token = raw["token"]  # Slack ignores `token` inside JSON bodies
            if method == SOCKET_METHOD:
                host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
                return self._json(200, mock.connections_open(token, host))
            self._json(200, mock.web_api(method, token, raw, form=form))

        def _socket(self, query: str) -> None:
            self.close_connection = True
            ticket = (parse_qs(query).get("ticket") or [""])[-1]
            peer = WebSocketPeer(self.connection)
            reject = None if mock.claim_ticket(ticket) else (401, "invalid ticket")
            if not peer.handshake(self.requestline, self.headers, reject):
                return
            mock.socket_opened(peer)
            try:
                peer.run(mock.socket_message)
            finally:
                mock.socket_closed(peer)

    return Handler


def serve(port: int, state_dir: Path | None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(SlackMock(state_dir)))
    server.daemon_threads = True
    return server
