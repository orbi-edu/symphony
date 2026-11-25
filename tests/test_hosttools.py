"""Host tools served by the runner (internals §3): tracker access without the credential in
the workspace, and human questions that never stall the run."""

from __future__ import annotations

import threading
import time

import pytest
from tests.conftest import FakeKV

from symphony import questions
from symphony.hosttools import (
    ASK_FALLBACK,
    ASK_TOOL,
    BLOCKED_RESULT,
    BLOCKED_TOOL,
    HostToolBroker,
    tool_definitions,
)
from symphony.sessions import EV_INPUT_RESOLVED, SessionStore
from symphony.trackers.base import TrackerUnavailable
from symphony.trackers.memory import MemoryTracker


class Responses:
    def __init__(self):
        self.items: list[tuple[str, str, bool]] = []
        self.arrived = threading.Event()

    def __call__(self, call_id, text, *, is_error=False):
        self.items.append((call_id, text, is_error))
        self.arrived.set()


@pytest.fixture
def broker_factory(redis_client):
    def make(tracker=None, owns=lambda: True, timeout_s=5.0):
        store = SessionStore(redis_client, "test")
        store.open("i1:1", kind="implement", identifier="ENG-1")
        responses = Responses()
        broker = HostToolBroker(
            store=store, sid="i1:1", kv=FakeKV(), tracker=tracker or MemoryTracker(),
            issue_id="i1", seq=1, identifier="ENG-1", respond=responses, owns=owns,
            input_timeout_s=timeout_s,
        )
        return broker, store, responses

    return make


def ask_frame(call_id, question="Which?"):
    return {"type": "host_tool_call", "id": call_id, "toolCallId": f"t-{call_id}",
            "toolName": ASK_TOOL, "arguments": {"question": question}}


def wait_pending(broker, call_id):
    deadline = time.monotonic() + 5
    while broker.pending_request() != call_id and time.monotonic() < deadline:
        time.sleep(0.005)
    assert broker.pending_request() == call_id


def test_definitions_register_tracker_tools_and_the_ask_tool_top_level():
    tools = tool_definitions(MemoryTracker())
    assert [t["name"] for t in tools] == [
        "linear_graphql", "symphony_create_issues", "symphony_attach_evidence", ASK_TOOL,
        BLOCKED_TOOL,
    ]
    assert {t["loadMode"] for t in tools} == {"essential"}
    ask = tools[-2]["parameters"]
    assert ask["required"] == ["question"] and "options" in ask["properties"]
    blocked = tools[-1]["parameters"]
    assert blocked["required"] == ["reason", "needs"]
    assert blocked["properties"]["needs"]["enum"] == ["answer", "access", "decision", "other"]


def blocked_frame(call_id, **arguments):
    return {"type": "host_tool_call", "id": call_id, "toolCallId": f"t-{call_id}",
            "toolName": BLOCKED_TOOL, "arguments": arguments}


def test_symphony_blocked_records_the_wait_and_tells_the_agent_to_end_its_turn(broker_factory):
    broker, _, responses = broker_factory()
    broker.handle(blocked_frame("b1", reason="acme/tools returns 404 for this token",
                                needs="access"))
    assert responses.arrived.wait(5)
    assert responses.items == [("b1", BLOCKED_RESULT, False)]
    assert broker.blocked == {"reason": "acme/tools returns 404 for this token", "needs": "access"}


@pytest.mark.parametrize("arguments", [
    {"reason": "", "needs": "access"},
    {"reason": "waiting", "needs": "coffee"},
    {"reason": "x" * 501, "needs": "other"},
])
def test_symphony_blocked_refuses_what_a_person_could_not_act_on(broker_factory, arguments):
    broker, _, responses = broker_factory()
    broker.handle(blocked_frame("b2", **arguments))
    assert responses.arrived.wait(5)
    [(_, _, is_error)] = responses.items
    assert is_error and broker.blocked is None


def test_a_superseded_attempt_cannot_block_the_ticket(broker_factory):
    broker, _, responses = broker_factory(owns=lambda: False)
    broker.handle(blocked_frame("b3", reason="needs a decision", needs="decision"))
    assert responses.arrived.wait(5)
    assert responses.items[0][2] is True and broker.blocked is None


def test_an_answer_without_a_request_id_completes_the_pending_ask(broker_factory):
    broker, _, responses = broker_factory()
    broker.handle(ask_frame("h1"))
    wait_pending(broker, "h1")
    assert broker.answer(None, "yes", "alice") is True
    assert responses.arrived.wait(5)
    assert responses.items == [("h1", "yes", False)]


def test_a_late_answer_matches_nothing(broker_factory):
    broker, _, _ = broker_factory()
    assert broker.answer("h-gone", "too late", "alice") is False


def test_a_cancelled_ask_resolves_without_answering_the_engine(broker_factory):
    broker, store, responses = broker_factory()
    broker.handle(ask_frame("h2"))
    wait_pending(broker, "h2")
    broker.handle({"type": "host_tool_cancel", "id": "x", "targetId": "h2"})
    broker.close()
    assert responses.items == []  # omp withdrew the call; there is nobody to answer
    resolved = [d for _, k, d in store.read_events("i1:1") if k == EV_INPUT_RESOLVED]
    assert resolved == [{"id": "h2", "answer": None, "by": "cancelled"}]
    assert store.meta("i1:1")["state"] == "running"


