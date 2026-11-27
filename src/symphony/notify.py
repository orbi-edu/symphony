"""Slack and Discord notifications (internals §8): `post(event, text, *, link=None, github_logins=(),
direct_to=())`.

Channels, picked from the environment at call time; every configured channel gets each event:

- Slack: `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` (Web API `chat.postMessage`, scope `chat:write`),
  otherwise `SLACK_WEBHOOK_URL` (an incoming webhook).
- Discord: `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` (`POST /channels/{id}/messages` under
  `DISCORD_API_URL`, `Authorization: Bot …`, permission Send Messages), otherwise
  `DISCORD_WEBHOOK_URL` (Execute Webhook, `wait=true` so a rejected message is reported).
- none: nothing happens.

Mentions come from `notify.users` in WORKFLOW.md: a `slack` and a `discord` map, each from
GitHub login (or e-mail, for people a ticket names: `questions.py`) to that service's user
id. A process that already holds a parsed config calls
`configure(cfg.notify_users)` once at start; a process that never does reads the maps from
the mounted WORKFLOW.md front matter (`$SYMPHONY_WORKFLOW_MOUNT/WORKFLOW.md`, reloaded when
the file changes), so every pod gets mentions without extra wiring.

Direct messages: the people `direct_to` names (`notify.users` keys: the ticket's assignee,
the person a question is for) also get the event as a direct message from the bot, on each
service whose channel is configured and that has a bot token (a webhook cannot open a DM):
Slack `chat.postMessage` with the user id as `channel` (the app's DM, scope `chat:write`),
Discord Create DM (`POST /users/@me/channels`) then Create Message in it. Each person once
per event, never a mention or a ping beyond the DM itself; a key `notify.users` does not map
is skipped (logged at debug). `notify.direct_messages: false` turns DMs off and
`notify.direct_events` limits them to some events.

Discord messages are Discord markdown, at most 2000 characters (the text is cut, the title
and link are kept), and `allowed_mentions` lets only the mapped user be pinged: never a role,
`@everyone` or `@here`, whatever the text says. A 429 is retried once after its
`retry_after` when that wait is short; otherwise the message is dropped.

`post` never raises into its caller: a notification is advice, never a reason for a run,
the merger or the daemon to fail. Each send is bounded by a 2 s timeout, and an identical
message that was delivered to a destination (a channel, or one person's DM) in the last 60 s
is not sent there again (per process). The caller's `text` is passed through as written.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial

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
    "retries_exhausted": (":octagonal_sign:", "Symphony stopped retrying a ticket"),
    "not_queued": (":inbox_tray:", "Ticket not queued"),
    "triage_opened": (":stethoscope:", "Failure under investigation"),
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
    "retries_exhausted": "\N{OCTAGONAL SIGN}",
    "not_queued": "\N{INBOX TRAY}",
    "triage_opened": "\N{STETHOSCOPE}",
}
_UNKNOWN_DISCORD_EMOJI = "\N{BELL}"

Sender = Callable[[str, list[str]], bool]  # (message, mapped user ids) → delivered
DirectSender = Callable[[str, str], bool]  # (user id, message) → delivered


@dataclass(frozen=True)
class _Settings:
    """`notify.users` (service → key, casefolded → user id) and the direct-message switches."""

    users: dict[str, dict[str, str]] = field(default_factory=dict)
    direct_messages: bool = True
    direct_events: frozenset[str] | None = None  # None: every event


_lock = threading.Lock()
_sent: dict[str, float] = {}  # destination + message digest → monotonic time of the last delivery
_configured: _Settings | None = None
_workflow: tuple[float | None, _Settings] = (None, _Settings())
_discord_dms: dict[tuple[str, str], str] = {}  # (bot token, user id) → that DM's channel id


def configure(
    users: Mapping[str, Mapping[str, str]] | None,
    *,
    direct_messages: bool = True,
    direct_events: Iterable[str] | None = None,
) -> None:
    """Set this process's `notify.users` maps (`cfg.notify_users`: `slack` and `discord`, each
    GitHub login or e-mail → user id) and `notify.direct_messages` / `notify.direct_events`
    (None: every event). `users=None` returns to the mounted WORKFLOW.md."""
    global _configured
    _configured = None if users is None else _Settings(
        _normalize_users(users), direct_messages,
        None if direct_events is None else frozenset(direct_events),
    )


def reset() -> None:
    """Forget the configured maps, the dedup memory and the DM channels (process start state)."""
    global _configured, _workflow
    with _lock:
        _sent.clear()
        _discord_dms.clear()
    _configured = None
    _workflow = (None, _Settings())


def users() -> dict[str, dict[str, str]]:
    """The `notify.users` maps in force: service → key (casefolded) → user id."""
    return _settings().users


_GITHUB_LOGIN = re.compile(r"[a-z0-9][a-z0-9-]{0,38}\Z")


def login_for(keys: Iterable[str | None]) -> str | None:
    """The GitHub login `notify.users` maps to the same chat user as the first of `keys` it
    maps (an e-mail, a tracker name or a login): the key itself when it is a login, else the
    first login-shaped key of that user. None when no key maps or that user has no login."""
    maps = users()
    for key in keys:
        key = (key or "").strip().casefold()
        if not key:
            continue
        matched = False
        for service in sorted(maps):
            mapped = maps[service]
            user_id = mapped.get(key)
            if user_id is None:
                continue
            matched = True
            if _GITHUB_LOGIN.match(key):
                return key
            logins = sorted(k for k, v in mapped.items() if v == user_id and _GITHUB_LOGIN.match(k))
            if logins:
                return logins[0]
        if matched:  # the first key that maps names the person; a later key could name another
            return None
    return None


def post(
    event: str,
    text: str,
    *,
    link: str | None = None,
    github_logins: Sequence[str] = (),
    direct_to: Sequence[str] = (),
) -> bool:
    """Send the event to every configured channel, and as a direct message to each person
    `direct_to` names. True when at least one channel or DM accepted it; False when none is
    configured, or each one deduplicated or failed (failures are logged, never raised)."""
    if event not in EVENTS:
        log.warning("notify_unknown_event", notify_event=event)
    delivered = False
    for channel in CHANNELS:
        if _safely(channel, event, _post, channel, event, text, link=link,
                   github_logins=github_logins):
            delivered = True
        if _safely(channel, event, _post_direct, channel, event, text, link=link, keys=direct_to):
            delivered = True
    return delivered


def _safely(channel: str, event: str, send: Callable[..., bool], *args, **kwargs) -> bool:
    try:
        return bool(send(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 — a notification never fails its caller
        log.warning(
            "notify_failed", notify_channel=channel, notify_event=event,
            error=type(exc).__name__,
        )
        return False


def _post(
    channel: str, event: str, text: str, *, link: str | None, github_logins: Sequence[str]
) -> bool:
    send = _slack_sender() if channel == "slack" else _discord_sender()
    if send is None:
        return False
    user_ids = _user_ids(channel, github_logins)
    fmt = format_message if channel == "slack" else format_discord_message
    mentions = " ".join(f"<@{uid}>" for uid in user_ids)
    message = fmt(event, text, link=link, mention=mentions)
    return _once(f"{channel}\0{event}\0{message}", partial(send, message, user_ids))


def _post_direct(
    channel: str, event: str, text: str, *, link: str | None, keys: Sequence[str]
) -> bool:
    """One DM per person `keys` maps on this service; no mention (a DM reaches its reader)."""
    send = _direct_sender(channel)
    if send is None:
        return False
    fmt = format_message if channel == "slack" else format_discord_message
    message = fmt(event, text, link=link)
    delivered = False
    for uid in _direct_ids(channel, event, keys):
        if _once(f"{channel}:dm:{uid}\0{event}\0{message}", partial(send, uid, message)):
            delivered = True
    return delivered


def _once(key: str, send: Callable[[], bool]) -> bool:
    """`send()`, unless the same message reached the same destination in the last 60 s."""
    digest = hashlib.sha256(key.encode()).hexdigest()
    now = time.monotonic()
    with _lock:
        for stale in [k for k, at in _sent.items() if now - at >= DEDUP_WINDOW_S]:
            del _sent[stale]
        if digest in _sent:
            return False
        # Claimed before the network call so concurrent identical posts send once;
        # released again below when the send fails, so a retry is not suppressed.
        _sent[digest] = now
    delivered = False
    try:
        delivered = send()
    finally:
        if not delivered:
            with _lock:
                if _sent.get(digest) == now:
                    del _sent[digest]
    return delivered


def _direct_sender(channel: str) -> DirectSender | None:
    """DMs come in addition to the channel: none where the channel is not configured, and
    only with the bot token."""
    if (_slack_sender() if channel == "slack" else _discord_sender()) is None:
        return None
    if channel == "slack":
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        return partial(_send_slack_bot, token) if token else None
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    return partial(_send_discord_dm, token) if token else None


def _direct_ids(channel: str, event: str, keys: Sequence[str]) -> list[str]:
    """The user ids to DM for `event` on `channel`: each person once; none when
    `notify.direct_messages` is off or `notify.direct_events` leaves the event out."""
    if not keys:
        return []
    settings = _settings()
    if not settings.direct_messages or (
            settings.direct_events is not None and event not in settings.direct_events):
        return []
    ids = list(dict.fromkeys(_user_ids(channel, keys)))
    if not ids:
        log.debug("notify_direct_unmapped", notify_channel=channel, notify_event=event)
    return ids


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
        return lambda message, users: _send_discord(url, headers, {}, message, users)
    if webhook:
        return lambda message, users: _send_discord(webhook, headers, {"wait": "true"}, message, users)
    return None


def _send_discord(
    url: str, headers: dict[str, str], params: dict[str, str], message: str, user_ids: list[str]
) -> bool:
    payload = {
        "content": message,
        # `parse` must stay empty when `users` is listed; together they ping only those users.
        "allowed_mentions": {"parse": [], "users": user_ids} if user_ids else {"parse": []},
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


def _send_discord_dm(token: str, user_id: str, message: str) -> bool:
    headers = {"User-Agent": DISCORD_USER_AGENT, "Authorization": f"Bot {token}"}
    dm = _discord_dm_channel(token, headers, user_id)
    if dm is None:
        return False
    # No user listed: `parse: []` keeps every role, `@everyone` and `@here` silent.
    return _send_discord(f"{discord_api_url()}/channels/{dm}/messages", headers, {}, message, [])


def _discord_dm_channel(token: str, headers: dict[str, str], user_id: str) -> str | None:
    """Create DM (`POST /users/@me/channels`): Discord returns the DM already open, if any.
    Kept for the process, so each person costs one call."""
    with _lock:
        known = _discord_dms.get((token, user_id))
    if known:
        return known
    response = httpx.post(f"{discord_api_url()}/users/@me/channels", headers=headers,
                          json={"recipient_id": user_id}, timeout=TIMEOUT_S)
    body = _json(response)
    if response.status_code != 200 or not body.get("id"):
        log.warning("notify_rejected", notify_channel="discord", method="create_dm",
                    status=response.status_code, discord_code=body.get("code"))
        return None
    with _lock:
        _discord_dms[(token, user_id)] = str(body["id"])
    return str(body["id"])


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


def _user_ids(channel: str, github_logins: Sequence[str]) -> list[str]:
    if not github_logins:
        return []
    channel_map = _settings().users.get(channel, {})
    return [uid for login in github_logins
            for uid in [channel_map.get(login.casefold())] if uid]


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


def _settings() -> _Settings:
    return _configured if _configured is not None else _workflow_settings()


def _workflow_settings() -> _Settings:
    global _workflow
    path = os.path.join(os.environ.get("SYMPHONY_WORKFLOW_MOUNT", "/workflow"), "WORKFLOW.md")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return _Settings()
    cached_mtime, cached = _workflow
    if cached_mtime == mtime:
        return cached
    settings = _Settings()
    try:
        with open(path, encoding="utf-8") as fh:
            section = _front_matter(fh.read()).get("notify") or {}
        users = section.get("users") or {}
        events = section.get("direct_events")
        settings = _Settings(
            _normalize_users(users) if isinstance(users, Mapping) else {},
            # The config parser rejects anything but a boolean; only `false` turns DMs off.
            section.get("direct_messages") is not False,
            frozenset(map(str, events)) if isinstance(events, list) else None,
        )
    except (OSError, yaml.YAMLError, AttributeError):
        log.warning("notify_users_unreadable")
    _workflow = (mtime, settings)
    return settings


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
    direct_to: Sequence[str] = (),
) -> bool:
    """Post a Slack Block Kit message with one button, in the channel and as a DM to each
    person `direct_to` names (`post`'s rules).  Discord gets a plain-text fallback with no
    button.  Returns True when at least one channel or DM accepted the message.

    ``github_logins`` is the list of reviewers to mention.
    """
    if event not in EVENTS:
        log.warning("notify_unknown_event", notify_event=event)
    logins = github_logins or []
    delivered = _safely("slack", event, _post_slack_blocks, event, text,
                        button_label=button_label, button_action_id=button_action_id,
                        link=link, github_logins=logins, direct_to=direct_to)
    # Discord — plain text, no button.
    if _safely("discord", event, _post, "discord", event, text, link=link, github_logins=logins):
        delivered = True
    if _safely("discord", event, _post_direct, "discord", event, text, link=link, keys=direct_to):
        delivered = True
    return delivered


def _post_slack_blocks(
    event: str,
    text: str,
    *,
    button_label: str,
    button_action_id: str,
    link: str | None = None,
    github_logins: list[str],
    direct_to: Sequence[str],
) -> bool:
    """Send a Slack Block Kit message with a button, to the channel and the DMs.  Uses
    ``chat.postMessage`` with the bot token only (a webhook cannot send blocks with
    interactive elements)."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL", "").strip()
    if not token or not channel:
        return False
    mention = " ".join(f"<@{uid}>" for uid in _user_ids("slack", github_logins))
    send = partial(_send_slack_blocks, token, event=event, text=text, button_label=button_label,
                   button_action_id=button_action_id, link=link)
    delivered = _once(f"slack\0{event}\0{button_action_id}", partial(send, channel, mention))
    for uid in _direct_ids("slack", event, direct_to):
        if _once(f"slack:dm:{uid}\0{event}\0{button_action_id}", partial(send, uid, "")):
            delivered = True
    return delivered


