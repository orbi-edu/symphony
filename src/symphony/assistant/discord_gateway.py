"""Discord transport: the Gateway in, the HTTP API out (`scripts/mocks/specs/discord-*.md`).

- Connect: `GET /gateway/bot` (cached), then the WSS URL with `?v=10&encoding=json`.
- Handshake: Hello, the first heartbeat after `heartbeat_interval * random()`, then one every
  interval; Identify with `GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT`.
- A heartbeat that gets no ACK before the next one means a zombie connection: close with
  4000 (not 1000/1001, which end the session) and resume.
- Resume on `resume_gateway_url` with `session_id` and `seq` after op 7, op 9 `d: true`, or a
  close code that allows it. Op 9 `d: false`, 4007 and 4009 start a new session. The close
  codes that forbid a reconnect (4004, 4010-4014) stop the transport.
- Failed connections back off exponentially with full jitter. Identify waits 5 s between
  attempts (`max_concurrency` 1) and honors `session_start_limit`. Sends stay under 120 per
  60 s per connection.
- Events: `MESSAGE_CREATE` that mentions the bot, in a DM, in a thread the bot started, or
  replying to a bot message; `INTERACTION_CREATE` for buttons, answered through
  `POST /interactions/{id}/{token}/callback`. Bots and webhooks are ignored.
- HTTP calls honor the per-route and global rate limits (`X-RateLimit-*`, 429 `retry_after`).
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx
import structlog
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from ..endpoints import discord_api_url
from ..notify import DISCORD_USER_AGENT
from . import Action, Button, File, HistoryMessage, Inbound, Ref

log = structlog.get_logger()

VERSION = 10
GUILDS, GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT = 1 << 0, 1 << 9, 1 << 12, 1 << 15
INTENTS = GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
EPHEMERAL = 1 << 6
FATAL_CLOSE = frozenset({4004, 4010, 4011, 4012, 4013, 4014})  # "Reconnect: false"
NEW_SESSION_CLOSE = frozenset({4007, 4009})
SEND_LIMIT = (110, 60.0)  # under the documented 120 per 60 s
IDENTIFY_GAP_S = 5.0
INVALID_SESSION_WAIT_S = (1.0, 5.0)  # a random wait before re-identifying after op 9
MAX_RETRY_AFTER_S = 60.0
HTTP_TIMEOUT_S = 15.0
THREAD_TYPES = (10, 11, 12)  # announcement, public and private threads
BUTTON_STYLES = {"primary": 1, "": 2, "danger": 4}
_REMEMBER = 5000


class DiscordError(RuntimeError):
    def __init__(self, route: str, status: int, code: int | None):
        super().__init__(f"{route}: HTTP {status} code {code}")
        self.route = route
        self.status = status
        self.code = code


class FatalGatewayError(RuntimeError):
    """A close code the docs say not to reconnect after."""


def backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * 2 ** attempt))


class _Reconnect(Exception):
    def __init__(self, resume: bool):
        self.resume = resume


class _RateLimits:
    """Per-route buckets and the global limit, from the `X-RateLimit-*` headers."""

    def __init__(self, stop: threading.Event):
        self.stop = stop
        self.lock = threading.Lock()
        self.global_until = 0.0
        self.until: dict[str, float] = {}

    def wait(self, route: str) -> None:
        while True:
            with self.lock:
                delay = max(self.global_until, self.until.get(route, 0.0)) - time.monotonic()
            if delay <= 0 or self.stop.wait(delay):
                return

    def update(self, route: str, response: httpx.Response) -> float | None:
        """Record the headers; the wait before a retry when the answer is a 429."""
        now = time.monotonic()
        headers = response.headers
        with self.lock:
            if headers.get("X-RateLimit-Remaining") == "0":
                self.until[route] = now + _float(headers.get("X-RateLimit-Reset-After"), 1.0)
            if response.status_code != 429:
                return None
            body = _json(response)
            wait = _float(body.get("retry_after", headers.get("Retry-After")), 1.0)
            if body.get("global") or headers.get("X-RateLimit-Global") == "true":
                self.global_until = now + wait
            else:
                self.until[route] = now + wait
            return wait


class DiscordGatewayTransport:
    name = "discord"
    message_limit = 2000

    def __init__(self, token: str, *, api_url: str | None = None,
                 backoff_base: float = 1.0, backoff_cap: float = 60.0):
        self._token = token
        self._api_url = api_url
        self._backoff = (backoff_base, backoff_cap)
        self._http = httpx.Client(timeout=HTTP_TIMEOUT_S, headers={
            "Authorization": f"Bot {token}", "User-Agent": DISCORD_USER_AGENT})
        self._stop = threading.Event()
        self._limits = _RateLimits(self._stop)
        self._thread: threading.Thread | None = None
        self._ws: ClientConnection | None = None
        self._ws_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._sent: deque[float] = deque()
        self._on_message: Callable[[Inbound], None] = lambda _m: None
        self._on_action: Callable[[Action], None] = lambda _a: None
        self._gateway_url = ""
        self._session_id = ""
        self._resume_url = ""
        self._seq: int | None = None
        self._last_identify = 0.0
        self._acked = True
        self._threads: dict[str, str] = {}  # thread id → parent channel id
        self._bot_threads: set[str] = set()
        self._roots: OrderedDict[str, str] = OrderedDict()  # message id → conversation id
        self._roots_lock = threading.Lock()
        self.bot_id = ""
        self.connected = False
        self.fatal_code: int | None = None
        self._ready = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DiscordGatewayTransport | None:
        env = os.environ if env is None else env
        token = env.get("DISCORD_BOT_TOKEN", "").strip()
        return cls(token) if token else None

    # lifecycle ------------------------------------------------------------------------

    def start(self, on_message: Callable[[Inbound], None],
              on_action: Callable[[Action], None]) -> None:
        self._on_message, self._on_action = on_message, on_action
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discord-gateway", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            ws.close(1000)  # a normal close ends the session; the process is going away
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._http.close()
        self.connected = False

    def _run(self) -> None:
        attempt = failed_resumes = 0
        while not self._stop.is_set():
            resume = bool(self._session_id and self._resume_url)
            self._ready = False
            try:
                if not resume:
                    self._before_identify()
                base = self._resume_url if resume else self._gateway_url
                url = f"{base.rstrip('/')}/?v={VERSION}&encoding=json"
                with connect(url, open_timeout=10, close_timeout=2, ping_interval=None,
                             max_size=8 << 20, compression=None) as ws:
                    with self._ws_lock:
                        self._ws = ws
                    self._session(ws, resume)
            except _Reconnect as exc:
                self._ready = True  # Discord asked for it: no backoff
                if not exc.resume:
                    self._forget_session()
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                log.info("discord_gateway_closed", code=code)
                if code in FATAL_CLOSE:
                    self.fatal_code = code
                    log.error("discord_gateway_fatal", code=code)
                    break
                if code in NEW_SESSION_CLOSE:
                    self._forget_session()
            except DiscordError as exc:
                log.warning("discord_gateway_api_error", status=exc.status, code=exc.code)
            except (OSError, TimeoutError, WebSocketException, httpx.HTTPError, ValueError,
                    KeyError) as exc:
                log.warning("discord_gateway_disconnected", error=type(exc).__name__)
            finally:
                with self._ws_lock:
                    self._ws = None
                self.connected = False
            if self._stop.is_set():
                break
            if self._ready:
                attempt = failed_resumes = 0
                continue  # the session worked: reconnect (and resume) at once
            if resume:
                failed_resumes += 1
                if failed_resumes >= 3:  # "If the reconnect fails": start a new session
                    self._forget_session()
                    failed_resumes = 0
            delay = backoff(attempt, *self._backoff)
            attempt += 1
            log.info("discord_gateway_reconnecting", delay_s=round(delay, 2), attempt=attempt)
            self._stop.wait(delay)

    def _before_identify(self) -> None:
        """Fetch the Gateway URL and wait for a free session start."""
        info = self._request("GET", "/gateway/bot", "GET /gateway/bot")
        self._gateway_url = info["url"]
        limit = info.get("session_start_limit") or {}
        if limit.get("remaining", 1) <= 0:
            wait = limit.get("reset_after", 0) / 1000
            log.warning("discord_session_start_limit", wait_s=wait)
            self._stop.wait(wait)
        gap = self._last_identify + IDENTIFY_GAP_S - time.monotonic()
        if gap > 0:
            self._stop.wait(gap)

    def _forget_session(self) -> None:
        self._session_id, self._resume_url, self._seq = "", "", None

    # gateway session ------------------------------------------------------------------

    def _session(self, ws: ClientConnection, resume: bool) -> None:
        """Run one connection until it ends."""
        hello = json.loads(ws.recv(timeout=30))
        if hello.get("op") != 10:
            raise ValueError("no Hello")
        interval = hello["d"]["heartbeat_interval"] / 1000
        self._sent.clear()
        self._acked = True
        beat_stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(ws, interval, beat_stop),
                                name="discord-heartbeat", daemon=True)
        beat.start()
        try:
            if resume:
                self._send(ws, {"op": 6, "d": {"token": self._token,
                                               "session_id": self._session_id,
                                               "seq": self._seq}})
            else:
                self._identify(ws)
            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                payload = json.loads(raw)
                op = payload.get("op")
                if op == 0:
                    if payload.get("s") is not None:
                        self._seq = payload["s"]
                    self._dispatch(payload.get("t"), payload.get("d"))
                elif op == 1:
                    self._send_heartbeat(ws)
                elif op == 11:
                    self._acked = True
                elif op == 7:
                    log.info("discord_gateway_reconnect_requested")
                    ws.close(4000, "reconnect")
                    raise _Reconnect(resume=True)
                elif op == 9:
                    resumable = bool(payload.get("d"))
                    log.info("discord_gateway_invalid_session", resumable=resumable)
                    ws.close(4000, "invalid session")
                    if not resumable:
                        self._stop.wait(random.uniform(*INVALID_SESSION_WAIT_S))
                    raise _Reconnect(resume=resumable)
            return
        finally:
            beat_stop.set()

    def _identify(self, ws: ClientConnection) -> None:
        self._last_identify = time.monotonic()
        self._send(ws, {"op": 2, "d": {
            "token": self._token,
            "intents": INTENTS,
            "properties": {"os": "linux", "browser": "symphony", "device": "symphony"},
        }})

    def _heartbeat(self, ws: ClientConnection, interval: float, stop: threading.Event) -> None:
        if stop.wait(interval * random.random()):
            return
        while not stop.is_set():
            if not self._acked:
                # No ACK since the last heartbeat: a zombie connection. Close it with a
                # non-1000 code so the session can be resumed.
                log.warning("discord_gateway_zombie")
                ws.close(4000, "heartbeat ACK missing")
                return
            try:
                self._send_heartbeat(ws)
            except (ConnectionClosed, OSError):
                return
            if stop.wait(interval):
                return

    def _send_heartbeat(self, ws: ClientConnection) -> None:
        self._acked = False
        self._send(ws, {"op": 1, "d": self._seq})

    def _send(self, ws: ClientConnection, payload: dict) -> None:
        with self._send_lock:
            now = time.monotonic()
            while self._sent and self._sent[0] < now - SEND_LIMIT[1]:
                self._sent.popleft()
            if len(self._sent) >= SEND_LIMIT[0]:
                self._stop.wait(self._sent[0] + SEND_LIMIT[1] - now)
            self._sent.append(time.monotonic())
            ws.send(json.dumps(payload))

    # dispatches -----------------------------------------------------------------------

    def _dispatch(self, name: str | None, data: Any) -> None:
        if name == "READY":
            self._session_id = data["session_id"]
            self._resume_url = data["resume_gateway_url"]
            self.bot_id = data["user"]["id"]
            self.connected = self._ready = True
            log.info("discord_gateway_ready")
            return
        if name == "RESUMED":
            self.connected = self._ready = True
            log.info("discord_gateway_resumed")
            return
        try:
            if name == "MESSAGE_CREATE":
                self._message(data)
            elif name == "INTERACTION_CREATE":
                self._interaction(data)
            elif name in ("THREAD_CREATE", "THREAD_UPDATE"):
                self._thread_seen(data)
            elif name == "THREAD_LIST_SYNC":
                for thread in data.get("threads") or []:
                    self._thread_seen(thread)
            elif name == "GUILD_CREATE":
                for thread in data.get("threads") or []:
                    self._thread_seen(thread)
        except Exception as exc:  # a bad payload or engine error must not drop the session
            log.exception("discord_gateway_handler_failed", event=name, error=type(exc).__name__)

    def _thread_seen(self, channel: dict) -> None:
        if channel.get("type") in THREAD_TYPES and channel.get("id"):
            self._threads[channel["id"]] = channel.get("parent_id") or ""
            if channel.get("owner_id") == self.bot_id:
                self._bot_threads.add(channel["id"])

    def _message(self, data: dict) -> None:
        author = data.get("author") or {}
        if author.get("bot") or data.get("webhook_id") or author.get("id") == self.bot_id:
            return
        channel, message_id = data["channel_id"], data["id"]
        is_dm = not data.get("guild_id")
        mentioned = any(u.get("id") == self.bot_id for u in data.get("mentions") or [])
        in_thread = channel in self._threads or data.get("channel_type") in THREAD_TYPES
        reference = (data.get("message_reference") or {}).get("message_id") or ""
        replied = (data.get("referenced_message") or {}).get("author") or {}
        reply_to_bot = bool(reference) and (self._root(reference) is not None
                                            or replied.get("id") == self.bot_id)
        if not (is_dm or mentioned or reply_to_bot or channel in self._bot_threads):
            return
        if is_dm or in_thread:
            thread = channel
        elif reply_to_bot:
            thread = self._root(reference) or reference
        else:
            thread = message_id
        self._remember(message_id, thread)
        files = tuple(File(name=a.get("filename") or "file", url=a.get("url") or "",
                           mimetype=a.get("content_type") or "",
                           size=int(a.get("size") or 0))
                      for a in data.get("attachments") or [] if isinstance(a, dict))
        self._on_message(Inbound(
            transport=self.name, channel=channel, thread=thread, message_id=message_id,
            user_id=author.get("id") or "", text=self._strip_mention(data.get("content") or ""),
            files=files, is_dm=is_dm, parent=self._threads.get(channel, ""),
        ))

    def _interaction(self, data: dict) -> None:
        if data.get("type") != 3 or (data.get("data") or {}).get("component_type") != 2:
            return
        user = (data.get("member") or {}).get("user") or data.get("user") or {}
        message = data.get("message") or {}
        channel = data.get("channel_id") or message.get("channel_id") or ""
        message_id = message.get("id") or ""
        if channel in self._threads or not data.get("guild_id"):
            thread = channel
        else:
            reference = (message.get("message_reference") or {}).get("message_id")
            thread = self._root(message_id) or reference or message_id
        custom_id = data["data"].get("custom_id") or ""
        self._on_action(Action(
            transport=self.name, channel=channel, thread=thread, message_id=message_id,
            user_id=user.get("id") or "", action_id=custom_id, value=custom_id,
            respond=(data["id"], data["token"]),
        ))

    def _strip_mention(self, text: str) -> str:
        if self.bot_id:
            text = re.sub(rf"<@!?{re.escape(self.bot_id)}>", "", text)
        return text.strip()

    def _remember(self, message_id: str, thread: str) -> None:
        with self._roots_lock:
            self._roots[message_id] = thread
            while len(self._roots) > _REMEMBER:
                self._roots.popitem(last=False)

    def _root(self, message_id: str) -> str | None:
        with self._roots_lock:
            return self._roots.get(message_id)


    # downloads --------------------------------------------------------------------

    _DOWNLOAD_TIMEOUT_S = 30.0
    _MAX_FILE_BYTES = 5 * 1024 * 1024

    def download(self, file: File) -> bytes:
        """Download a Discord CDN attachment (no auth needed)."""
        try:
            with self._http.stream(
                "GET", file.url, timeout=self._DOWNLOAD_TIMEOUT_S,
                follow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    raise DiscordError("files.download", resp.status_code, None)
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(8192):
                    total += len(chunk)
                    if total > self._MAX_FILE_BYTES:
                        raise DiscordError("files.download", 413, None)
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TimeoutException as exc:
            raise DiscordError("files.download", 408, None) from exc
    # outbound -------------------------------------------------------------------------

    def post(self, channel: str, thread: str, text: str,
             buttons: Sequence[Button] = ()) -> Ref:
        body: dict[str, Any] = {"content": text, "allowed_mentions": {"parse": []}}
        if buttons:
            body["components"] = _components(buttons)
        if thread and thread != channel:
            # A reply chain in a channel: answer the conversation's root message.
            body["message_reference"] = {"message_id": thread, "channel_id": channel,
                                         "fail_if_not_exists": False}
        out = self._request("POST", f"/channels/{channel}/messages",
                            f"POST /channels/{channel}/messages", body)
        self._remember(out["id"], thread or channel)
        return Ref(channel=channel, message_id=out["id"])

    def update(self, ref: Ref, text: str, buttons: Sequence[Button] = ()) -> None:
        self._request("PATCH", f"/channels/{ref.channel}/messages/{ref.message_id}",
                      f"PATCH /channels/{ref.channel}/messages/:id",
                      {"content": text, "components": _components(buttons),
                       "allowed_mentions": {"parse": []}})

    def ephemeral(self, channel: str, thread: str, user_id: str, text: str) -> None:
        # Discord has no ephemeral message outside an interaction: reply in the thread.
        self.post(channel, thread, text)

    def ack_action(self, action: Action, text: str | None = None) -> None:
        interaction_id, token = action.respond  # type: ignore[misc]
        if text:
            body: dict[str, Any] = {"type": 4, "data": {
                "content": text[:2000], "flags": EPHEMERAL, "allowed_mentions": {"parse": []}}}
        else:
            body = {"type": 6}
        self._request("POST", f"/interactions/{interaction_id}/{token}/callback",
                      f"POST /interactions/{interaction_id}/callback", body, auth=False)

    def history(self, channel: str, thread: str, limit: int) -> list[HistoryMessage]:
        return []

    # HTTP -----------------------------------------------------------------------------

    def _request(self, method: str, path: str, route: str, body: dict | None = None, *,
                 auth: bool = True) -> dict:
        url = f"{self._api_url or discord_api_url()}{path}"
        for _ in range(5):
            self._limits.wait(route)
            request = self._http.build_request(method, url, json=body)
            if not auth:  # interaction callbacks are authorized by their token
                del request.headers["Authorization"]
            response = self._http.send(request)
            wait = self._limits.update(route, response)
            if wait is not None:
                if wait > MAX_RETRY_AFTER_S:
                    raise DiscordError(route, 429, None)
                log.info("discord_rate_limited", route=route, retry_after_s=wait)
                continue
            if response.status_code >= 400:
                raise DiscordError(route, response.status_code, _json(response).get("code"))
            return _json(response) if response.content else {}
        raise DiscordError(route, 429, None)


def _components(buttons: Sequence[Button]) -> list[dict]:
    """Buttons in action rows of at most five."""
    rows = []
    for start in range(0, len(buttons), 5):
        rows.append({"type": 1, "components": [
            {"type": 2, "style": BUTTON_STYLES.get(b.style, 2), "label": b.label[:80],
             "custom_id": b.id[:100]} for b in buttons[start:start + 5]]})
    return rows


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _float(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default
