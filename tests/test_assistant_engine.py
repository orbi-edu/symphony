"""The assistant engine with a fake transport and a scripted fake agent (no model, no omp)."""

from __future__ import annotations

import threading
import time

import pytest

from symphony.assistant import HistoryMessage, Inbound, Ref, split_text
from symphony.assistant.confirm import Confirmer, Policy, ProposalStore
from symphony.assistant.engine import BUSY, WORKING, Engine, TurnResult, link_citations, mode
from symphony.config import parse_workflow


def eventually(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition never held")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeOutbound:
    def __init__(self, name="slack", message_limit=40_000) -> None:
        self.name = name
        self.message_limit = message_limit
        self.connected = True
        self.lock = threading.Lock()
        self.messages: dict[str, dict] = {}  # message id → {channel, thread, text, buttons}
        self.order: list[str] = []
        self.updates: list[tuple[str, str]] = []
        self.acks: list[tuple[object, str | None]] = []
        self.past: list[HistoryMessage] = []
        self._n = 0

    def post(self, channel, thread, text, buttons=()):
        assert len(text) <= self.message_limit
        with self.lock:
            self._n += 1
            mid = f"m{self._n}"
            self.messages[mid] = {"channel": channel, "thread": thread, "text": text,
                                  "buttons": list(buttons)}
            self.order.append(mid)
        return Ref(channel, mid)

    def update(self, ref, text, buttons=()):
        assert len(text) <= self.message_limit
        with self.lock:
            self.messages[ref.message_id].update(text=text, buttons=list(buttons))
            self.updates.append((ref.message_id, text))

    def ephemeral(self, channel, thread, user_id, text):
        pass

    def ack_action(self, action, text=None):
        self.acks.append((action, text))

    def history(self, channel, thread, limit):
        return list(self.past)[-limit:]

    def texts(self, thread=None) -> list[str]:
        with self.lock:
            return [self.messages[m]["text"] for m in self.order
                    if thread is None or self.messages[m]["thread"] == thread]


class FakeAgent:
    def __init__(self, factory, sid, role) -> None:
        self.factory, self.sid, self.role = factory, sid, role
        self.prompts: list[str] = []
        self.closed: str | None = None

    def ask(self, prompt, on_text, timeout_s):
        self.prompts.append(prompt)
        script = self.factory.scripts.pop(0) if self.factory.scripts else None
        if script is None:
            return TurnResult("completed", f"answer to: {prompt.splitlines()[-1]}")
        return script(self, prompt, on_text)

    def close(self, outcome):
        self.closed = outcome


class FakeAgents:
    def __init__(self) -> None:
        self.opened: list[FakeAgent] = []
        self.metas: list[dict] = []
        self.intents: list = []
        self.scripts: list = []
        self.lock = threading.Lock()

    def open(self, sid, role, meta, on_intent, tools=()):
        agent = FakeAgent(self, sid, role)
        agent.tools = {t.name: t for t in tools}
        with self.lock:
            self.opened.append(agent)
            self.metas.append(meta)
            self.intents.append(on_intent)
        return agent

    def commits(self):
        return {"acme/app": "abc123"}


WORKFLOW = """---
tracker: {kind: memory}
notify:
  users:
    slack: {alice: U1}
assistant:
  enabled: true
  slack: {channels: [C1]}
  max_concurrent: %d
---
x
"""


def build(redis_client, *, cap=3, out=None, clock=None, interval=1.5):
    cfg = parse_workflow(WORKFLOW % cap)
    out = out or FakeOutbound()
    agents = FakeAgents()
    store = ProposalStore(redis_client, "t")
    confirmer = Confirmer(store, tracker=lambda: None, audit=lambda *a: None,
                          policy=lambda: Policy({}, "Backlog", "Todo", (), ()))
    engine = Engine(outbounds={out.name: out}, agents=agents, policy=lambda: cfg,
                    confirmer=confirmer, clock=clock or Clock(), stream_interval_s=interval)
    return engine, out, agents


def msg(text, thread="t1", channel="C1", user="U1", n=[0], **kw):  # noqa: B006 — a counter
    n[0] += 1
    return Inbound("slack", channel, thread, f"in{n[0]}", user, text, **kw)


def answered(out, thread, count=1):
    return lambda: sum(1 for t in out.texts(thread) if t.startswith("answer to")) >= count


def test_mode_routes_to_the_role_and_strips_the_word(redis_client):
    engine, out, agents = build(redis_client)
    engine.on_message(msg("research how deploys work", thread="a"))
    engine.on_message(msg("plan: split the parser", thread="b"))
    engine.on_message(msg("what is ENG-1?", thread="c"))
    eventually(lambda: len(agents.opened) == 3 and all(a.prompts for a in agents.opened))
    roles = {a.sid.split(":")[3]: a for a in agents.opened}
    assert roles["a"].role == "researcher" and "U1" not in roles["a"].prompts[0]
    assert roles["a"].prompts[0].endswith("alice asks:\nhow deploys work")
    assert roles["b"].role == "planner" and roles["b"].prompts[0].endswith("split the parser")
    assert roles["c"].role == "assistant"
    assert mode("planet names") == (None, "planet names")
    meta = agents.metas[0]
    assert {"transport", "channel", "thread", "asker", "identifier", "title"} <= set(meta)
    assert meta["asker"] == "alice"


def test_a_failed_turn_is_logged_with_its_cause_but_not_the_conversation(redis_client):
    """The person only sees "I could not answer"; the log must say why (found on kind: a
    provider 403 left no trace outside the session)."""
    engine, out, agents = build(redis_client)
    logged = []

    class Log:
        def warning(self, event, **fields):
            logged.append((event, fields))

        info = warning

    engine.log = Log()
    agents.scripts.append(lambda agent, prompt, on_text: TurnResult(
        "failed", "", error="403 You don't have access to the model"))
    engine.on_message(msg("a secret question about payroll", thread="f"))
    eventually(lambda: any(t.startswith("I could not answer") for t in out.texts("f")))
    [(event, fields)] = [e for e in logged if e[0] == "assistant_turn_ended"]
    assert fields["status"] == "failed" and fields["role"] == "assistant"
    assert "403" in fields["error"]
    assert "payroll" not in repr(logged)


def test_a_follow_up_reuses_the_thread_session(redis_client):
    engine, out, agents = build(redis_client)
    engine.on_message(msg("plan the importer"))
    eventually(answered(out, "t1"))
    engine.on_message(msg("make the first ticket smaller"))
    eventually(answered(out, "t1", 2))
    assert len(agents.opened) == 1
    agent = agents.opened[0]
    assert agent.role == "planner"  # no mode word: the thread keeps its role
    assert agent.prompts[1] == "alice asks:\nmake the first ticket smaller"
    # a different mode word starts a new session for the new role
    engine.on_message(msg("research the old importer"))
    eventually(answered(out, "t1", 3))
    assert [a.role for a in agents.opened] == ["planner", "researcher"]
    assert agent.closed == "completed"


def test_an_idle_session_ends_and_the_next_message_gets_the_thread_as_context(redis_client):
    clock = Clock()
    engine, out, agents = build(redis_client, clock=clock)
    engine.on_message(msg("what does the gateway do?"))
    eventually(answered(out, "t1"))
    clock.t += 29 * 60
    assert engine.sweep() == 0
    clock.t += 2 * 60
    assert engine.sweep() == 1
    assert agents.opened[0].closed == "completed"
    assert engine.stats()["threads"] == 0
    out.past = [HistoryMessage("U2", "earlier question"), HistoryMessage("B", "earlier answer", True),
                HistoryMessage("U1", "and the API?")]
    engine.on_message(msg("and the API?"))
    eventually(lambda: len(agents.opened) == 2 and agents.opened[1].prompts)
    prompt = agents.opened[1].prompts[0]
    assert "U2: earlier question" in prompt and "you: earlier answer" in prompt
    assert prompt.count("and the API?") == 1  # the question is not repeated as context


def test_context_falls_back_to_what_the_engine_saw(redis_client):
    clock = Clock()
    engine, out, agents = build(redis_client, clock=clock)
    engine.on_message(msg("first question"))
    eventually(answered(out, "t1"))
    clock.t += 31 * 60
    engine.sweep()
    engine.on_message(msg("second question"))  # the transport has no history (Discord)
    eventually(lambda: len(agents.opened) == 2 and agents.opened[1].prompts)
    prompt = agents.opened[1].prompts[0]
    assert "U1: first question" in prompt and "you: answer to: first question" in prompt


def test_over_the_cap_it_says_busy_and_answers_later(redis_client):
    engine, out, agents = build(redis_client, cap=1)
    release = threading.Event()

    def slow(agent, prompt, on_text):
        release.wait(5)
        return TurnResult("completed", "slow answer")

    agents.scripts.append(slow)
    engine.on_message(msg("first", thread="a"))
    eventually(lambda: engine.stats()["active"] == 1)
    engine.on_message(msg("second", thread="b"))
    eventually(lambda: out.texts("b") == [BUSY])
    eventually(lambda: engine.stats()["queued"] == 1)
    assert len(agents.opened) == 1
    release.set()
    eventually(lambda: out.texts("b") == ["answer to: second"])
    assert out.texts("a") == ["slow answer"]
    b_updates = [text for mid, text in out.updates if out.messages[mid]["thread"] == "b"]
    assert b_updates[0] == WORKING  # the busy note turns into the answer's placeholder


def test_streaming_updates_are_throttled(redis_client):
    clock = Clock()
    engine, out, agents = build(redis_client, clock=clock)

    def stream(agent, prompt, on_text):
        for dt, text in ((0.5, "a"), (0.5, "ab"), (0.6, "abc"), (0.4, "abcd"), (1.2, "abcde")):
            clock.t += dt
            on_text(text)
        return TurnResult("completed", "abcdef")

    agents.scripts.append(stream)
    engine.on_message(msg("stream it"))
    eventually(lambda: out.texts("t1") == ["abcdef"])
    [placeholder] = out.order
    assert [t for mid, t in out.updates if mid == placeholder] == ["abc", "abcde", "abcdef"]


def test_long_answers_are_split_per_transport_limit(redis_client):
    out = FakeOutbound("discord", message_limit=50)
    engine, _, agents = build(redis_client, out=out)
    long = " ".join(f"word{i}" for i in range(30))  # 209 characters
    agents.scripts.append(lambda agent, prompt, on_text: TurnResult("completed", long))
    engine.on_message(Inbound("discord", "D1", "t1", "x", "U9", "explain", is_dm=True))
    eventually(lambda: len(out.texts("t1")) >= 5)
    parts = out.texts("t1")
    assert all(len(p) <= 50 for p in parts)
    assert " ".join(parts) == long
    assert parts == split_text(long, 50)


def test_only_allowed_channels_and_dms_are_answered(redis_client):
    engine, out, agents = build(redis_client)
    engine.on_message(msg("hello", channel="C9", thread="x"))
    engine.on_message(msg("hello", channel="D7", thread="y", is_dm=True))
    engine.on_message(msg("hello", channel="T5", thread="z", parent="C1"))
    eventually(answered(out, "y"))
    eventually(answered(out, "z"))
    time.sleep(0.05)
    assert out.texts("x") == []


def test_a_plan_is_posted_with_buttons_and_stored(redis_client):
    engine, out, agents = build(redis_client)
    proposal = {"summary": "Split the parser", "tickets": [
        {"key": "a", "title": "Extract the lexer", "body": "Move it."},
        {"key": "b", "title": "Add tests", "body": "Cover it.", "blocked_by": ["a"]},
    ]}
    agents.scripts.append(lambda agent, prompt, on_text: TurnResult("completed", "Here is the plan.", proposal))
    engine.on_message(msg("plan the parser split"))
    eventually(lambda: len(out.texts("t1")) == 2)
    plan = out.messages[out.order[-1]]
    assert "Extract the lexer" in plan["text"] and "after 'Extract the lexer'" in plan["text"]
    ids = [b.id for b in plan["buttons"]]
    pid = ids[0].split(":")[1]
    assert ids == [f"proposal:{pid}:create", f"proposal:{pid}:start", f"proposal:{pid}:dismiss"]
    stored = engine.confirmer.store.get(pid)
    assert stored["tickets"] == proposal["tickets"] and stored["channel"] == "C1"


def test_a_tern_follow_up_is_answered_in_the_thread(redis_client):
    engine, out, agents = build(redis_client)
    engine.on_message(msg("first"))
    eventually(answered(out, "t1"))
    agents.intents[0]("operator", "one more thing")
    eventually(answered(out, "t1", 2))
    assert "operator asked from Tern: one more thing" in out.texts("t1")
    assert agents.opened[0].prompts[-1] == "operator asks:\none more thing"


def test_code_citations_link_to_the_mirror_commit():
    text = "See `acme/app:src/main.py:12` and other/x:a.py:1."
    slack = link_citations(text, "slack", {"acme/app": "abc"}, "https://github.example")
    assert "<https://github.example/acme/app/blob/abc/src/main.py#L12|acme/app:src/main.py:12>" in slack
    assert "other/x:a.py:1" in slack  # unknown repository: left as written
    discord = link_citations(text, "discord", {"acme/app": "abc"}, "https://github.example")
    assert "[acme/app:src/main.py:12](<https://github.example/acme/app/blob/abc/src/main.py#L12>)" in discord


@pytest.mark.parametrize("limit", [10, 37, 100])
def test_split_text_keeps_every_word(limit):
    text = "alpha beta\ngamma " * 20
    parts = split_text(text, limit)
    assert all(len(p) <= limit for p in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")