def _send_slack_blocks(token: str, channel: str, mention: str, *, event: str, text: str,
                       button_label: str, button_action_id: str, link: str | None) -> bool:
    emoji, title = EVENTS.get(event, _UNKNOWN_EVENT)
    header = f"{emoji} *{title}*"
    if mention:
        header += " " + mention
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header + "\n" + _escape(text)}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": button_label},
             "action_id": button_action_id},
        ]},
    ]
    if link:
        blocks[0]["text"]["text"] += f"\n<{_escape(link)}|Open>"
    fallback = format_message(event, text, link=link, mention=mention)
    return bool(_slack_call(token, "chat.postMessage", {
        "channel": channel, "text": fallback, "blocks": blocks, "unfurl_links": False,
    }))


# Questions (`questions.py`): a message people answer with a button or in its thread ------

QUESTION_ACTION_PREFIX = "ask:"
_BUTTON_TEXT_MAX = 75  # Slack's limit for a button's plain_text
_BUTTONS_MAX = 25  # elements in one actions block
_TITLE_MAX = 80  # the ticket title in a question's header
_FALLBACK_QUESTION_MAX = 150  # the question in a notification's text
_QUESTION_FOOTER = "Answer with a button, in this thread, on the ticket or in a comment on the PR."


def question_action(qid: str, index: int) -> str:
    """The button id of option `index` (0-based) of question `qid`."""
    return f"{QUESTION_ACTION_PREFIX}{qid}:{index}"


