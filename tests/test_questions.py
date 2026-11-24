"""Agent questions outlive the run that asked (questions.py): posted on the ticket and in
Slack, answered from either (first answer wins), reminded once, and delivered to whoever works
the ticket when the answer comes."""

from __future__ import annotations

import pytest
from tests.conftest import FakeKV
from tests.test_dispatch import cfg

from symphony import notify, questions, state
from symphony.assistant import Action, Inbound
from symphony.assistant.answers import Answers
from symphony.sessions import SessionStore
from symphony.trackers.memory import MemoryTracker

NS = "test"
HOUR = 3_600_000
VICTOR = {"name": "Victor", "email": "victor@example.com", "mention": "https://t.example/victor"}


class Slack:
    """`notify.post_question`/`edit_question`, recorded. With `dm_channel`, each post also
    goes as one DM there, as Slack answers for a member ID."""

    def __init__(self):
        self.posts: list[dict] = []
        self.edits: list[tuple[dict, str, str]] = []
        self.dm_channel: str | None = None

    def post(self, text, *, qid, options, mention_keys=(), direct_keys=(), link=None, thread=None):
        where = {"channel": "C1", "ts": f"{len(self.posts) + 1}.0"}
        if self.dm_channel and direct_keys:
            where["dms"] = [{"channel": self.dm_channel, "ts": f"{len(self.posts) + 1}.5"}]
        self.posts.append({"text": text, "qid": qid, "options": list(options),
                           "mention_keys": list(mention_keys), "direct_keys": list(direct_keys),
                           "thread": thread, **where})
        return where

    def edit(self, where, head, text):
        self.edits.append((where, head, text))
        return True


class Out:
    def __init__(self):
        self.notes: list[str] = []

    def ack_action(self, action, text=None):
        self.notes.append(text or "")

    def ephemeral(self, channel, thread, user_id, text):
        self.notes.append(text)


@pytest.fixture
def world(redis_client, monkeypatch):
    slack = Slack()
    monkeypatch.setattr(notify, "post_question", slack.post)
    monkeypatch.setattr(notify, "edit_question", slack.edit)
    tracker = MemoryTracker()
    tracker.add_issue("i1", "ENG-1", state="In Review")
    tracker.people["i1"] = [{"name": "Ana", "email": "ana@example.com"}, VICTOR]
    kv = FakeKV()
    sessions = SessionStore(redis_client, NS)
    now = [1_000_000]

    def ask(qid="q1", options=("Keep it", "Change it"), to="victor"):
        questions.ask(redis_client, NS, qid=qid, issue_id="i1", identifier="ENG-1", sid="i1:1",
                      url="https://t.example/ENG-1", question="Which layout?", options=options,
                      to=to, now_ms=now[0])

    def tick(**over):
        questions.tick(redis_client, NS, kv=kv, tracker=tracker, cfg=cfg(**over), now_ms=now[0])

    return {"client": redis_client, "slack": slack, "tracker": tracker, "kv": kv,
            "sessions": sessions, "now": now, "ask": ask, "tick": tick}


def comments(world):
    return world["tracker"].comments.get("i1", [])


def reply_on_ticket(world, body, author="Victor"):
    root = comments(world)[0]["id"]
    return questions.queue_reply(world["client"], NS, comment_id=f"human-{body}", parent_id=root,
                                 body=body, author=author)


def run_waiting(world, qid="q1", seq=1):
    """A live attempt waiting on `qid`."""
    world["kv"].put(state.issue_key("i1"), {"status": "running", "dispatch_seq": seq})
    sid = f"i1:{seq}"
    world["sessions"].open(sid, kind="implement", state="running")
    world["sessions"].request_input(sid, qid, "Which layout?", ["Keep it", "Change it"])
    return sid


def test_a_question_is_posted_on_the_ticket_and_in_slack_naming_who_decides(world):
    world["ask"]()
    world["tick"]()
    [question] = comments(world)
    assert question["body"].startswith("**Question from the agent** for https://t.example/victor")
    assert "1. Keep it\n2. Change it" in question["body"]
    [post] = world["slack"].posts
    assert post["qid"] == "q1" and post["options"] == ["Keep it", "Change it"]
    assert post["mention_keys"] == ["victor@example.com", "Victor"]
    assert "from Victor" in post["text"]
    world["tick"]()  # nothing is posted twice
    assert len(comments(world)) == 1 and len(world["slack"].posts) == 1


