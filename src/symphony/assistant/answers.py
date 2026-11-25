"""Answers to agents' questions from chat (`questions.py`): a button on the question, or a
reply in its thread, in the channel or in the DM the question was sent as. Only people
mapped in `notify.users` answer; the answer is recorded at once and the daemon delivers it to
the work."""

from __future__ import annotations

import time
from collections.abc import Callable

from .. import notify, questions
from . import Action, Inbound, Outbound
from .confirm import mapped_login


def _now_ms() -> int:
    return int(time.time() * 1000)


class Answers:
    def __init__(self, client, namespace: str, notify_users: Callable[[], dict],
                 now_ms: Callable[[], int] = _now_ms):
        self.client = client
        self.namespace = namespace
        self.notify_users = notify_users
        self.now_ms = now_ms

    def thread_known(self, channel: str, thread: str) -> bool:
        """Whether a Slack thread is a question's: its replies are answers."""
        return questions.by_ref(self.client, self.namespace, "slack", f"{channel}:{thread}") is not None

    def on_click(self, action: Action, out: Outbound) -> str:
        parsed = notify.parse_question_action(action.action_id)
        rec = questions.load(self.client, self.namespace, parsed[0]) if parsed else None
        if parsed is None or rec is None or parsed[1] >= len(rec.get("options") or []):
            out.ack_action(action, "This question is no longer open.")
            return "unknown_question"
        return self._submit(action.transport, action.user_id, parsed[0],
                            rec["options"][parsed[1]], lambda text: out.ack_action(action, text))

    def on_reply(self, msg: Inbound, out: Outbound) -> bool:
        """Take a reply in a question's thread as its answer. False: not a question's thread
        (in a DM too: only the thread of a question Symphony sent there is one)."""
        if not msg.text.strip():
            return False
        qid = questions.by_ref(self.client, self.namespace, "slack", f"{msg.channel}:{msg.thread}")
        if qid is None:
            return False
        self._submit(msg.transport, msg.user_id, qid, msg.text,
                     lambda text: out.ephemeral(msg.channel, msg.thread, msg.user_id, text))
        return True

    def _submit(self, transport: str, user_id: str, qid: str, text: str,
                reply: Callable[[str], None]) -> str:
        login = mapped_login(self.notify_users(), transport, user_id)
        if login is None:
            reply("Only people mapped in notify.users can answer the agent's questions.")
            return "unmapped"
        won, rec = questions.submit(self.client, self.namespace, qid, text, by=login,
                                    via="slack", now_ms=self.now_ms())
        if won:
            reply(f"Answer recorded: {rec['answer']}. It is on its way to the agent.")
            return "answered"
        if rec and rec.get("state") == "answered":
            reply(f"Already answered by {rec.get('by')}: {rec.get('answer')}")
            return "already_answered"
        if rec and rec.get("closed") == "canceled":
            reply("Thanks, but this question is closed: the ticket was canceled.")
            return "canceled"
        reply("This question is closed.")
        return "closed"
