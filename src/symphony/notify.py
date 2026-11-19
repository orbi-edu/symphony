"""Slack and Discord notifications (internals §8): `post(event, text, *, link=None, github_login=None)`.

Channels, picked from the environment at call time; every configured channel gets each event:

- Slack: `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` (Web API `chat.postMessage`, scope `chat:write`),
  otherwise `SLACK_WEBHOOK_URL` (an incoming webhook).
- Discord: `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` (`POST /channels/{id}/messages` under
  `DISCORD_API_URL`, `Authorization: Bot …`, permission Send Messages), otherwise
  `DISCORD_WEBHOOK_URL` (Execute Webhook, `wait=true` so a rejected message is reported).
- none: nothing happens.

Mentions come from `notify.users` in WORKFLOW.md: a `slack` and a `discord` map, each from
GitHub login to that service's user id. A process that already holds a parsed config calls
`configure(cfg.notify_users)` once at start; a process that never does reads the maps from
the mounted WORKFLOW.md front matter (`$SYMPHONY_WORKFLOW_MOUNT/WORKFLOW.md`, reloaded when
the file changes), so every pod gets mentions without extra wiring.

Discord messages are Discord markdown, at most 2000 characters (the text is cut, the title
and link are kept), and `allowed_mentions` lets only the mapped user be pinged: never a role,
`@everyone` or `@here`, whatever the text says. A 429 is retried once after its
`retry_after` when that wait is short; otherwise the message is dropped.

`post` never raises into its caller: a notification is advice, never a reason for a run,
the merger or the daemon to fail. Each send is bounded by a 2 s timeout, and an identical
message that was delivered to a channel in the last 60 s is not sent there again (per
process). The caller's `text` is passed through as written.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Mapping

import httpx
import structlog
import yaml

from . import __version__
from .config import DISCORD_USER_ID
from .endpoints import discord_api_url, slack_api_url

log = structlog.get_logger()

TIMEOUT_S = 2.0
DEDUP_WINDOW_S = 60.0
CHANNELS = ("slack", "discord")
DISCORD_MAX_CHARS = 2000
# A longer wait than this would hold the caller; the message is dropped instead.
DISCORD_MAX_RETRY_AFTER_S = 5.0
# Discord asks bots for `DiscordBot ($url, $version)`; the URL names the project, not an account.
DISCORD_USER_AGENT = f"DiscordBot (https://symphony.invalid, {__version__})"

# event → (Slack emoji, title). The set is the contract's event list.
EVENTS: dict[str, tuple[str, str]] = {
    "input_requested": (":raising_hand:", "Agent is waiting for an answer"),
    "back_to_author": (":leftwards_arrow_with_hook:", "PR returned to its author"),
    "needs_maintainer": (":rotating_light:", "A maintainer is needed"),
    "needs_approval": (":eyes:", "Waiting for human approval"),
    "preview_ready": (":iphone:", "Simulator preview ready"),
    "batch_split": (":scissors:", "Merge train batch was split"),
    "main_red": (":red_circle:", "main is broken"),
    "main_green": (":large_green_circle:", "main is green again"),
    "run_error": (":x:", "Run failed with an error"),
}
_UNKNOWN_EVENT = (":bell:", "Symphony notice")
# Discord does not expand `:shortcodes:` sent through the API, so it gets the characters.
DISCORD_EMOJI: dict[str, str] = {
    "input_requested": "\N{HAPPY PERSON RAISING ONE HAND}",
    "back_to_author": "\N{LEFTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16}",
    "needs_maintainer": "\N{POLICE CARS REVOLVING LIGHT}",
    "needs_approval": "\N{EYES}",
    "preview_ready": "\N{MOBILE PHONE}",
    "batch_split": "\N{BLACK SCISSORS}\N{VARIATION SELECTOR-16}",
    "main_red": "\N{LARGE RED CIRCLE}",
    "main_green": "\N{LARGE GREEN CIRCLE}",
    "run_error": "\N{CROSS MARK}",
}
_UNKNOWN_DISCORD_EMOJI = "\N{BELL}"

Sender = Callable[[str, str | None], bool]  # (message, mapped user id) → delivered

_lock = threading.Lock()
_sent: dict[str, float] = {}  # channel + message digest → monotonic time of the last delivery
_configured_users: dict[str, dict[str, str]] | None = None
_workflow_users: tuple[float | None, dict[str, dict[str, str]]] = (None, {})


def configure(users: Mapping[str, Mapping[str, str]] | None) -> None:
    """Set the `notify.users` maps for this process (`cfg.notify_users`: `slack` and
    `discord`, each GitHub login → user id). `None` returns to the mounted WORKFLOW.md."""
    global _configured_users
    _configured_users = None if users is None else _normalize_users(users)


def reset() -> None:
    """Forget the configured maps and the dedup memory (process start state)."""
    global _configured_users, _workflow_users
    with _lock:
        _sent.clear()
    _configured_users = None
    _workflow_users = (None, {})


def post(
    event: str,
    text: str,
    *,
    link: str | None = None,
    github_login: str | None = None,
) -> bool:
    """Send the event to every configured channel. True when at least one channel accepted
    it; False when none is configured, or each one deduplicated or failed (failures are
    logged, never raised)."""
    if event not in EVENTS:
        log.warning("notify_unknown_event", notify_event=event)
    delivered = False
    for channel in CHANNELS:
        try:
            if _post(channel, event, text, link=link, github_login=github_login):
                delivered = True
        except Exception as exc:  # noqa: BLE001 — a notification never fails its caller
            log.warning(
                "notify_failed", notify_channel=channel, notify_event=event,
                error=type(exc).__name__,
            )
    return delivered


def _post(
    channel: str, event: str, text: str, *, link: str | None, github_login: str | None
) -> bool:
    send = _slack_sender() if channel == "slack" else _discord_sender()
    if send is None:
        return False
    user_id = _user_id(channel, github_login)
    fmt = format_message if channel == "slack" else format_discord_message
    message = fmt(event, text, link=link, mention=f"<@{user_id}>" if user_id else "")
    digest = hashlib.sha256(f"{channel}\0{event}\0{message}".encode()).hexdigest()
    now = time.monotonic()
    with _lock:
        for key in [k for k, at in _sent.items() if now - at >= DEDUP_WINDOW_S]:
            del _sent[key]
        if digest in _sent:
            return False
        # Claimed before the network call so concurrent identical posts send once;
        # released again below when the send fails, so a retry is not suppressed.
        _sent[digest] = now
    delivered = False
    try:
        delivered = send(message, user_id)
    finally:
        if not delivered:
            with _lock:
                if _sent.get(digest) == now:
                    del _sent[digest]
    return delivered


# Slack ----------------------------------------------------------------------------------


def _slack_sender() -> Sender | None:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if token and channel:
        return lambda message, _user: _send_slack_bot(token, channel, message)
    if webhook:
        return lambda message, _user: _send_slack_webhook(webhook, message)
    return None


def _send_slack_bot(token: str, channel: str, message: str) -> bool:
    response = httpx.post(
        f"{slack_api_url()}/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": channel, "text": message, "mrkdwn": True, "unfurl_links": False},
        timeout=TIMEOUT_S,
    )
    body = _json(response)
    if response.status_code != 200 or not body.get("ok"):
        log.warning(
            "notify_rejected", notify_channel="slack", status=response.status_code,
            slack_error=body.get("error", ""),
        )
        return False
    return True


def _send_slack_webhook(url: str, message: str) -> bool:
    response = httpx.post(url, json={"text": message}, timeout=TIMEOUT_S)
    if response.status_code != 200:
        log.warning("notify_rejected", notify_channel="slack", status=response.status_code)
        return False
    return True


def format_message(event: str, text: str, *, link: str | None = None, mention: str = "") -> str:
    """Slack mrkdwn: `<emoji> *<title>*`, the mention, the text, and the link."""
    emoji, title = EVENTS.get(event, _UNKNOWN_EVENT)
    head = f"{emoji} *{title}*"
    if mention:
        head += f" {mention}"
    parts = [head]
    if text.strip():
        parts.append(_escape(text.strip()))
    if link:
        parts.append(f"<{_escape(link)}|Open>")
    return "\n".join(parts)


def _escape(value: str) -> str:
    # The three characters Slack's mrkdwn requires escaped; everything else is literal.
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Discord --------------------------------------------------------------------------------


def _discord_sender() -> Sender | None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    headers = {"User-Agent": DISCORD_USER_AGENT}
    if token and channel:
        url = f"{discord_api_url()}/channels/{channel}/messages"
        headers["Authorization"] = f"Bot {token}"
        return lambda message, user: _send_discord(url, headers, {}, message, user)
    if webhook:
        return lambda message, user: _send_discord(webhook, headers, {"wait": "true"}, message, user)
    return None


def _send_discord(
    url: str, headers: dict[str, str], params: dict[str, str], message: str, user_id: str | None
) -> bool:
    payload = {
        "content": message,
        # `parse` must stay empty when `users` is listed; together they ping only that user.
        "allowed_mentions": {"parse": [], "users": [user_id]} if user_id else {"parse": []},
    }
    response = httpx.post(url, headers=headers, params=params, json=payload, timeout=TIMEOUT_S)
    if response.status_code == 429:
        wait = _retry_after(response)
        if wait is not None and wait <= DISCORD_MAX_RETRY_AFTER_S:
            time.sleep(wait)
            response = httpx.post(
                url, headers=headers, params=params, json=payload, timeout=TIMEOUT_S
            )
    if response.status_code not in (200, 204):
        body = _json(response)
        log.warning(
            "notify_rejected", notify_channel="discord", status=response.status_code,
            discord_code=body.get("code"),
        )
        return False
    return True


def _retry_after(response: httpx.Response) -> float | None:
    # Discord: rely on the `retry_after` field or the `Retry-After` header (seconds).
    value = _json(response).get("retry_after", response.headers.get("Retry-After"))
    try:
        wait = float(value)
    except (TypeError, ValueError):
        return None
    return wait if wait >= 0 else None


def format_discord_message(
    event: str, text: str, *, link: str | None = None, mention: str = ""
) -> str:
    """Discord markdown, at most 2000 characters: `<emoji> **<title>**`, the mention, the
    text (cut with `…` when too long) and `[Open](<link>)` (no link preview)."""
    _, title = EVENTS.get(event, _UNKNOWN_EVENT)
    head = f"{DISCORD_EMOJI.get(event, _UNKNOWN_DISCORD_EMOJI)} **{title}**"
    if mention:
        head += f" {mention}"
    tail = f"[Open](<{link}>)" if link else ""
    body = text.strip()
    if body:
        room = DISCORD_MAX_CHARS - len(head) - 1 - (len(tail) + 1 if tail else 0)
        if len(body) > room:
            body = body[: max(room - 1, 0)].rstrip() + "…"
    return "\n".join(part for part in (head, body, tail) if part)[:DISCORD_MAX_CHARS]


# Shared ---------------------------------------------------------------------------------


def _json(response: httpx.Response) -> dict:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    body = response.json()
    return body if isinstance(body, dict) else {}


def _user_id(channel: str, github_login: str | None) -> str | None:
    if not github_login:
        return None
    users = _configured_users if _configured_users is not None else _users_from_workflow()
    return users.get(channel, {}).get(github_login.casefold())


def _normalize_users(users: Mapping) -> dict[str, dict[str, str]]:
    # GitHub logins are case-insensitive; user ids are opaque and kept verbatim.
    out: dict[str, dict[str, str]] = {channel: {} for channel in CHANNELS}
    for channel in CHANNELS:
        section = users.get(channel)
        if not isinstance(section, Mapping):
            continue
        for login, user_id in section.items():
            login, user_id = str(login or "").strip(), str(user_id or "").strip()
            # Discord rejects a mention list that is not snowflakes, dropping the message.
            if login and user_id and (channel != "discord" or DISCORD_USER_ID.fullmatch(user_id)):
                out[channel][login.casefold()] = user_id
    return out


def _users_from_workflow() -> dict[str, dict[str, str]]:
    global _workflow_users
    path = os.path.join(os.environ.get("SYMPHONY_WORKFLOW_MOUNT", "/workflow"), "WORKFLOW.md")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    cached_mtime, cached = _workflow_users
    if cached_mtime == mtime:
        return cached
    users: dict[str, dict[str, str]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            front = _front_matter(fh.read())
        section = (front.get("notify") or {}).get("users") or {}
        if isinstance(section, Mapping):
            users = _normalize_users(section)
    except (OSError, yaml.YAMLError, AttributeError):
        log.warning("notify_users_unreadable")
    _workflow_users = (mtime, users)
    return users


def _front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    loaded = yaml.safe_load(text[3:end])
    return loaded if isinstance(loaded, dict) else {}


# Block Kit messages with a button (Slack only; Discord gets plain text) ----------


def post_with_button(
    event: str,
    text: str,
    *,
    button_label: str,
    button_action_id: str,
    link: str | None = None,
    github_logins: list[str] | None = None,
) -> bool:
    """Post a Slack Block Kit message with one button.  Discord gets a plain-text
    fallback with no button.  Returns True when at least one channel accepted the
    message.

    ``github_logins`` is the list of reviewers to mention.
    """
    if event not in EVENTS:
        log.warning("notify_unknown_event", notify_event=event)
    delivered = False
    # Slack — Block Kit with button.
    try:
        if _post_slack_blocks(event, text, button_label=button_label,
                              button_action_id=button_action_id, link=link,
                              github_logins=github_logins or []):
            delivered = True
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_failed", notify_channel="slack", notify_event=event,
                    error=type(exc).__name__)
    # Discord — plain text, no button.
    try:
        login = (github_logins or [None])[0]
        if _post("discord", event, text, link=link, github_login=login):
            delivered = True
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_failed", notify_channel="discord", notify_event=event,
                    error=type(exc).__name__)
    return delivered


def _post_slack_blocks(
    event: str,
    text: str,
    *,
    button_label: str,
    button_action_id: str,
    link: str | None = None,
    github_logins: list[str],
) -> bool:
    """Send a Slack Block Kit message with a button.  Uses ``chat.postMessage`` with
    the bot token only (a webhook cannot send blocks with interactive elements)."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL", "").strip()
    if not token or not channel:
        return False
    users = _configured_users if _configured_users is not None else _users_from_workflow()
    slack_map = users.get("slack", {})
    mention_parts = []
    for login in github_logins:
        uid = slack_map.get(login.casefold())
        if uid:
            mention_parts.append(f"<@{uid}>")
    emoji, title = EVENTS.get(event, _UNKNOWN_EVENT)
    header = f"{emoji} *{title}*"
    if mention_parts:
        header += " " + " ".join(mention_parts)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header + "\n" + _escape(text)}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": button_label},
             "action_id": button_action_id},
        ]},
    ]
    if link:
        blocks[0]["text"]["text"] += f"\n<{_escape(link)}|Open>"
    fallback = format_message(event, text, link=link,
                              mention=" ".join(mention_parts) if mention_parts else "")
    # Dedup using the same mechanism.
    digest = hashlib.sha256(f"slack\0{event}\0{button_action_id}".encode()).hexdigest()
    now = time.monotonic()
    with _lock:
        for key in [k for k, at in _sent.items() if now - at >= DEDUP_WINDOW_S]:
            del _sent[key]
        if digest in _sent:
            return False
        _sent[digest] = now
    ok = False
    try:
        response = httpx.post(
            f"{slack_api_url()}/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": fallback, "blocks": blocks,
                  "unfurl_links": False},
            timeout=TIMEOUT_S,
        )
        body = _json(response)
        if response.status_code == 200 and body.get("ok"):
            ok = True
        else:
            log.warning("notify_rejected", notify_channel="slack", status=response.status_code,
                        slack_error=body.get("error", ""))
    finally:
        if not ok:
            with _lock:
                if _sent.get(digest) == now:
                    del _sent[digest]
    return ok
