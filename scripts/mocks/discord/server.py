"""Discord mock (internals §14): Execute Webhook and Create Message, validated against
`scripts/mocks/specs/discord-openapi.json.gz` (discord/discord-api-spec) and the vendored
`discord-*.md` reference pages.

Served routes, under `/api` with or without a version (`/api/v10/...`), as Discord serves them:

- `POST /webhooks/{webhook.id}/{webhook.token}` (`execute_webhook`): no auth header; `wait`
  (boolean query string) → 200 + the message, otherwise 204.
- `POST /channels/{channel.id}/messages` (`create_message`): `Authorization: Bot <token>`. A
  `message_reference` must name a known message (`fail_if_not_exists` defaults to true).
- `PATCH /channels/{channel.id}/messages/{message.id}` (`update_message`): the bot's own
  messages only (else 403 `Cannot edit a message authored by another user`, 50005).
- `GET /gateway/bot` and `GET /applications/@me` (with `flags`; `POST /_mock/application`
  `{"flags": int}` sets them, default `GATEWAY_MESSAGE_CONTENT`, `1 << 18`).
- `POST /interactions/{interaction.id}/{interaction.token}/callback`: no bot token needed.
  The token must match, the first answer must come within 3 s (`discord-receiving-and-
  responding.md`), else 404 `Unknown interaction` (10062); a second answer → 400 (40060).
  Type 4 posts a message (an ephemeral one with flag 64 is only recorded), 6 only acks, 7
  edits the message the button is on.
- Any other operation of the spec → 501; a path not in the spec → 404.

Gateway (`discord-gateway.md`, `discord-gateway-events.md`), a websocket at the URL that
`GET /gateway/bot` returns, `?v=10&encoding=json` (else close 4012 / 4002):
- Hello (op 10) at once; Heartbeat (op 1) → Heartbeat ACK (op 11), unless turned off.
- Identify (op 2): token (4004), `properties.os|browser|device`, `intents` (bits outside the
  documented list → 4013; `MESSAGE_CONTENT` without the application flag → 4014); twice → 4005.
  → Dispatch `READY` with `session_id` and `resume_gateway_url`.
- Resume (op 6): replays the session's missed dispatches after `seq`, then `RESUMED`; an
  unknown or invalidated session → Invalid Session (op 9) `d: false`; `seq` ahead → 4007.
- Before Identify only Heartbeat is allowed (4003); unknown op → 4001; a payload over 4096
  bytes → 4002; more than 120 payloads per 60 s → 4008.
- Without the `MESSAGE_CONTENT` intent, guild messages that do not mention the bot arrive
  with empty `content`, as documented.

Bodies are `application/json` or `application/x-www-form-urlencoded`, checked against the
operation's request schema; `multipart/form-data` (file uploads) is not modelled. Errors are
Discord's `{"code", "message"[, "errors"]}`; nested `errors` follow the docs (string codes such
as `BASE_TYPE_MAX_LENGTH`), where the spec types them as integers:

- missing or unknown bot token → 401 `401: Unauthorized` (code 0); a token without access to
  the channel → 403 `Missing Access` (50001); unknown channel → 404 `Unknown Channel` (10003);
- unknown webhook → 404 `Unknown Webhook` (10015); wrong webhook token → 401
  `Invalid Webhook Token` (50027);
- unparseable JSON → 400 (50109); a schema violation, content over 2000 characters (the
  docs' limit), `parse` listing a mention type that is also listed explicitly, or a bad path
  snowflake / boolean query string → 400 `Invalid Form Body` (50035) with an `errors` tree;
  no content → 400 `Cannot send an empty message` (50006).
- rate limiting (control below) → 429 with `Retry-After`, `X-RateLimit-*` and
  `{"message", "retry_after", "global", "code"}` (the spec requires `code`; the docs'
  example omits it).

Mentions follow the allowed-mentions rules: `<@id>` in the content pings a known user only
when `allowed_mentions` allows it (default: every type for bot messages, users only for
webhooks); `@everyone`/`@here` only with `everyone` parsed.

Control (`/_mock/*`, JSON):
- `GET /_mock/messages` — everything posted (via, channel_id, content, allowed_mentions,
  mentions, mention_everyone, user_agent, ...)
- `POST /_mock/reset` — back to the seed
- `POST /_mock/ratelimit` `{"times": int, "retry_after": float, "global": bool}` — the next
  `times` API requests get a 429 (`times: 0` turns it off)
- `POST /_mock/tokens` `{"token", "channels": [id, ...]}`; `POST /_mock/channels`
  `{"id", "name"}`; `POST /_mock/webhooks` `{"id", "token", "channel_id", "name"}`;
  `POST /_mock/users` `{"id", "username"}`
- `GET /_mock/state` — fixtures. `GET /_mock/callbacks`, `GET /_mock/edits` — interaction
  answers and message edits.

Gateway control (`/_control/gateway*`, JSON):
- `GET /_control/gateway` — `{connections, identifies, resumes, heartbeats, closes, errors,
  sessions}`
- `POST /_control/gateway/hello` `{"heartbeat_interval": ms}` — for the next connections
- `POST /_control/gateway/heartbeat_ack` `{"enabled": bool}` — stop ACKs to make a zombie
- `POST /_control/gateway/heartbeat_request` — send op 1
- `POST /_control/gateway/reconnect` — send op 7
- `POST /_control/gateway/invalid_session` `{"resumable": bool}` — send op 9; when not
  resumable, the session is dropped
- `POST /_control/gateway/close` `{"code": 4000}`; `POST /_control/gateway/drop` (no frame)
- `POST /_control/gateway/dispatch` `{"t", "d"}` — any dispatch
- `POST /_control/gateway/message` `{"channel_id", "author_id", "content", "guild_id"?,
  "channel_type"?, "reference"?: message id}` → `MESSAGE_CREATE`, `{"id"}`
- `POST /_control/gateway/thread` `{"id", "parent_id", "owner_id", "name"}` → `THREAD_CREATE`
- `POST /_control/gateway/interaction` `{"custom_id", "user_id", "channel_id", "message_id",
  "guild_id"?}` — a click on a button of a bot message → `INTERACTION_CREATE`, `{"id", "token"}`
A dispatch with no live connection is kept for a resume. Limits: one shard, JSON encoding
only (no compression), no guild state beyond what the control pushes.

State is persisted to `<state>/discord.json` after every change and loaded at start. Gateway
sessions and pending interactions are in memory only.
"""

