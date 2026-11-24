"""Agent questions outlive the run that asked (questions.py): posted on the ticket and in
Slack, answered from either (first answer wins), reminded once, and delivered to whoever works
the ticket when the answer comes."""

from __future__ import annotations

import itertools
import threading

import httpx
import pytest
from scripts.mocks.linear import server as linear_mock
from scripts.mocks.linear.store import DEFAULT_API_KEY
from tests.conftest import FakeKV
from tests.test_dispatch import cfg

from symphony import edge, notify, questions, state
from symphony.assistant import Action, Inbound
from symphony.assistant.answers import Answers
from symphony.sessions import SessionStore
from symphony.trackers.linear import LinearTracker
from symphony.trackers.memory import MemoryTracker

NS = "test"
HOUR = 3_600_000
VICTOR = {"name": "Victor", "email": "victor@example.com", "mention": "https://t.example/victor"}
_PEOPLE = itertools.count(1)  # ids of comments people write on the tracker


class Slack:
    """`notify.post_question`/`edit_question`, recorded."""

    def __init__(self):
        self.posts: list[dict] = []
        self.edits: list[tuple[dict, str, str]] = []

    def post(self, text, *, qid, options, mention_keys=(), link=None, thread=None):
        where = {"channel": "C1", "ts": f"{len(self.posts) + 1}.0"}
        self.posts.append({"text": text, "qid": qid, "options": list(options),
                           "mention_keys": list(mention_keys), "thread": thread, **where})
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
    # Like `edge.make_tracker`: every comment the adapter writes is recorded as Symphony's.
    tracker = MemoryTracker(on_comment=lambda cid: questions.record_own(redis_client, NS, cid))
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
    return comment_on_ticket(world, body, author=author, parent=comments(world)[0]["id"])


def comment_on_ticket(world, body, *, author="Ana", parent=None, mine=False, at=None,
                      settled=True):
    """A comment on the ticket, queued the way the Linear webhook queues it: written through
    Symphony's tracker adapter (`mine`, recorded), or by someone else on the tracker.
    `settled`: it arrived long enough ago for the daemon to judge it."""
    tracker = world["tracker"]
    cid = tracker.comment("i1", body, parent=parent) if mine else f"human-{next(_PEOPLE)}"
    now = world["now"][0]
    return questions.queue_reply(world["client"], NS, comment_id=cid, issue_id="i1",
                                 parent_id=parent or "", body=body, author=author,
                                 at_ms=now if at is None else at,
                                 now_ms=now - questions.SETTLE_MS if settled else now)


def pr_comment(body="2", *, login="alice", kind="User", node="IC_1"):
    """A pull request conversation comment as the GitHub webhook carries it."""
    return {"node_id": node, "body": body, "user": {"login": login, "type": kind},
            "created_at": "2026-08-07T09:00:00Z"}


def answer_on_pull(world, comment, *, title="ENG-1: layout", state_="open", number=7):
    return questions.pull_answer(world["client"], NS, repo="acme/app", number=number,
                                 pull={"title": title, "body": "", "state": state_},
                                 comment=comment, at_ms=questions.iso_ms(comment["created_at"]),
                                 now_ms=world["now"][0])


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
    questions.queue_reply(world["client"], NS, comment_id=reminder["id"], issue_id="i1",
                          parent_id=comments(world)[0]["id"], body=reminder["body"], author="bot",
                          at_ms=world["now"][0], now_ms=world["now"][0] - questions.SETTLE_MS)
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


def test_a_comment_on_a_ticket_without_open_questions_is_not_queued(world):
    assert not questions.queue_reply(world["client"], NS, comment_id="c", issue_id="i1",
                                     parent_id="other", body="hi", author="Ana", at_ms=1,
                                     now_ms=1)


# -- where people answer: the ticket and the pull request, beyond the question's thread -----

def test_with_one_open_question_a_top_level_comment_after_it_answers(world):
    world["ask"]()
    world["tick"]()
    world["now"][0] += 60_000
    assert comment_on_ticket(world, "opção 2")
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["answer"], rec["by"], rec["via"]) == ("Change it", "Ana", "linear")


def test_with_one_open_question_a_reply_to_any_symphony_comment_answers(world):
    world["ask"]()
    world["tick"]()
    note = world["tracker"].comment("i1", "I will ask for an explicit decision here.")
    world["now"][0] += 60_000
    comment_on_ticket(world, "Remove the legend", parent=note)
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["answer"], rec["via"]) == ("Remove the legend", "linear")


def test_with_two_open_questions_only_a_reply_to_a_question_answers(world):
    world["ask"]("q1")
    world["ask"]("q2")
    world["tick"]()
    world["now"][0] += 60_000
    note = world["tracker"].comment("i1", "Progress note.")
    comment_on_ticket(world, "2")
    comment_on_ticket(world, "1", parent=note)
    world["tick"]()
    assert [questions.load(world["client"], NS, q)["state"] for q in ("q1", "q2")] == ["open"] * 2
    comment_on_ticket(world, "1", parent=questions.load(world["client"], NS, "q2")["comment"])
    world["tick"]()
    states = [questions.load(world["client"], NS, q)["state"] for q in ("q1", "q2")]
    assert states == ["open", "answered"]