def parse_question_action(action_id: str) -> tuple[str, int] | None:
    """`ask:<qid>:<index>` → (qid, index)."""
    if not action_id.startswith(QUESTION_ACTION_PREFIX):
        return None
    qid, _, index = action_id[len(QUESTION_ACTION_PREFIX):].rpartition(":")
    return (qid, int(index)) if qid and index.isdigit() else None


def post_question(
    question: str, *, qid: str, options: Sequence[str], ticket: str, title: str = "",
    decider: str = "", heading: str = "", mention_keys: Sequence[str] = (),
    direct_keys: Sequence[str] = (), link: str | None = None,
    thread: Mapping[str, str] | None = None,
) -> dict | None:
    """Post an agent's question: in Slack (bot token and channel) at the top of the channel
    or, with `thread` (`{channel, ts}`), as a reply also shown there. The message is a divider
    (consecutive messages of the app stay apart), a header line (`heading`, default the
    event's title · the ticket and its `title`, linked · who decides: the user `mention_keys`
    maps, else `decider`), the question as written, one button per option and a one-line
    footer. The options are also listed as text only when a button had to cut one. Discord,
    when configured, gets the text and the options without buttons. `direct_keys` (who
    decides, the ticket's assignee) also get it as a DM, buttons included, under `post`'s
    rules. Returns `{channel, ts, header, dms}`: Slack's channel message (left out when Slack
    did not take it), its header for `edit_question`, and `dms`, the Slack DMs
    `[{channel, ts}]`; None when Slack took neither. Never raises."""
    discord_text = "\n\n".join(part for part in (
        heading, " · ".join(p for p in (ticket, title.strip(), decider) if p), question.strip(),
        "\n".join(f"{i}. {o.strip()}" for i, o in enumerate(options, 1))) if part)
    _safely("discord", "input_requested", _post, "discord", "input_requested", discord_text,
            link=link, github_logins=mention_keys)
    _safely("discord", "input_requested", _post_direct, "discord", "input_requested",
            discord_text, link=link, keys=direct_keys)
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = (thread or {}).get("channel") or os.environ.get("SLACK_CHANNEL", "").strip()
    if not token or not channel:
        return None
    mention = " ".join(f"<@{uid}>" for uid in _user_ids("slack", mention_keys))
    header = question_header(ticket=ticket, title=title, link=link, who=mention or _escape(decider),
                             heading=heading)
    payload: dict = {"channel": channel, "text": _question_fallback(header, question),
                     "blocks": _question_blocks(header, question, qid, options),
                     "unfurl_links": False}
    if thread and thread.get("ts"):
        payload.update(thread_ts=thread["ts"], reply_broadcast=True)
    body = _slack_call(token, "chat.postMessage", payload)
    out: dict = ({"channel": body.get("channel") or channel, "ts": body["ts"], "header": header}
                 if body.get("ts") else {"header": header})
    # A DM starts its own thread (the reply `thread` names is the channel's): the answer can
    # be a button or a reply in that DM thread.
    dms = []
    dm_payload = {k: v for k, v in payload.items() if k not in ("thread_ts", "reply_broadcast")}
    for uid in _direct_ids("slack", "input_requested", direct_keys):
        sent = _slack_call(token, "chat.postMessage", {**dm_payload, "channel": uid})
        if sent.get("ts"):
            # Slack answers with the DM's own channel id (`D…`): replies there carry it.
            dms.append({"channel": sent.get("channel") or uid, "ts": sent["ts"]})
    if dms:
        out["dms"] = dms
    return out if out.get("ts") or dms else None