def test_shutdown_releases_a_waiting_ask_at_once(broker_factory):
    broker, _, _ = broker_factory(timeout_s=60)
    broker.handle(ask_frame("h3"))
    wait_pending(broker, "h3")
    started = time.monotonic()
    broker.close()
    assert time.monotonic() - started < 2


def test_the_ask_timeout_answers_with_the_fallback(broker_factory):
    broker, _, responses = broker_factory(timeout_s=0.05)
    broker._serve(ask_frame("h4"))
    assert responses.items == [("h4", ASK_FALLBACK, False)]


def test_an_ask_outlives_its_timeout_and_names_who_decides(broker_factory, redis_client):
    broker, _, _ = broker_factory(timeout_s=0.05)
    broker._serve({**ask_frame("h8"), "arguments": {"question": "Which?", "to": "Victor"}})
    rec = questions.load(redis_client, "test", "h8")
    assert (rec["state"], rec["to"], rec["identifier"], rec["sid"]) == ("open", "Victor", "ENG-1",
                                                                      "i1:1")


@pytest.mark.parametrize(("arguments", "says"), [
    ({"question": "Which layout? " + "context " * 200}, "too long for a person to read on Slack"),
    ({"question": "Before I go on: (1) keep the table? (2) move the button? (3) drop CSV?"},
     "put each on its own line"),
    ({"question": "Which?", "options": ["a", "b", "c", "d", "e", "f"]}, "6 options, at most 5"),
    ({"question": "Which?", "options": ["Keep", "x" * 61]}, "an option is over 60 characters"),
])
def test_a_question_hard_to_read_on_slack_is_sent_back_to_the_agent(
        broker_factory, redis_client, arguments, says):
    broker, store, responses = broker_factory()
    broker._serve({**ask_frame("h10"), "arguments": arguments})
    [(call_id, text, is_error)] = responses.items
    assert call_id == "h10" and is_error and says in text
    assert questions.load(redis_client, "test", "h10") is None  # nothing was posted
    assert broker.pending_request() is None


def test_sub_questions_one_per_line_are_accepted(broker_factory, redis_client):
    broker, _, responses = broker_factory(timeout_s=0.05)
    question = "Which parts change?\n1. the table\n2. the button\n3. the CSV export"
    broker._serve({**ask_frame("h11"), "arguments": {"question": question,
                                                    "options": ["All", "Only the table"]}})
    assert responses.items == [("h11", ASK_FALLBACK, False)]
    assert questions.load(redis_client, "test", "h11")["question"] == question


def test_an_answer_in_tern_closes_the_question(broker_factory, redis_client):
    broker, _, responses = broker_factory()
    broker.handle(ask_frame("h9"))
    wait_pending(broker, "h9")
    broker.answer("h9", "yes", "alice")
    assert responses.arrived.wait(5)
    deadline = time.monotonic() + 5
    while questions.load(redis_client, "test", "h9")["state"] != "answered":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    rec = questions.load(redis_client, "test", "h9")
    assert (rec["answer"], rec["by"], rec["via"], rec["delivered"]) == ("yes", "alice",
                                                                       "symphony", "answer")


def test_an_empty_question_is_a_tool_error(broker_factory):
    broker, _, responses = broker_factory()
    broker._serve(ask_frame("h5", question="  "))
    assert responses.items[0][2] is True


def test_a_superseded_attempt_cannot_mutate_the_ticket(broker_factory):
    tracker = MemoryTracker()
    broker, _, responses = broker_factory(tracker=tracker, owns=lambda: False)
    broker._serve({"type": "host_tool_call", "id": "h6", "toolCallId": "t6",
                   "toolName": "linear_graphql", "arguments": {"query": "mutation"}})
    assert tracker.mutations == []
    assert responses.items[0][2] is True and "no longer owns" in responses.items[0][1]


def test_a_tracker_outage_is_a_retryable_tool_error_not_cached(broker_factory):
    class Down(MemoryTracker):
        calls = 0

        def execute_agent_tool(self, name, args):
            Down.calls += 1
            raise TrackerUnavailable("rate limited")

    broker, _, responses = broker_factory(tracker=Down())
    frame = {"type": "host_tool_call", "id": "h7", "toolCallId": "t7",
             "toolName": "linear_graphql", "arguments": {"query": "q"}}
    broker._serve(frame)
    broker._serve(frame)
    assert Down.calls == 2  # a failure is never served from the replay cache
    assert all(is_error and "rate limited" in text for _, text, is_error in responses.items)


def test_an_unknown_host_tool_is_an_error(broker_factory):
    broker, _, responses = broker_factory()
    broker._serve({"type": "host_tool_call", "id": "h8", "toolCallId": "t8",
                   "toolName": "rm_rf", "arguments": {}})
    assert responses.items == [("h8", "unknown host tool 'rm_rf'", True)]
