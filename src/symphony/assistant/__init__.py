"""The assistant: a chat bot on Slack and Discord that answers questions, researches and plans.

This module holds the types the engine and the transports share. A transport (Slack Socket
Mode, the Discord Gateway) turns vendor events into `Inbound` messages and `Action` clicks,
and implements `Outbound` to post replies. The engine (`engine.py`) knows no vendor.

Threading model: a transport runs its own background threads. `on_message` and `on_action`
must return quickly; the engine hands the work to its own threads. `Outbound` methods block
until the vendor answers and are safe to call from any thread.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class File:
    """A file attached to a message. The engine only names it; it does not download it."""

    name: str
    url: str = ""


@dataclass(frozen=True)
class Inbound:
    """One message for the assistant.

    - `channel` is where replies go (a Discord thread is its own channel).
    - `parent` is the parent channel of a Discord thread, empty otherwise. The engine allows a
      message when `is_dm`, or when `channel` or `parent` is in the allowed channels.
    - `thread` names the conversation: the Slack `thread_ts` (or the message `ts` for a new
      thread), the Discord thread id, or the root message id for a Discord reply chain.
    - `text` has the bot mention removed.
    """

    transport: str  # "slack" | "discord"
    channel: str
    thread: str
    message_id: str
    user_id: str
    text: str
    files: tuple[File, ...] = ()
    is_dm: bool = False
    parent: str = ""


@dataclass(frozen=True)
class Ref:
    """A message the bot posted, so it can be edited later."""

    channel: str
    message_id: str


@dataclass(frozen=True)
class Button:
    """A transport-neutral button. `id` is `proposal:<id>:create|start|dismiss`."""

    id: str
    label: str
    style: str = ""  # "" | "primary" | "danger"


@dataclass(frozen=True)
class Action:
    """A button click. `respond` is the transport's private handle for answering it (the
    Discord interaction id and token, the Slack response URL). The engine never reads it."""

    transport: str
    channel: str
    thread: str
    message_id: str
    user_id: str
    action_id: str  # the clicked `Button.id`
    value: str = ""
    respond: object = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class HistoryMessage:
    user_id: str
    text: str
    is_bot: bool = False


@runtime_checkable
class Outbound(Protocol):
    """Replies. `message_limit` is the longest text one `post`/`update` accepts (Slack 4,000,
    Discord 2,000). Slack splits a text into section blocks of at most 3,000 characters with
    `split_text(text, 3000)`; the engine splits longer answers into several messages."""

    name: str
    message_limit: int

    def post(
        self, channel: str, thread: str, text: str, buttons: Sequence[Button] = ()
    ) -> Ref: ...

    def update(self, ref: Ref, text: str, buttons: Sequence[Button] = ()) -> None:
        """Replace the text and the buttons. An empty `buttons` removes them."""

    def ephemeral(self, channel: str, thread: str, user_id: str, text: str) -> None:
        """A note only `user_id` sees (Slack `chat.postEphemeral`). Discord has no ephemeral
        message outside an interaction, so it posts a normal reply in the thread."""

    def ack_action(self, action: Action, text: str | None = None) -> None:
        """Answer a click. With `text`, only the clicker sees it (Slack `chat.postEphemeral`,
        Discord callback type 4 with flag EPHEMERAL). Without it, a silent ack (Discord
        callback type 6; Slack has already acked the envelope). The engine calls this at
        once, before slow work, so Discord's 3 s window holds."""

    def history(self, channel: str, thread: str, limit: int) -> list[HistoryMessage]:
        """The last `limit` messages of a thread, oldest first (Slack
        `conversations.replies`). A transport that cannot read history returns []."""


@runtime_checkable
class Transport(Outbound, Protocol):
    connected: bool

    def start(
        self,
        on_message: Callable[[Inbound], None],
        on_action: Callable[[Action], None],
    ) -> None:
        """Connect and dispatch in background threads; reconnect on its own. Returns at once."""

    def stop(self) -> None:
        """Close the connection and join the threads."""


def split_text(text: str, limit: int) -> list[str]:
    """Split `text` into parts of at most `limit` characters, at a line break when one is
    near the end, else at a space, else hard. Empty text gives one empty part."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", limit // 2, limit + 1)
        if cut <= 0:
            cut = rest.rfind(" ", limit // 2, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:]
        if rest[:1] in ("\n", " "):
            rest = rest[1:]  # the break itself is dropped
    parts.append(rest)
    return parts