def question_header(*, ticket: str, title: str = "", link: str | None = None, who: str = "",
                    heading: str = "") -> str:
    """`:raising_hand: *<heading>* · <link|KEY · title> · <who>` (Slack mrkdwn; `who` is
    already mrkdwn, a mention or an escaped name)."""
    emoji, default = EVENTS["input_requested"]
    title = title.strip()
    if len(title) > _TITLE_MAX:
        title = title[:_TITLE_MAX - 1].rstrip() + "…"
    label = _escape(" · ".join(p for p in (ticket, title) if p))
    where = f"<{_escape(link)}|{label}>" if link and label else label
    return " · ".join(p for p in (f"{emoji} *{heading or default}*", where, who) if p)


def _question_fallback(header: str, question: str) -> str:
    """The notification text: the header and the start of the question."""
    text = question.strip()
    if len(text) > _FALLBACK_QUESTION_MAX:
        text = text[:_FALLBACK_QUESTION_MAX - 1].rstrip() + "…"
    return f"{header}\n{_escape(text)}"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _question_blocks(header: str, question: str, qid: str, options: Sequence[str]) -> list[dict]:
    shown = list(options[:_BUTTONS_MAX])
    labels = [_button_text(i, option) for i, option in enumerate(shown)]
    body = _escape(question.strip())  # newlines and blank lines as written
    if len(shown) < len(options) or any(
            label != f"{i + 1}. {option.strip()}" for i, (label, option) in
            enumerate(zip(labels, shown, strict=True))):
        # A button could not show it all: the full options, once, under the question.
        body += "\n\n" + "\n".join(f"{i}. {_escape(o.strip())}" for i, o in enumerate(options, 1))
    blocks: list[dict] = [{"type": "divider"}, _section(header), _section(body)]
    if labels:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": question_action(qid, i), "value": str(i),
             "text": {"type": "plain_text", "text": label}}
            for i, label in enumerate(labels)]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _QUESTION_FOOTER}]})
    return blocks