def test_a_reply_on_the_ticket_answers_the_waiting_run(world):
    world["ask"]()
    world["tick"]()
    sid = run_waiting(world)
    assert reply_on_ticket(world, "2")
    world["tick"]()
    [(_, kind, actor, payload)] = world["sessions"].read_intents(sid)
    assert (kind, actor, payload) == ("answer", "Victor", {"request_id": "q1", "text": "Change it"})
    ack = comments(world)[-1]
    assert ack["parent"] == comments(world)[0]["id"]
    assert ack["body"].startswith("Answer received from Victor. The agent was waiting")
    head, text = world["slack"].edits[0][1:]
    assert "Answered" in head and "Victor, on the ticket: Change it" in text


def test_symphonys_own_replies_never_count_as_answers(world):
    world["ask"]()
    world["tick"]()
    world["now"][0] += 3 * HOUR
    world["tick"]()  # the reminder is a reply in the question's thread
    reminder = comments(world)[-1]
    questions.queue_reply(world["client"], NS, comment_id=reminder["id"],
                          parent_id=comments(world)[0]["id"], body=reminder["body"], author="bot")
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "open"


def test_the_first_answer_wins(world):
    world["ask"]()
    world["tick"]()
    won, _ = questions.submit(world["client"], NS, "q1", "1", by="ana", via="slack", now_ms=1)
    reply_on_ticket(world, "2")
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert won and (rec["answer"], rec["by"], rec["via"]) == ("Keep it", "ana", "slack")
    assert comments(world)[-1]["body"].startswith("**Answered** by ana in Slack: Keep it")


@pytest.mark.parametrize(("ticket_state", "kind", "moved_to"), [
    ("In Review", "rework", "Rework"),
    ("Todo", "queued", "Todo"),
    ("Done", "none", "Done"),
])
def test_an_answer_after_the_run_ended_sends_the_ticket_back_to_rework(
        world, ticket_state, kind, moved_to):
    world["tracker"].set_state("i1", ticket_state)
    world["ask"]()
    world["tick"]()
    questions.submit(world["client"], NS, "q1", "my own idea", by="ana", via="slack", now_ms=1)
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["delivered"] == kind
    assert world["tracker"].state_of("i1")["name"] == moved_to
    assert comments(world)[-1]["body"].endswith(questions.DELIVERY[kind])


def test_an_answer_reaches_a_run_that_went_on_without_it(world):
    world["ask"]()
    world["tick"]()
    world["kv"].put(state.issue_key("i1"), {"status": "running", "dispatch_seq": 2})
    world["sessions"].open("i1:2", kind="implement", state="running")
    questions.submit(world["client"], NS, "q1", "2", by="ana", via="slack", now_ms=1)
    world["tick"]()
    [(_, kind, _actor, payload)] = world["sessions"].read_intents("i1:2")
    assert kind == "steer" and "Answer: Change it" in payload["text"]


def test_the_person_is_reminded_once_after_the_reminder_delay(world):
    world["ask"]()
    world["tick"]()
    world["now"][0] += 2 * HOUR - 1
    world["tick"]()
    assert len(comments(world)) == 1
    world["now"][0] += 1
    world["tick"]()
    world["now"][0] += HOUR
    world["tick"]()
    reminders = comments(world)[1:]
    assert len(reminders) == 1 and "https://t.example/victor" in reminders[0]["body"]
    [_, again] = world["slack"].posts
    assert again["thread"] == {"channel": "C1", "ts": "1.0"} and again["options"]


def test_no_reminder_when_it_is_off(world):
    world["ask"]()
    world["tick"](ask_reminder_ms=0)
    world["now"][0] += 10 * HOUR
    world["tick"](ask_reminder_ms=0)
    assert len(comments(world)) == 1


def test_an_answer_given_in_tern_is_announced_but_not_delivered_again(world):
    world["ask"]()
    world["tick"]()
    sid = run_waiting(world)
    questions.submit(world["client"], NS, "q1", "Keep it", by="op", via="symphony", now_ms=1,
                     delivered="answer")
    world["tick"]()
    assert world["sessions"].read_intents(sid) == []
    assert comments(world)[-1]["body"].startswith("**Answered** by op in Symphony: Keep it")