from __future__ import annotations

import copy
import functools
import gzip
import json
import math
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from scripts.mocks.discord.gateway import INTENTS, DiscordGateway
from scripts.mocks.wsserver import WebSocketPeer

SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "discord-openapi.json.gz"
SPEC_URI = "urn:discord-openapi"
WEBHOOK_ROUTE = "/webhooks/{webhook_id}/{webhook_token}"
MESSAGES_ROUTE = "/channels/{channel_id}/messages"
MESSAGE_ROUTE = "/channels/{channel_id}/messages/{message_id}"
CALLBACK_ROUTE = "/interactions/{interaction_id}/{interaction_token}/callback"
MAX_CONTENT = 2000  # "the message contents (up to 2000 characters)"
EPHEMERAL = 1 << 6
INTERACTION_WINDOW_S = 3.0  # "you must send an initial response within 3 seconds"
DEFAULT_APP_FLAGS = 1 << 18  # GATEWAY_MESSAGE_CONTENT

DEFAULT_TOKEN = "mock-discord-bot-token"
BOT_USER = {"id": "1300000000000000001", "username": "symphony"}
DEFAULT_CHANNEL = {"id": "1100000000000000001", "name": "symphony"}
DEFAULT_WEBHOOK = {
    "id": "1200000000000000001",
    "token": "mock-webhook-token",
    "channel_id": DEFAULT_CHANNEL["id"],
    "name": "Symphony",
}
DEFAULT_WEBHOOK_PATH = f"/api/webhooks/{DEFAULT_WEBHOOK['id']}/{DEFAULT_WEBHOOK['token']}"
ID_BASE = 1_400_000_000_000_000_000
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_MENTION = re.compile(r"<@!?(\d+)>")
_SNOWFLAKE = re.compile(r"(0|[1-9][0-9]*)\Z")
_TRUE, _FALSE = ("True", "true", "1"), ("False", "false", "0")


@functools.cache
def load_spec() -> dict:
    with gzip.open(SPEC_PATH) as fh:
        return json.load(fh)


@functools.cache
def _registry() -> Registry:
    return Registry().with_resource(SPEC_URI, DRAFT202012.create_resource(load_spec()))


@functools.cache
def validator(pointer: str) -> Draft202012Validator:
    """A validator for a JSON pointer into the spec (`#/components/schemas/Error`)."""
    return Draft202012Validator({"$ref": f"{SPEC_URI}{pointer}"}, registry=_registry())


def _request_pointer(route: str, media: str, verb: str = "post") -> str:
    escaped = route.replace("~", "~0").replace("/", "~1")
    return f"#/paths/{escaped}/{verb}/requestBody/content/{media.replace('/', '~1')}/schema"


