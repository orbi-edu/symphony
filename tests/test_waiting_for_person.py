"""A ticket whose agent cannot go on without a person waits for one instead of looping:
`symphony_blocked` ends the run at the turn boundary, the safety net ends runs whose
continuation turns change nothing, and the daemon keeps the ticket parked until a person
acts (concepts § Waiting for a person)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, runner_env
from tests.test_dispatch import FakeEffects
from tests.test_dispatch import cfg as daemon_cfg

from symphony import admin_read, questions, state
from symphony.config import ConfigError, parse_workflow
from symphony.kv import RedisKV
from symphony.orchestrator import BLOCKED_ON_PERSON, tick
from symphony.runner import _acts, _finish, run_attempt
from symphony.trackers.memory import MemoryTracker

CFG = parse_workflow(
    "---\ntracker: {kind: memory}\nagent: {max_turns: 10}\n"
    "engine: {provider: acme, model: acme-1}\n---\nwork on {{ issue.identifier }}\n"
)
TRACKER_TOOLS = frozenset({"linear_graphql"})


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).isoformat().replace("+00:00", "Z")


class Workspace:
    """A sandbox whose git state the test moves: the fingerprint script reads `version`."""

    name = "ws-ENG-1"
    id = "sb-1"

    def __init__(self) -> None:
        self.version = 0

    def exec_script(self, script, timeout_s=None):
        if "cksum" in script:
            return 0, f"{1000 + self.version} 42\n"
        return 0, ""

    def commit(self) -> None:
        self.version += 1


@pytest.fixture
def attempt(redis_client):
    def make(cfg=CFG, **entry):
        kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
        tracker.add_issue("i1", "ENG-1", state="Todo", labels=("repo:acme/app",))
        kv.put(state.issue_key("i1"), {
            "status": "claimed", "identifier": "ENG-1", "attempt": 1, "dispatch_seq": 1,
            "runs": 1, "tokens": {}, "session_path": None, "failure_summary": None, **entry,
        })
        kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "d1"})
        engine, sandbox = FakeEngine(), Workspace()
        env = runner_env(redis_client, kv=kv, tracker=tracker, cfg=cfg, clock=clock,
                         engine=engine, sandbox=sandbox)
        return env, kv, engine, sandbox

    return make


def tool(name, **args):
    return {"type": "tool_execution_start", "toolCallId": f"t-{name}", "toolName": name,
            "args": args}


def side_effect(fn):
    def gate(_engine):
        fn()
        return True
    return gate


# -- symphony_blocked ------------------------------------------------------------------
def test_symphony_blocked_ends_the_run_at_the_turn_boundary(attempt):
    env, kv, engine, _ = attempt()
    engine.push({"type": "host_tool_call", "id": "b1", "toolCallId": "tc-b1",
                 "toolName": "symphony_blocked",
                 "arguments": {"reason": "acme/tools returns 404 for this token", "needs": "access"}})
    engine.push(lambda e: any(c["id"] == "b1" for c in e.commands("host_tool_result")))
    engine.run(text="I need access to acme/tools.")
    engine.run()  # a next turn that must never be prompted

    assert run_attempt(env, "i1", 1, 1) == "blocked"
    assert engine.prompted == 1
    entry = kv.get(state.issue_key("i1"))
    assert (entry["status"], entry["blocked_reason"], entry["blocked_needs"]) == (
        "blocked", BLOCKED_ON_PERSON, "access")
    assert entry["blocked_detail"] == "acme/tools returns 404 for this token"
    assert entry["attempt"] == 1 and not entry.get("failures") and entry["retry_at"] is None
    assert env.sessions.meta("i1:1")["outcome"] == "blocked"


# -- the safety net ----------------------------------------------------------------------
def test_idle_continuation_turns_end_the_run_as_blocked(attempt):
    env, kv, engine, _ = attempt()
    for _ in range(10):
        engine.push(tool("read", path="WORKPAD.md"))
        engine.push(tool("bash", command="git status --porcelain && git log -3 2>&1 | head"))
        engine.push(tool("linear_graphql", query="query { issue(id: \"i1\") { title } }"))
        engine.run(text="Still blocked: needs repo access.")

    assert run_attempt(env, "i1", 1, 1) == "blocked"
    # turn 1 is the dispatch's own start; turns 2-4 are the three idle continuations
    assert engine.prompted == 4
    entry = kv.get(state.issue_key("i1"))
    assert entry["blocked_reason"] == BLOCKED_ON_PERSON and entry["blocked_needs"] == "other"
    assert entry["blocked_detail"] == "no progress in 3 continuation turns"


def test_continuations_that_commit_are_not_cut(attempt):
    env, _, engine, sandbox = attempt()
    for _ in range(10):
        engine.push(side_effect(sandbox.commit))
        engine.run()

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert engine.prompted == 10  # agent.max_turns, not the safety net


def test_continuations_that_act_beyond_reads_are_not_cut(attempt):
    env, _, engine, _ = attempt()
    actions = [tool("edit", path="a.py"), tool("bash", command="git push origin HEAD"),
               tool("linear_graphql", query="mutation { commentCreate(input: {}) { success } }"),
               tool("symphony_ask", question="pg?")]
    for turn in range(10):
        engine.push(actions[turn % len(actions)])
        engine.run()

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert engine.prompted == 10


def test_the_idle_count_goes_on_across_continuation_dispatches(attempt):
    env, kv, engine, _ = attempt(cfg=replace(CFG, max_turns=1), idle_continuations=2)
    engine.run()
    assert run_attempt(env, "i1", 1, 1) == "blocked"
    assert kv.get(state.issue_key("i1"))["blocked_detail"] == "no progress in 3 continuation turns"


def test_a_fresh_dispatch_starts_counting_from_its_first_continuation(attempt):
    env, kv, engine, _ = attempt(cfg=replace(CFG, max_turns=1))
    engine.run()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == "retry_queued" and entry["idle_continuations"] == 0


def test_the_safety_net_can_be_turned_off(attempt):
    env, _, engine, _ = attempt(cfg=replace(CFG, max_idle_continuations=0))
    for _ in range(10):
        engine.run()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert engine.prompted == 10


@pytest.mark.parametrize(("frame", "acts"), [
    (tool("read", path="a"), False),
    (tool("grep", pattern="x"), False),
    (tool("bash", command="cd repos/web && git -C . status && ls -la 2>/dev/null | wc -l"), False),
    (tool("bash", command="gh pr view 12 --json state; gh pr checks 12"), False),
    (tool("linear_graphql", query="# who\n query { viewer { id } }"), False),
    (tool("linear_graphql", query="mutation { issueUpdate(id: \"i1\") { success } }"), True),
    (tool("bash", command="git status && git commit -am wip"), True),
    (tool("bash", command="grep -rl x . | xargs sed -i s/x/y/"), True),
    (tool("write", path="a"), True),
    ({"type": "message_end"}, False),
])
def test_what_counts_as_a_read(frame, acts):
    assert _acts(frame, TRACKER_TOOLS) is acts


def test_the_workflow_key_defaults_to_three_and_refuses_a_negative_count():
    assert CFG.max_idle_continuations == 3
    on = parse_workflow("---\ntracker: {kind: memory}\nagent: {max_idle_continuations: 5}\n---\nx")
    assert on.max_idle_continuations == 5
    with pytest.raises(ConfigError, match="max_idle_continuations"):
        parse_workflow("---\ntracker: {kind: memory}\nagent: {max_idle_continuations: -1}\n---\nx")


# -- the daemon: parked until a person acts ---------------------------------------------
def _blocked_ticket(kv, clock, tracker, c, needs="answer"):
    """Dispatch the ticket, then end its run the way the runner does on `symphony_blocked`."""
    tracker.add_issue("i1", "ENG-1", labels=("repo:acme/app",), updated_at=iso(clock.now_ms()),
                      url="https://tracker.example.com/ENG-1")
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 1)]
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="running", runner_call_id="fc-1", blocked_needs=needs)
    kv.put(state.issue_key("i1"), entry)
    issue = tracker.fetch_issues_by_ids(["i1"])[0]
    runner = SimpleNamespace(kv=kv, cfg=c, now_ms=clock.now_ms)
    assert _finish(runner, "i1", "blocked", issue, "which database: pg or sqlite?") == "blocked"


def _quiet_ticks(kv, clock, tracker, c, n=3):
    for _ in range(n):
        clock.advance(60_000)
        fx = FakeEffects()
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
        assert fx.spawned == [] and getattr(fx, "reported", []) == []


def test_a_blocked_ticket_is_reported_once_and_never_redispatched(kv, clock, tracker):
    c = daemon_cfg()
    _blocked_ticket(kv, clock, tracker, c)

    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert fx.spawned == []
    [(issue_id, comment)] = fx.reported
    assert issue_id == "i1" and "waiting for a person" in comment
    assert "an answer: which database: pg or sqlite?" in comment
    assert "blocked_on_person" in fx.names()
    # The comment's own echo moves the ticket's updated_at: not a person.
    tracker.set_state("i1", "Todo", updated_at=iso(clock.now_ms() + 2_000))
    _quiet_ticks(kv, clock, tracker, c)
    assert kv.get(state.issue_key("i1"))["status"] == "blocked"


@pytest.mark.parametrize("touch", ["comment", "label"])
def test_a_person_touching_the_ticket_redispatches_it_once(kv, clock, tracker, touch):
    c = daemon_cfg()
    _blocked_ticket(kv, clock, tracker, c)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # reports
    clock.advance(300_000)
    if touch == "comment":
        tracker.set_state("i1", "Todo", updated_at=iso(clock.now_ms()))
    else:
        tracker.add_issue("i1", "ENG-1", labels=("repo:acme/app", "backend"),
                          updated_at=iso(clock.now_ms()), url="https://tracker.example.com/ENG-1")

    spawned = []
    for _ in range(3):
        fx = FakeEffects()
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
        spawned += fx.spawned
        clock.advance(1_000)  # inside the orphan-claim window: the spawned runner is starting
    assert spawned == [("i1", 1)]  # the same attempt: waiting is not a failure
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == "claimed" and entry["blocked_reason"] is None


def test_an_answer_to_its_question_redispatches_it_once(kv, clock, tracker, redis_client):
    c = daemon_cfg()
    _blocked_ticket(kv, clock, tracker, c)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # reports
    questions.ask(redis_client, "test", qid="q1", issue_id="i1", identifier="ENG-1", sid="i1:1",
                  url=None, question="pg or sqlite?", options=["pg", "sqlite"], to=None,
                  now_ms=clock.now_ms())
    clock.advance(60_000)
    # Answered in Slack: nothing on the ticket changes.
    won, _ = questions.submit(redis_client, "test", "q1", "pg", by="alice", via="slack",
                              now_ms=clock.now_ms())
    assert won
    questions.tick(redis_client, "test", kv=kv, tracker=tracker, cfg=c, now_ms=clock.now_ms())
    assert questions.load(redis_client, "test", "q1")["delivered"] == "queued"

    spawned = []
    for _ in range(3):
        fx = FakeEffects()
        tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
        spawned += fx.spawned
        clock.advance(1_000)
    assert spawned == [("i1", 1)]


def test_the_wait_survives_a_daemon_restart(redis_client, clock, tracker):
    c = daemon_cfg()
    _blocked_ticket(RedisKV(redis_client), clock, tracker, c)
    tick(RedisKV(redis_client), tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # reports

    # A new daemon process: a new store client and new effects, the same Redis.
    restarted = RedisKV(redis_client)
    _quiet_ticks(restarted, clock, tracker, c)
    entry = restarted.get(state.issue_key("i1"))
    assert (entry["status"], entry["blocked_reason"]) == ("blocked", BLOCKED_ON_PERSON)
    assert entry["blocked_reported_at"] is not None


def test_the_api_shows_blocked_on_person_with_the_reason(kv, clock, tracker):
    c = daemon_cfg()
    _blocked_ticket(kv, clock, tracker, c, needs="decision")
    entry = kv.get(state.issue_key("i1"))
    assert admin_read._status(entry) == "blocked_on_person"
    assert admin_read._run("i1", entry)["status"] == "blocked_on_person"
    assert admin_read._blocked(entry) == {"needs": "decision",
                                          "reason": "which database: pg or sqlite?",
                                          "at": iso(clock.now_ms())}
    exhausted = dict(entry, blocked_reason="retries_exhausted")
    assert admin_read._status(exhausted) == "blocked" and admin_read._blocked(exhausted) is None