def test_slack_buttons_and_thread_replies_answer_for_mapped_people(world):
    world["ask"]()
    world["tick"]()
    users = {"slack": {"ana": "U1"}}
    answers = Answers(world["client"], NS, notify_users=lambda: users)
    out = Out()
    stranger = Action(transport="slack", channel="C1", thread="1.0", message_id="1.0",
                      user_id="U9", action_id=notify.question_action("q1", 1), value="1")
    assert answers.on_click(stranger, out) == "unmapped"
    assert questions.load(world["client"], NS, "q1")["state"] == "open"
    assert answers.thread_known("C1", "1.0") and not answers.thread_known("C1", "9.0")
    reply = Inbound(transport="slack", channel="C1", thread="1.0", message_id="2.0",
                    user_id="U1", text="go with 2", files=(), is_dm=False)
    assert answers.on_reply(reply, out)
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["answer"], rec["by"], rec["via"]) == ("go with 2", "ana", "slack")
    click = Action(transport="slack", channel="C1", thread="1.0", message_id="1.0",
                   user_id="U1", action_id=notify.question_action("q1", 0), value="0")
    assert answers.on_click(click, out) == "already_answered"


def test_an_unanswered_question_closes_after_a_week(world):
    world["ask"]()
    world["tick"]()
    world["now"][0] += questions.EXPIRE_MS
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "closed"
    assert "Closed without an answer" in world["slack"].edits[-1][1]
    reply_on_ticket(world, "1")
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "closed"


def test_a_reply_that_is_not_to_a_question_is_not_queued(world):
    assert not questions.queue_reply(world["client"], NS, comment_id="c", parent_id="other",
                                     body="hi", author="Ana")


def test_orphaned_attempts_are_closed_and_their_question_resolved(redis_client):
    now = [10 * HOUR]
    store = SessionStore(redis_client, NS, now_ms=lambda: now[0])
    store.open("i1:2", kind="implement", state="running")
    store.request_input("i1:2", "q1", "Which?")
    store.open("i1:3", kind="implement", state="running")
    store.open("merger:acme:app:1:reviewer:1", kind="review", state="running")
    now[0] += HOUR
    closed = store.end_orphans(lambda issue: 3, idle_ms=10 * 60_000)
    assert closed == ["i1:2"]
    assert store.meta("i1:2")["state"] == "ended" and store.meta("i1:2")["input_request"] is None
    assert store.meta("i1:3")["state"] == "running"


def test_numbers_pick_options():
    options = ["Keep it", "Change it"]
    assert [questions.choice(t, options) for t in ("2", " 1. ", "#2", "opção 1", "3", "2 maybe")] \
        == ["Change it", "Keep it", "Change it", "Keep it", "3", "2 maybe"]


def test_the_question_goes_by_dm_to_who_decides_and_the_assignee(world):
    world["tracker"].add_issue("i1", "ENG-1", state="In Review", assignee="Alice",
                               assignee_email="alice@example.com")
    world["slack"].dm_channel = "D1"
    world["ask"]()
    world["tick"]()
    [post] = world["slack"].posts
    assert post["mention_keys"] == ["victor@example.com", "Victor"]
    assert post["direct_keys"] == ["victor@example.com", "Victor", "alice@example.com", "Alice"]
    rec = questions.load(world["client"], NS, "q1")
    assert rec["assignee"] == {"name": "Alice", "email": "alice@example.com", "mention": ""}
    assert rec["slack"]["dms"] == [{"channel": "D1", "ts": "1.5"}]
    # The reminder's DM is a thread of its own too.
    world["now"][0] += 3 * HOUR
    world["tick"]()
    assert world["slack"].posts[1]["direct_keys"] == post["direct_keys"]
    users = {"slack": {"alice@example.com": "U2"}}
    answers = Answers(world["client"], NS, notify_users=lambda: users)
    assert answers.thread_known("D1", "1.5") and answers.thread_known("D1", "2.5")
    assert not answers.thread_known("D1", "9.0")


def test_a_reply_in_the_dm_thread_answers_and_the_dm_is_edited(world):
    world["slack"].dm_channel = "D1"
    world["ask"]()
    world["tick"]()
    users = {"slack": {"victor@example.com": "U1"}}
    answers = Answers(world["client"], NS, notify_users=lambda: users)
    out = Out()
    elsewhere = Inbound(transport="slack", channel="D1", thread="7.0", message_id="8.0",
                        user_id="U1", text="2", files=(), is_dm=True)
    assert not answers.on_reply(elsewhere, out)  # not a question's DM thread
    reply = Inbound(transport="slack", channel="D1", thread="1.5", message_id="3.0",
                    user_id="U1", text="2", files=(), is_dm=True)
    assert answers.on_reply(reply, out)
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["answer"], rec["by"], rec["via"]) == ("Change it", "victor@example.com", "slack")
    world["tick"]()
    [(where, head, _)] = world["slack"].edits
    assert "Answered" in head and where["dms"] == [{"channel": "D1", "ts": "1.5"}]