def _seed() -> dict:
    return {
        "tokens": {DEFAULT_TOKEN: [DEFAULT_CHANNEL["id"]]},
        "channels": [dict(DEFAULT_CHANNEL)],
        "webhooks": [dict(DEFAULT_WEBHOOK)],
        "users": [],
        "messages": [],
        "received": [],  # user messages pushed over the gateway: {id, channel_id, author_id}
        "callbacks": [],
        "edits": [],
        "application": {"flags": DEFAULT_APP_FLAGS},
        "counter": 0,
        "ratelimit": {"times": 0, "retry_after": 1.0, "global": False},
    }


class DiscordError(Exception):
    def __init__(self, status: int, code: int, message: str, errors: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body: dict[str, Any] = {"code": code, "message": message}
        if errors:
            self.body["errors"] = errors


def _form_error(field: str | None, code: str, message: str) -> DiscordError:
    tree: dict = {"_errors": [{"code": code, "message": message}]}
    return DiscordError(400, 50035, "Invalid Form Body", {field: tree} if field else tree)


def _schema_error(error) -> DiscordError:
    tree: dict = {"_errors": [{"code": _error_code(error), "message": error.message[:300]}]}
    for part in reversed(list(error.absolute_path)):
        tree = {str(part): tree}
    return DiscordError(400, 50035, "Invalid Form Body", tree)


def _error_code(error) -> str:
    # The codes the docs show; nested `_errors[].code` is a string there (the spec says integer).
    if error.validator in ("maxLength", "maxItems"):
        return "BASE_TYPE_MAX_LENGTH"
    if error.validator == "required":
        return "BASE_TYPE_REQUIRED"
    if error.validator == "type":
        return "MODEL_TYPE_CONVERT"
    return "BASE_TYPE_CHOICES"


class DiscordMock:
    """Transport-independent state + Discord semantics."""

    def __init__(self, state_dir: Path | None = None):
        self.state_file = state_dir / "discord.json" if state_dir else None
        self.lock = threading.Lock()
        self.state = _seed()
        if self.state_file and self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))
        self.interactions: dict[str, dict] = {}  # id → {token, channel_id, message_id, at, answered}
        # Read without the mock lock: the gateway calls these under its own lock.
        self.gateway = DiscordGateway(
            token_ok=lambda token: token in self.state["tokens"],
            app_flags=lambda: int(self.state["application"]["flags"]),
            bot_user=_user(BOT_USER, bot=True),
            application_id=BOT_USER["id"],
        )

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.state, indent=2))

    # rate limit -------------------------------------------------------------------

    def take_rate_limit(self) -> tuple[dict, dict] | None:
        """The 429 body and headers when this request is rate limited."""
        with self.lock:
            rl = self.state["ratelimit"]
            if rl["times"] <= 0:
                return None
            rl["times"] -= 1
            self._save()
            retry = float(rl["retry_after"])
            body = {"message": "You are being rate limited.", "retry_after": retry,
                    "global": bool(rl["global"]), "code": 0}
            headers = {
                "Retry-After": str(math.ceil(retry)),
                "X-RateLimit-Limit": "5",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": f"{EPOCH.timestamp() + retry:.3f}",
                "X-RateLimit-Reset-After": f"{retry:.3f}",
                "X-RateLimit-Bucket": "mockbucket",
                "X-RateLimit-Scope": "global" if rl["global"] else "user",
            }
            if rl["global"]:
                headers["X-RateLimit-Global"] = "true"
            return body, headers

    # API --------------------------------------------------------------------------

    def execute_webhook(self, webhook_id: str, token: str, query: dict, body: dict,
                        user_agent: str) -> dict | None:
        _snowflake("webhook_id", webhook_id)
        wait = _boolean("wait", query.get("wait"), default=False)
        with self.lock:
            hook = next((h for h in self.state["webhooks"] if h["id"] == webhook_id), None)
            if hook is None:
                raise DiscordError(404, 10015, "Unknown Webhook")
            if hook["token"] != token:
                raise DiscordError(401, 50027, "Invalid Webhook Token")
            author = {"id": hook["id"], "username": hook["name"]}
            message = self._create(WEBHOOK_ROUTE, hook["channel_id"], author, body,
                                   via="webhook", user_agent=user_agent, webhook_id=hook["id"],
                                   default_parse=["users"])
        return message if wait else None

    def create_message(self, channel_id: str, auth: str, body: dict, user_agent: str) -> dict:
        token = auth[len("Bot "):].strip() if auth.startswith("Bot ") else ""
        with self.lock:
            channels = self.state["tokens"].get(token)
            if channels is None:
                raise DiscordError(401, 0, "401: Unauthorized")
            _snowflake("channel_id", channel_id)
            if not any(c["id"] == channel_id for c in self.state["channels"]):
                raise DiscordError(404, 10003, "Unknown Channel")
            if channel_id not in channels:
                raise DiscordError(403, 50001, "Missing Access")
            return self._create(MESSAGES_ROUTE, channel_id, dict(BOT_USER), body, via="bot",
                                user_agent=user_agent, webhook_id=None,
                                default_parse=["users", "roles", "everyone"])

    def _create(self, route: str, channel_id: str, author: dict, body: dict, *, via: str,
                user_agent: str, webhook_id: str | None, default_parse: list[str]) -> dict:
        problem = best_match(validator(_request_pointer(route, "application/json")).iter_errors(body))
        if problem is not None:
            raise _schema_error(problem)
        content = body.get("content") or ""
        if len(content) > MAX_CONTENT:
            raise _form_error("content", "BASE_TYPE_MAX_LENGTH",
                              f"Must be {MAX_CONTENT} or fewer in length.")
        if not content and not any(body.get(k) for k in ("embeds", "components", "sticker_ids",
                                                          "poll", "attachments")):
            raise DiscordError(400, 50006, "Cannot send an empty message")
        allowed = body.get("allowed_mentions")
        parse = default_parse if allowed is None else [p for p in allowed.get("parse") or [] if p]
        explicit = {} if allowed is None else {
            kind: [x for x in allowed.get(kind) or [] if x] for kind in ("users", "roles")
        }
        for kind, ids in explicit.items():
            if ids and kind in parse:
                raise _form_error("allowed_mentions", "MESSAGE_ALLOWED_MENTIONS_PARSE_EXCLUSIVE",
                                  f'parse:["{kind}"] and {kind}: [ids...] are mutually exclusive.')
        known = {u["id"]: u for u in self.state["users"]}
        pinged = []
        for user_id in dict.fromkeys(_MENTION.findall(content)):
            if user_id in known and ("users" in parse or user_id in explicit.get("users", [])):
                pinged.append(_user(known[user_id]))
        everyone = "everyone" in parse and ("@everyone" in content or "@here" in content)
        reference = body.get("message_reference")
        if reference and reference.get("message_id") and reference.get(
                "fail_if_not_exists", True) and not self._known_message(reference["message_id"]):
            raise _form_error("message_reference", "MESSAGE_REFERENCE_UNKNOWN_MESSAGE",
                              "Unknown message")
        self.state["counter"] += 1
        n = self.state["counter"]
        message: dict[str, Any] = {
            "type": 0,
            "content": content,
            "mentions": pinged,
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "timestamp": (EPOCH + timedelta(seconds=n)).isoformat(),
            "edited_timestamp": None,
            "flags": 0,
            "components": _components(body.get("components")),
            "id": str(ID_BASE + n),
            "channel_id": channel_id,
            "author": _user(author, bot=True),
            "pinned": False,
            "mention_everyone": everyone,
            "tts": bool(body.get("tts")),
        }
        if webhook_id:
            message["webhook_id"] = webhook_id
        if reference and reference.get("message_id"):
            message["message_reference"] = {
                "type": 0, "channel_id": reference.get("channel_id") or channel_id,
                "message_id": reference["message_id"]}
        self.state["messages"].append({
            "via": via,
            "id": message["id"],
            "channel_id": channel_id,
            "content": content,
            "allowed_mentions": allowed,
            "mentions": [u["id"] for u in pinged],
            "mention_everyone": everyone,
            "user_agent": user_agent,
            "body": body,
            "message": message,
        })
        self._save()
        return message

    def _known_message(self, message_id: str) -> bool:
        return any(m["id"] == message_id for m in self.state["messages"] + self.state["received"])

    def _bot_record(self, channel_id: str, message_id: str) -> dict | None:
        return next((m for m in self.state["messages"] if m["id"] == message_id
                     and m["channel_id"] == channel_id), None)

    def edit_message(self, channel_id: str, message_id: str, auth: str, body: dict) -> dict:
        token = auth[len("Bot "):].strip() if auth.startswith("Bot ") else ""
        with self.lock:
            channels = self.state["tokens"].get(token)
            if channels is None:
                raise DiscordError(401, 0, "401: Unauthorized")
            _snowflake("channel_id", channel_id)
            _snowflake("message_id", message_id)
            if not any(c["id"] == channel_id for c in self.state["channels"]):
                raise DiscordError(404, 10003, "Unknown Channel")
            if channel_id not in channels:
                raise DiscordError(403, 50001, "Missing Access")
            problem = best_match(validator(_request_pointer(
                MESSAGE_ROUTE, "application/json", "patch")).iter_errors(body))
            if problem is not None:
                raise _schema_error(problem)
            record = self._bot_record(channel_id, message_id)
            if record is None or record["via"] != "bot":
                if any(m["id"] == message_id for m in self.state["received"]
                       + self.state["messages"]):
                    raise DiscordError(403, 50005,
                                       "Cannot edit a message authored by another user")
                raise DiscordError(404, 10008, "Unknown Message")
            return self._apply_edit(record, body, via="edit")

    def _apply_edit(self, record: dict, body: dict, *, via: str) -> dict:
        message = record["message"]
        if "content" in body:
            content = body.get("content") or ""
            if len(content) > MAX_CONTENT:
                raise _form_error("content", "BASE_TYPE_MAX_LENGTH",
                                  f"Must be {MAX_CONTENT} or fewer in length.")
            message["content"] = record["content"] = content
        if "components" in body:
            message["components"] = _components(body.get("components"))
            record["body"]["components"] = body.get("components") or []
        self.state["counter"] += 1
        message["edited_timestamp"] = (EPOCH + timedelta(seconds=self.state["counter"])).isoformat()
        self.state["edits"].append({"via": via, "id": record["id"],
                                    "channel_id": record["channel_id"], "body": body})
        self._save()
        return message

    def gateway_bot(self, auth: str, host: str) -> dict:
        if self._bot_token(auth) is None:
            raise DiscordError(401, 0, "401: Unauthorized")
        return {"url": f"ws://{host}", "shards": 1,
                "session_start_limit": {"total": 1000, "remaining": 999,
                                        "reset_after": 14_400_000, "max_concurrency": 1}}

    def application(self, auth: str) -> dict:
        if self._bot_token(auth) is None:
            raise DiscordError(401, 0, "401: Unauthorized")
        flags = int(self.state["application"]["flags"])
        return {
            "id": BOT_USER["id"], "name": BOT_USER["username"], "icon": None,
            "description": "", "type": None, "bot_public": False,
            "bot_require_code_grant": False, "flags": flags, "flags_new": str(flags),
            "verify_key": "0" * 64, "redirect_uris": [], "interactions_endpoint_url": None,
            "role_connections_verification_url": None,
            "owner": _user({"id": "1300000000000000099", "username": "owner"}),
            "approximate_guild_count": 1, "approximate_user_install_count": 0,
            "approximate_user_authorization_count": 0, "explicit_content_filter": 0,
            "team": None, "eligible_oauth2_scopes": ["bot", "applications.commands"],
            "bot": _user(BOT_USER, bot=True),
        }

    def _bot_token(self, auth: str) -> str | None:
        token = auth[len("Bot "):].strip() if auth.startswith("Bot ") else ""
        return token if token in self.state["tokens"] else None

    # interactions -------------------------------------------------------------------

    def callback(self, interaction_id: str, token: str, body: dict) -> None:
        with self.lock:
            _snowflake("interaction_id", interaction_id)
            problem = best_match(validator(_request_pointer(
                CALLBACK_ROUTE, "application/json")).iter_errors(body))
            if problem is not None:
                raise _schema_error(problem)
            pending = self.interactions.get(interaction_id)
            if pending is None or pending["token"] != token:
                raise DiscordError(404, 10062, "Unknown interaction")
            if pending["answered"]:
                raise DiscordError(400, 40060, "Interaction has already been acknowledged.")
            if time.monotonic() - pending["at"] > INTERACTION_WINDOW_S:
                raise DiscordError(404, 10062, "Unknown interaction")
            kind, data = body["type"], body.get("data") or {}
            if kind not in (4, 5, 6, 7, 9):
                raise _form_error("type", "INTERACTION_RESPONSE_TYPE_INVALID",
                                  "Interaction response type is invalid for this interaction.")
            pending["answered"] = True
            ephemeral = bool((data.get("flags") or 0) & EPHEMERAL)
            entry = {"interaction_id": interaction_id, "type": kind, "data": data,
                     "ephemeral": ephemeral, "delay_s": round(time.monotonic() - pending["at"], 4)}
            if kind == 4 and not ephemeral:
                entry["message_id"] = self._create(
                    MESSAGES_ROUTE, pending["channel_id"], dict(BOT_USER),
                    {k: v for k, v in data.items() if k != "flags"}, via="interaction",
                    user_agent="", webhook_id=None, default_parse=["users"])["id"]
            elif kind == 7:
                record = self._bot_record(pending["channel_id"], pending["message_id"])
                if record is not None:
                    self._apply_edit(record, data, via="interaction")
            self.state["callbacks"].append(entry)
            self._save()

    # gateway pushes -----------------------------------------------------------------

    def _ensure_channel(self, channel_id: str, name: str) -> None:
        _snowflake("channel_id", channel_id)
        if not any(c["id"] == channel_id for c in self.state["channels"]):
            self.state["channels"].append({"id": channel_id, "name": name})
        for channels in self.state["tokens"].values():
            if channel_id not in channels:
                channels.append(channel_id)

    def push_message(self, body: dict) -> tuple[int, Any]:
        with self.lock:
            channel_id = str(body["channel_id"])
            self._ensure_channel(channel_id, body.get("channel_name", channel_id))
            guild_id = body.get("guild_id")
            author_id = str(body["author_id"])
            content = str(body.get("content", ""))
            known = {u["id"]: u for u in self.state["users"]}
            known[BOT_USER["id"]] = BOT_USER
            author = known.get(author_id, {"id": author_id, "username": f"user{author_id[-4:]}"})
            mentions = [_user(known[i], bot=i == BOT_USER["id"])
                        for i in dict.fromkeys(_MENTION.findall(content)) if i in known]
            self.state["counter"] += 1
            n = self.state["counter"]
            message_id = str(ID_BASE + n)
            # discord-gateway.md, Message Content Intent: without it, content is empty except
            # in DMs, in messages that mention the app, and in the app's own messages.
            mentioned = any(u["id"] == BOT_USER["id"] for u in mentions)
            if (guild_id and not mentioned and author_id != BOT_USER["id"]
                    and not self.gateway.intents() & INTENTS["MESSAGE_CONTENT"]):
                content = ""
            channel_type = body.get("channel_type", 0 if guild_id else 1)
            message: dict[str, Any] = {
                "type": 19 if body.get("reference") else 0, "content": content,
                "mentions": mentions, "mention_roles": [], "attachments": [], "embeds": [],
                "timestamp": (EPOCH + timedelta(seconds=n)).isoformat(),
                "edited_timestamp": None, "flags": 0, "components": [], "id": message_id,
                "channel_id": channel_id, "author": _user(author, bot=author_id == BOT_USER["id"]),
                "pinned": False, "mention_everyone": False, "tts": False,
                "channel_type": channel_type,
            }
            if guild_id:
                message["guild_id"] = str(guild_id)
                message["member"] = {"roles": [], "joined_at": EPOCH.isoformat(), "deaf": False,
                                     "mute": False, "flags": 0}
            reference = body.get("reference")
            if reference:
                if not self._known_message(str(reference)):
                    return 400, {"error": "unknown_reference"}
                ref_channel = next(m["channel_id"] for m in self.state["messages"]
                                   + self.state["received"] if m["id"] == str(reference))
                message["message_reference"] = {"type": 0, "channel_id": ref_channel,
                                                "message_id": str(reference)}
                if guild_id:
                    message["message_reference"]["guild_id"] = str(guild_id)
                ref = next((m.get("message") for m in self.state["messages"]
                            if m["id"] == str(reference)), None)
                if ref is not None:
                    message["referenced_message"] = ref
            self.state["received"].append({"id": message_id, "channel_id": channel_id,
                                           "author_id": author_id})
            self._save()
        self.gateway.dispatch("MESSAGE_CREATE", message)
        return 200, {"id": message_id}

    def push_thread(self, body: dict) -> tuple[int, Any]:
        with self.lock:
            thread_id, parent_id = str(body["id"]), str(body["parent_id"])
            self._ensure_channel(thread_id, body.get("name", "thread"))
            self._save()
        channel = {"id": thread_id, "type": 11, "guild_id": str(body.get("guild_id", "1")),
                   "parent_id": parent_id, "owner_id": str(body.get("owner_id", BOT_USER["id"])),
                   "name": body.get("name", "thread"), "newly_created": True,
                   "thread_metadata": {"archived": False, "auto_archive_duration": 1440,
                                       "archive_timestamp": EPOCH.isoformat(), "locked": False}}
        self.gateway.dispatch("THREAD_CREATE", channel)
        return 200, {"id": thread_id}

    def push_interaction(self, body: dict) -> tuple[int, Any]:
        with self.lock:
            channel_id, message_id = str(body["channel_id"]), str(body["message_id"])
            record = self._bot_record(channel_id, message_id)
            if record is None:
                return 404, {"error": "message_not_found"}
            custom_id = body.get("custom_id")
            buttons = [c for row in record["body"].get("components") or []
                       for c in row.get("components") or [] if c.get("type") == 2]
            if not any(b.get("custom_id") == custom_id for b in buttons):
                return 400, {"error": "button_not_found"}
            user_id = str(body["user_id"])
            known = {u["id"]: u for u in self.state["users"]}
            user = _user(known.get(user_id, {"id": user_id, "username": f"user{user_id[-4:]}"}))
            self.state["counter"] += 1
            interaction_id = str(ID_BASE + self.state["counter"])
            token = "aW50ZXJhY3Rpb246" + uuid.uuid4().hex
            guild_id = body.get("guild_id")
            interaction: dict[str, Any] = {
                "id": interaction_id, "application_id": BOT_USER["id"], "type": 3,
                "data": {"custom_id": custom_id, "component_type": 2},
                "channel_id": channel_id,
                "channel": {"id": channel_id, "type": 0 if guild_id else 1},
                "token": token, "version": 1, "message": record["message"],
                "app_permissions": "0", "locale": "en-US", "entitlements": [],
                "authorizing_integration_owners": {"0": str(guild_id)} if guild_id
                else {"1": user_id},
                "context": 0 if guild_id else 1, "attachment_size_limit": 10_485_760,
            }
            if guild_id:
                interaction["guild_id"] = str(guild_id)
                interaction["guild_locale"] = "en-US"
                interaction["member"] = {"user": user, "roles": [], "permissions": "0",
                                         "joined_at": EPOCH.isoformat(), "deaf": False,
                                         "mute": False, "flags": 0}
            else:
                interaction["user"] = user
            self.interactions[interaction_id] = {"token": token, "channel_id": channel_id,
                                                 "message_id": message_id,
                                                 "at": time.monotonic(), "answered": False}
            self._save()
        self.gateway.dispatch("INTERACTION_CREATE", interaction)
        return 200, {"id": interaction_id, "token": token}

    def gateway_control(self, verb: str, name: str, body: dict) -> tuple[int, Any]:
        try:
            if verb == "POST" and name == "message":
                return self.push_message(body)
            if verb == "POST" and name == "thread":
                return self.push_thread(body)
            if verb == "POST" and name == "interaction":
                return self.push_interaction(body)
        except DiscordError as exc:
            return exc.status, exc.body
        except KeyError as exc:
            return 400, {"error": f"missing field {exc}"}
        if verb == "POST" and name == "dispatch":
            event = self.gateway.dispatch(str(body["t"]), body.get("d"))
            return (200, {"s": event["s"]}) if event else (409, {"error": "no_session"})
        return self.gateway.control(verb, name, body)

    # control ----------------------------------------------------------------------

    def control(self, verb: str, name: str, body: dict) -> tuple[int, Any]:
        with self.lock:
            st = self.state
            if verb == "GET" and name == "messages":
                return 200, {"messages": copy.deepcopy(st["messages"])}
            if verb == "GET" and name in ("callbacks", "edits"):
                return 200, {name: copy.deepcopy(st[name])}
            if verb == "GET" and name == "state":
                return 200, {k: v for k, v in st.items() if k != "messages"}
            if verb != "POST":
                return 404, {"error": "unknown control route"}
            if name == "reset":
                self.state = _seed()
                self.interactions.clear()
                with self.gateway.lock:
                    self.gateway.reset()
            elif name == "application":
                st["application"]["flags"] = int(body["flags"])
            elif name == "ratelimit":
                st["ratelimit"] = {
                    "times": int(body.get("times", 1)),
                    "retry_after": float(body.get("retry_after", 1.0)),
                    "global": bool(body.get("global", False)),
                }
            elif name == "tokens":
                st["tokens"][body["token"]] = list(body.get("channels", []))
            elif name == "channels":
                st["channels"].append({"id": body["id"], "name": body["name"]})
            elif name == "webhooks":
                st["webhooks"].append({k: body[k] for k in ("id", "token", "channel_id", "name")})
            elif name == "users":
                st["users"].append({"id": body["id"], "username": body["username"]})
            else:
                return 404, {"error": "unknown control route"}
            self._save()
            return 200, {"ok": True}


