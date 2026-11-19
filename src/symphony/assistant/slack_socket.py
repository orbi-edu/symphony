"""Slack transport: Socket Mode in, Web API out (`scripts/mocks/specs/slack-*.md`).

- Connect: `apps.connections.open` with the app-level token (`xapp-…`), then the returned
  WSS URL. Slack sends `hello`; the URL is single use, so every reconnect opens a new one.
- Every envelope is acked by `envelope_id` before it is handled, well inside Slack's 3 s.
- `disconnect` (a refresh, a warning or `link_disabled`) and a dropped socket reconnect,
  with exponential backoff and full jitter after failures.
- Events: `app_mention`, `message` in a DM (`channel_type: im`), and `message` replies in a
  thread the bot posted in. The bot's own messages and other bots' messages are ignored.
  Interactive: `block_actions` button clicks.
- Web API calls use the bot token (`xoxb-…`) and wait out `429 Retry-After`.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from ..endpoints import slack_api_url
from . import Action, Button, File, HistoryMessage, Inbound, Ref, split_text

log = structlog.get_logger()

SECTION_LIMIT = 3000  # characters in one section block
MAX_RETRY_AFTER_S = 60.0
HTTP_TIMEOUT_S = 15.0
# Message subtypes that are still a person writing.
_PERSON_SUBTYPES = (None, "file_share", "thread_broadcast")
_REMEMBER = 5000  # threads and message ids kept for routing and dedup


class SlackError(RuntimeError):
    """A Web API call failed; `error` is Slack's error code."""

    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


def backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * 2 ** attempt))


@dataclass(frozen=True)
class _Click:
    """`Action.respond` for Slack: whether the clicked message is a thread reply."""

    in_thread: bool


class _Recent:
    """A bounded insertion-ordered set."""

    def __init__(self, size: int = _REMEMBER):
        self.size = size
        self.items: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.lock = threading.Lock()

    def add(self, key: tuple[str, str]) -> bool:
        """Add `key`; False when it was already there."""
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return False
            self.items[key] = None
            while len(self.items) > self.size:
                self.items.popitem(last=False)
            return True

    def __contains__(self, key: tuple[str, str]) -> bool:
        with self.lock:
            return key in self.items