def test_symphonys_own_and_earlier_comments_never_answer(world):
    world["ask"]()
    world["tick"]()
    asked = world["now"][0]
    world["now"][0] += 60_000
    # written through the adapter (the agent's note, the merger, a report), whoever the author
    comment_on_ticket(world, "2", mine=True, author="Ana")
    comment_on_ticket(world, "2", mine=True, parent=comments(world)[0]["id"])  # even in-thread
    comment_on_ticket(world, "2", parent="human-note")  # a reply to a person, not to Symphony
    comment_on_ticket(world, "2", at=asked - 1)  # made before the question
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "open"


def test_the_api_key_accounts_comment_counts_when_symphony_did_not_write_it(world):
    """Symphony may run with a person's own API key: that person answers from the same
    account, in the tracker's own interface."""
    world["ask"]()
    world["tick"]()
    world["now"][0] += 60_000
    comment_on_ticket(world, "1", author="Symphony")
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["answer"], rec["by"]) == ("Keep it", "Symphony")


def test_a_comment_is_judged_only_once_it_has_settled(world):
    """The webhook can arrive before the process that wrote a comment has recorded it."""
    world["ask"]()
    world["tick"]()
    world["now"][0] += 60_000
    comment_on_ticket(world, "2", settled=False)
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "open"
    world["now"][0] += questions.SETTLE_MS
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["answer"] == "Change it"


def test_a_person_on_the_tickets_pull_request_answers(world):
    world["ask"]()
    world["tick"]()
    assert answer_on_pull(world, pr_comment("2")) == "q1"
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["answer"], rec["by"], rec["via"]) == ("Change it", "alice", "github")
    assert comments(world)[-1]["body"].startswith(
        "**Answered** by alice on the pull request: Change it")
    assert "alice, on the pull request: Change it" in world["slack"].edits[0][2]
    assert world["client"].get(state.answered_key("i1")) == '["IC_1"]'  # the review watch skips it


@pytest.mark.parametrize("comment,title,state_", [
    (pr_comment(kind="Bot", login="symphony[bot]"), "ENG-1: layout", "open"),
    (pr_comment(), "ENG-2: other ticket", "open"),
    (pr_comment(), "ENG-12: other ticket", "open"),
    (pr_comment(), "ENG-1: layout", "closed"),
])
def test_bots_other_tickets_and_closed_pull_requests_do_not_answer(world, comment, title, state_):
    world["ask"]()
    world["tick"]()
    assert answer_on_pull(world, comment, title=title, state_=state_) is None
    assert questions.load(world["client"], NS, "q1")["state"] == "open"


def test_a_pull_request_comment_does_not_guess_between_two_questions(world):
    world["ask"]("q1")
    world["ask"]("q2")
    world["tick"]()
    assert answer_on_pull(world, pr_comment()) is None


def test_the_question_says_where_to_answer(world):
    world["ask"]()
    world["tick"]()
    assert ("Answer by replying here, in the Slack thread, with a button, or in a comment on "
            "the PR") in comments(world)[0]["body"]


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


def test_the_linear_adapter_records_every_comment_it_writes(tmp_path, monkeypatch, redis_client):
    """Against the Linear mock: Symphony's comments and the agent's `linear_graphql` ones are
    recorded; a comment by the same account that Symphony did not write is not."""
    srv = linear_mock.serve(0, tmp_path / "state")
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("LINEAR_API_URL", f"{base}/graphql")
    try:
        linear = LinearTracker(
            api_key=DEFAULT_API_KEY, project_slug="", team_key="ENG", active_states=["Todo"],
            terminal_states=["Done"],
            on_comment=lambda cid: questions.record_own(redis_client, NS, cid))
        ticket = linear.create_issue("Layout", "", labels=[])["identifier"]
        note = linear.comment(ticket, "I will ask for an explicit decision here.")
        agent = linear.execute_agent_tool("linear_graphql", {
            "query": "mutation Note($input: CommentCreateInput!) "
                     "{ commentCreate(input: $input) { success comment { id } } }",
            "variables": {"input": {"issueId": linear._ticket(ticket)["id"], "body": "Progress."}},
        })["commentCreate"]["comment"]["id"]
        me = linear._post("query { viewer { id } }", {})["viewer"]["id"]
        same_account = httpx.post(f"{base}/_mock/issues/{ticket}/comments",
                                  json={"body": "Remove it", "as": me}).json()["id"]
        assert questions.is_own(redis_client, NS, note)
        assert questions.is_own(redis_client, NS, agent)
        assert not questions.is_own(redis_client, NS, same_account)
    finally:
        srv.shutdown()
        srv.server_close()


def test_every_process_tracker_records_symphonys_comments(redis_client, monkeypatch):
    """`edge.make_tracker`, which every process uses, records in the shared Redis."""
    path = redis_client.connection_pool.connection_kwargs["path"]
    monkeypatch.setenv("REDIS_URL", f"unix://{path}")
    tracker = edge.make_tracker(cfg(namespace=NS))
    tracker.add_issue("i1", "ENG-1")
    assert questions.is_own(redis_client, NS, tracker.comment("i1", "A report."))