def edit_question(where: Mapping, head: str, text: str = "", *, question: str = "") -> bool:
    """Replace a question posted by `post_question` (its channel message and its DMs), buttons
    removed: the divider and header kept, then `question` as written and the outcome (`head`,
    Slack mrkdwn written by Symphony, and `text`, shown as written). True when every one of
    them was edited."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    refs = [ref for ref in (where, *(where.get("dms") or ()))
            if ref.get("channel") and ref.get("ts")]
    if not token or not refs:
        return False
    header = where.get("header") or ""
    outcome = f"{head}\n{_escape(text.strip())}" if text.strip() else head
    blocks = [{"type": "divider"}, *([_section(header)] if header else []),
              *([_section(_escape(question.strip()))] if question.strip() else []),
              _section(outcome)]
    message = "\n".join(p for p in (header, outcome) if p)[:3000]
    edited = [bool(_slack_call(token, "chat.update", {
        "channel": ref["channel"], "ts": ref["ts"], "text": message, "blocks": blocks,
    }).get("ok")) for ref in refs]
    return all(edited)


def _button_text(index: int, option: str) -> str:
    label = f"{index + 1}. {option.strip()}"
    return label if len(label) <= _BUTTON_TEXT_MAX else label[:_BUTTON_TEXT_MAX - 1] + "…"


def _slack_call(token: str, method: str, payload: dict) -> dict:
    """A Slack Web API call; the body when Slack accepted it, else {} (logged)."""
    try:
        response = httpx.post(f"{slack_api_url()}/{method}", json=payload, timeout=TIMEOUT_S,
                              headers={"Authorization": f"Bearer {token}"})
        body = _json(response)
    except Exception as exc:  # noqa: BLE001 — a notification never fails its caller
        log.warning("notify_failed", notify_channel="slack", method=method,
                    error=type(exc).__name__)
        return {}
    if response.status_code != 200 or not body.get("ok"):
        log.warning("notify_rejected", notify_channel="slack", method=method,
                    status=response.status_code, slack_error=body.get("error", ""))
        return {}
    return body