class SlackSocketTransport:
    name = "slack"
    # The engine edits the first part of an answer in place (`chat.update`), whose `text`
    # cannot exceed 4,000 characters (`msg_too_long`); `chat.postMessage` allows 40,000.
    # One limit serves both calls, so it is the smaller one.
    message_limit = 4_000

    def __init__(self, app_token: str, bot_token: str, *, api_url: str | None = None,
                 backoff_base: float = 1.0, backoff_cap: float = 60.0):
        self._app_token = app_token
        self._bot_token = bot_token
        self._api_url = api_url
        self._backoff = (backoff_base, backoff_cap)
        self._http = httpx.Client(timeout=HTTP_TIMEOUT_S)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: ClientConnection | None = None
        self._ws_lock = threading.Lock()
        self._on_message: Callable[[Inbound], None] = lambda _m: None
        self._on_action: Callable[[Action], None] = lambda _a: None
        self._threads = _Recent()  # (channel, thread_ts) the bot posted in
        self._seen = _Recent()  # (channel, ts) already handed to the engine
        self.bot_user_id = ""
        self.bot_id = ""
        self._hello = False
        self.connected = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SlackSocketTransport | None:
        """Both tokens are needed: the app token for the socket, the bot token for replies."""
        env = os.environ if env is None else env
        app, bot = env.get("SLACK_APP_TOKEN", "").strip(), env.get("SLACK_BOT_TOKEN", "").strip()
        return cls(app, bot) if app and bot else None

    # lifecycle ------------------------------------------------------------------------

    def start(self, on_message: Callable[[Inbound], None],
              on_action: Callable[[Action], None]) -> None:
        self._on_message, self._on_action = on_message, on_action
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="slack-socket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            ws.close()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._http.close()
        self.connected = False

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            refresh = self._hello = False
            try:
                if not self.bot_user_id:
                    auth = self._call("auth.test", {})
                    self.bot_user_id, self.bot_id = auth.get("user_id", ""), auth.get("bot_id", "")
                url = self._call("apps.connections.open", {}, token=self._app_token)["url"]
                with connect(url, open_timeout=10, close_timeout=2, ping_interval=30,
                             ping_timeout=20, max_size=8 << 20) as ws:
                    with self._ws_lock:
                        self._ws = ws
                    refresh = self._read(ws)
                    if self._hello:
                        attempt = 0  # the connection worked; start the backoff over
            except SlackError as exc:
                log.warning("slack_socket_api_error", method=exc.method, error=exc.error)
            except (OSError, TimeoutError, WebSocketException, httpx.HTTPError, ValueError) as exc:
                log.warning("slack_socket_disconnected", error=type(exc).__name__)
            finally:
                with self._ws_lock:
                    self._ws = None
                self.connected = False
            if self._stop.is_set():
                break
            if refresh:
                continue  # Slack asked for a new connection: open one at once
            delay = backoff(attempt, *self._backoff)
            attempt += 1
            log.info("slack_socket_reconnecting", delay_s=round(delay, 2), attempt=attempt)
            self._stop.wait(delay)

    def _read(self, ws: ClientConnection) -> bool:
        """Handle frames until the socket ends. True when Slack sent `disconnect`."""
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            except ConnectionClosed:
                return False
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            kind = frame.get("type")
            if frame.get("envelope_id"):
                # Ack first: Slack retries an envelope that is not acked within 3 s.
                ws.send(json.dumps({"envelope_id": frame["envelope_id"]}))
            if kind == "hello":
                self.connected = self._hello = True
                log.info("slack_socket_connected",
                         connections=frame.get("num_connections"))
            elif kind == "disconnect":
                log.info("slack_socket_disconnect", reason=frame.get("reason"))
                ws.close()
                return frame.get("reason") != "link_disabled"
            elif kind == "events_api":
                self._guard(self._on_event, frame.get("payload") or {})
            elif kind == "interactive":
                self._guard(self._on_interactive, frame.get("payload") or {})
        return False

    def _guard(self, handler: Callable[[dict], None], payload: dict) -> None:
        try:
            handler(payload)
        except Exception as exc:  # a bad payload or engine error must not drop the socket
            log.exception("slack_socket_handler_failed", error=type(exc).__name__)

    # inbound --------------------------------------------------------------------------

    def _on_event(self, payload: dict) -> None:
        event = payload.get("event") or {}
        kind = event.get("type")
        if kind not in ("app_mention", "message"):
            return
        user = event.get("user") or ""
        if event.get("bot_id") or not user or user == self.bot_user_id:
            return
        if event.get("subtype") not in _PERSON_SUBTYPES:
            return
        channel, ts = event.get("channel") or "", event.get("ts") or ""
        thread_ts = event.get("thread_ts") or ""
        is_dm = event.get("channel_type") == "im"
        if kind == "message" and not is_dm and (
                not thread_ts or (channel, thread_ts) not in self._threads):
            return  # a channel message: only replies in threads the bot is in
        if not channel or not ts or not self._seen.add((channel, ts)):
            return  # `app_mention` and `message` both arrive for a mention in a thread
        thread = thread_ts or ts
        self._threads.add((channel, thread))
        files = tuple(File(name=f.get("name") or f.get("title") or "file",
                           url=f.get("url_private") or "",
                           mimetype=f.get("mimetype") or "",
                           size=int(f.get("size") or 0))
                      for f in event.get("files") or [] if isinstance(f, dict))
        self._on_message(Inbound(
            transport=self.name, channel=channel, thread=thread, message_id=ts, user_id=user,
            text=self._strip_mention(event.get("text") or ""), files=files, is_dm=is_dm,
        ))

    def _on_interactive(self, payload: dict) -> None:
        if payload.get("type") != "block_actions":
            return
        user = (payload.get("user") or {}).get("id") or ""
        container = payload.get("container") or {}
        message = payload.get("message") or {}
        channel = (payload.get("channel") or {}).get("id") or container.get("channel_id") or ""
        message_ts = container.get("message_ts") or message.get("ts") or ""
        thread = message.get("thread_ts") or message_ts
        # Where the clicked message lives: in a thread, or at the top of the channel (a
        # notification). A note answering the click goes to the same place.
        in_thread = bool(message.get("thread_ts"))
        for action in payload.get("actions") or []:
            self._on_action(Action(
                transport=self.name, channel=channel, thread=thread, message_id=message_ts,
                user_id=user, action_id=action.get("action_id") or "",
                value=action.get("value") or "", respond=_Click(in_thread),
            ))

    def _strip_mention(self, text: str) -> str:
        if self.bot_user_id:
            text = re.sub(rf"<@{re.escape(self.bot_user_id)}(\|[^>]*)?>", "", text)
        return text.strip()

    # downloads --------------------------------------------------------------------

    _SLACK_HOSTS = frozenset({"files.slack.com", "files-pri.slack.com"})
    _DOWNLOAD_TIMEOUT_S = 30.0
    _MAX_FILE_BYTES = 5 * 1024 * 1024

    def download(self, file: File) -> bytes:
        """Download a Slack ``url_private`` file using the bot token.

        Follows redirects only to Slack hosts.  Raises on auth failure (missing
        ``files:read`` scope), timeout, oversize responses and non-Slack redirects.
        """
        url = file.url
        from urllib.parse import urlparse

        # The bot token goes only to Slack (its file hosts, or the configured API host — a
        # test double in tests): a file url from anywhere else is not fetched.
        allowed = self._SLACK_HOSTS | {urlparse(self._api_url or slack_api_url()).hostname or ""}
        if (urlparse(url).hostname or "") not in allowed:
            raise SlackError("files.download", "not_a_slack_url")
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        try:
            with self._http.stream(
                "GET", url, headers=headers, timeout=self._DOWNLOAD_TIMEOUT_S,
                follow_redirects=False,
            ) as resp:
                # Follow redirects only to Slack hosts.
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    host = urlparse(location).hostname or ""
                    if host not in allowed:
                        raise SlackError("files.download", "redirect_to_non_slack_host")
                    resp.close()
                    return self._download_final(location, headers)
                if resp.status_code == 403 or resp.status_code == 401:
                    raise SlackError("files.download", "missing_scope")
                if resp.status_code != 200:
                    raise SlackError("files.download", f"http_{resp.status_code}")
                return self._read_body(resp)
        except httpx.TimeoutException as exc:
            raise SlackError("files.download", "timeout") from exc

    def _download_final(self, url: str, headers: dict) -> bytes:
        with self._http.stream(
            "GET", url, headers=headers, timeout=self._DOWNLOAD_TIMEOUT_S,
            follow_redirects=False,
        ) as resp:
            if resp.status_code != 200:
                raise SlackError("files.download", f"http_{resp.status_code}")
            return self._read_body(resp)

    def _read_body(self, resp: httpx.Response) -> bytes:
        # Without `files:read`, Slack answers 200 with its HTML sign-in page instead of the file.
        if resp.headers.get("content-type", "").split(";")[0].strip().lower() == "text/html":
            raise SlackError("files.download", "missing_scope")
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes(8192):
            total += len(chunk)
            if total > self._MAX_FILE_BYTES:
                raise SlackError("files.download", "file_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    # outbound -------------------------------------------------------------------------

    def post(self, channel: str, thread: str, text: str,
             buttons: Sequence[Button] = ()) -> Ref:
        body: dict[str, Any] = {"channel": channel, "text": text,
                                "blocks": _blocks(text, buttons)}
        if thread:
            body["thread_ts"] = thread
        out = self._call("chat.postMessage", body)
        self._threads.add((channel, thread or out["ts"]))
        return Ref(channel=out.get("channel") or channel, message_id=out["ts"])

    def update(self, ref: Ref, text: str, buttons: Sequence[Button] = ()) -> None:
        # Blocks always go along: an empty list of buttons drops the actions block.
        self._call("chat.update", {"channel": ref.channel, "ts": ref.message_id, "text": text,
                                   "blocks": _blocks(text, buttons)})

    def ephemeral(self, channel: str, thread: str, user_id: str, text: str) -> None:
        body = {"channel": channel, "user": user_id, "text": text}
        if thread:
            body["thread_ts"] = thread
        self._call("chat.postEphemeral", body)

    def ack_action(self, action: Action, text: str | None = None) -> None:
        # The envelope is acked on receipt; only a note needs a call. A note in the thread
        # of a top-level message is hidden until someone opens that thread: a click on a
        # channel message gets its note in the channel.
        if text:
            in_thread = action.respond.in_thread if isinstance(action.respond, _Click) else True
            self.ephemeral(action.channel, action.thread if in_thread else "", action.user_id, text)

    def history(self, channel: str, thread: str, limit: int) -> list[HistoryMessage]:
        out = self._call("conversations.replies",
                         {"channel": channel, "ts": thread, "limit": max(1, min(limit, 1000))},
                         http_get=True)
        messages = out.get("messages") or []
        return [HistoryMessage(user_id=m.get("user") or m.get("bot_id") or "",
                               text=m.get("text") or "",
                               is_bot=bool(m.get("bot_id")) or m.get("user") == self.bot_user_id)
                for m in messages[-limit:] if isinstance(m, dict)]

    # Web API --------------------------------------------------------------------------

    def _call(self, method: str, body: dict, *, token: str | None = None,
              http_get: bool = False) -> dict:
        url = f"{self._api_url or slack_api_url()}/{method}"
        headers = {"Authorization": f"Bearer {token or self._bot_token}"}
        for _ in range(5):
            if http_get:
                response = self._http.get(url, params=body, headers=headers)
            else:
                response = self._http.post(url, json=body, headers=headers)
            data = response.json() if response.content else {}
            if response.status_code == 429 or data.get("error") == "ratelimited":
                wait = _retry_after(response)
                if wait > MAX_RETRY_AFTER_S:
                    raise SlackError(method, "ratelimited")
                log.info("slack_rate_limited", method=method, retry_after_s=wait)
                if self._stop.wait(wait):
                    raise SlackError(method, "stopped")
                continue
            if not data.get("ok"):
                raise SlackError(method, str(data.get("error") or f"http_{response.status_code}"))
            return data
        raise SlackError(method, "ratelimited")


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _blocks(text: str, buttons: Sequence[Button]) -> list[dict]:
    """Section blocks of at most 3,000 characters, then one actions block for the buttons."""
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": part}}
                          for part in split_text(text, SECTION_LIMIT) if part]
    if buttons:
        elements = []
        for button in buttons:
            element: dict[str, Any] = {
                "type": "button", "action_id": button.id, "value": button.id,
                "text": {"type": "plain_text", "text": button.label[:75]},
            }
            if button.style in ("primary", "danger"):
                element["style"] = button.style
            elements.append(element)
        blocks.append({"type": "actions", "block_id": "symphony_actions", "elements": elements})
    return blocks