def _user(user: dict, *, bot: bool = False) -> dict:
    out = {"id": user["id"], "username": user["username"], "avatar": None, "discriminator": "0",
           "public_flags": 0, "flags": 0, "global_name": None, "primary_guild": None}
    if bot:
        out["bot"] = True
    return out


def _components(rows: list | None) -> list:
    """Request components → the message's components (each gets a numeric `id`)."""
    out, n = [], 0
    for row in rows or []:
        n += 1
        row_id = n
        children = []
        for child in row.get("components") or []:
            n += 1
            children.append(dict({k: v for k, v in child.items() if v is not None}, id=n))
        out.append({"type": row.get("type", 1), "id": row_id, "components": children})
    return out


def _ok_headers() -> dict[str, str]:
    return {"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "4",
            "X-RateLimit-Reset": f"{time.time() + 1:.3f}", "X-RateLimit-Reset-After": "1.000",
            "X-RateLimit-Bucket": "mockbucket"}


def _snowflake(field: str, value: str) -> None:
    if not _SNOWFLAKE.match(value):
        raise _form_error(field, "NUMBER_TYPE_COERCE", f'Value "{value[:40]}" is not snowflake.')


def _boolean(field: str, value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise _form_error(field, "BOOLEAN_TYPE_CONVERT", "Must be either true or false.")


@functools.cache
def _spec_routes() -> tuple[tuple[tuple[str, ...], frozenset[str]], ...]:
    return tuple((tuple(path.strip("/").split("/")), frozenset(k.upper() for k in item))
                 for path, item in load_spec()["paths"].items())


def _in_spec(parts: list[str], verb: str) -> bool:
    """Whether the spec has this operation (path segments after `/api[/vN]`)."""
    for template, verbs in _spec_routes():
        if verb in verbs and len(template) == len(parts) and all(
                t == p or (t.startswith("{") and t.endswith("}"))
                for t, p in zip(template, parts, strict=True)):
            return True
    return False


def _route(path: str) -> list[str] | None:
    """`/api[/vN]/...` → the path segments after the prefix."""
    parts = path.strip("/").split("/")
    if not parts or parts[0] != "api":
        return None
    parts = parts[1:]
    if parts and re.fullmatch(r"v\d+", parts[0]):
        parts = parts[1:]
    return parts


def make_handler(mock: DiscordMock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _json(self, status: int, data: Any, headers: dict | None = None) -> None:
            raw = b"" if data is None else json.dumps(data).encode()
            self.send_response(status)
            if data is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_PATCH(self):  # noqa: N802
            self._dispatch("PATCH")

        def _dispatch(self, verb: str) -> None:
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            if (verb == "GET" and url.path in ("", "/")
                    and self.headers.get("Upgrade", "").lower() == "websocket"):
                return self._gateway(query)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if url.path.startswith("/_mock/"):
                data = json.loads(raw) if raw else {}
                status, out = mock.control(verb, url.path[len("/_mock/"):], data)
                return self._json(status, out)
            if url.path == "/_control/gateway" or url.path.startswith("/_control/gateway/"):
                data = json.loads(raw) if raw else {}
                name = url.path[len("/_control/gateway"):].strip("/")
                status, out = mock.gateway_control(verb, name, data)
                return self._json(status, out)
            parts = _route(url.path) or []
            shape = tuple(p if p in ("webhooks", "channels", "messages", "gateway", "bot",
                                     "applications", "@me", "interactions", "callback")
                          else "{}" for p in parts)
            allowed = {
                ("webhooks", "{}", "{}"): "POST",
                ("channels", "{}", "messages"): "POST",
                ("channels", "{}", "messages", "{}"): "PATCH",
                ("gateway", "bot"): "GET",
                ("applications", "@me"): "GET",
                ("interactions", "{}", "{}", "callback"): "POST",
            }.get(shape)
            if allowed is None or verb != allowed:
                if _in_spec(parts, verb):  # a real operation this mock does not serve
                    return self._json(501, {"message": "Not served by the mock", "code": 0})
                if allowed is None:
                    return self._json(404, {"message": "404: Not Found", "code": 0})
                return self._json(405, {"message": "405: Method Not Allowed", "code": 0})
            limited = mock.take_rate_limit()
            if limited is not None:
                return self._json(429, limited[0], limited[1])
            agent = self.headers.get("User-Agent", "")
            auth = self.headers.get("Authorization", "")
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
            ok = _ok_headers()
            try:
                if shape[0] == "gateway":
                    return self._json(200, mock.gateway_bot(auth, host), ok)
                if shape[0] == "applications":
                    return self._json(200, mock.application(auth), ok)
                body = _body(raw, self.headers.get("Content-Type", ""))
                if shape[0] == "webhooks":
                    out = mock.execute_webhook(parts[1], parts[2], query, body, agent)
                    return self._json(200 if out is not None else 204, out, ok)
                if shape[0] == "interactions":
                    mock.callback(parts[1], parts[2], body)
                    return self._json(204, None, ok)
                if len(parts) == 4:
                    return self._json(200, mock.edit_message(parts[1], parts[3], auth, body), ok)
                return self._json(200, mock.create_message(parts[1], auth, body, agent), ok)
            except DiscordError as exc:
                return self._json(exc.status, exc.body)

        def _gateway(self, query: dict[str, str]) -> None:
            self.close_connection = True
            peer = WebSocketPeer(self.connection)
            if not peer.handshake(self.requestline, self.headers):
                return
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
            mock.gateway.serve_connection(peer, host, mock.gateway.check_query(query))

    return Handler


def _body(raw: bytes, content_type: str) -> dict:
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "application/json":
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DiscordError(400, 50109, "The request body contains invalid JSON.") from None
    elif media == "application/x-www-form-urlencoded":
        body = {k: v[-1] for k, v in parse_qs(raw.decode(errors="replace")).items()}
    else:
        # Discord: a missing or unsupported Content-Type is an invalid form body.
        raise DiscordError(400, 50035, "Invalid Form Body")
    if not isinstance(body, dict):
        raise _form_error(None, "MODEL_TYPE_CONVERT", "Only dictionaries may be used in a DictType")
    return body


def serve(port: int, state_dir: Path | None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(DiscordMock(state_dir)))
    server.daemon_threads = True
    return server
