"""Answer runs: the runner half of the review watch.

Contract: specs/005-review-conversation-watch/spec.md

An answer run executes while the ticket sits in a state the agent does not own. Every test
here is about that being safe — it may speak, and it must give the state back exactly as it
found it.
"""

from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, runner_env

from symphony import prwatch, state
from symphony.config import parse_workflow
from symphony.orchestrator import PARKED
from symphony.runner import run_attempt
from symphony.trackers.memory import MemoryTracker

CFG = parse_workflow(
    "---\ntracker: {kind: memory}\nagent: {max_turns: 20}\n"
    "engine: {provider: acme, model: acme-1}\n"
    "review_watch:\n  enabled: true\n  answer_max_turns: 2\n"
    '  answer_prompt: "Answer the reviewer on {{ issue.identifier }}."\n'
    "---\nimplement {{ issue.identifier }}\n"
)


def make(redis_client, issue_state="In Review", mode=prwatch.ANSWER_MODE, runs=3, scripted=None):
    kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
    tracker.add_issue("i1", "ENG-1", state=issue_state)
    kv.put(
        state.issue_key("i1"),
        {
            "status": "claimed",
            "identifier": "ENG-1",
            "attempt": 2,
            "dispatch_seq": 5,
            "runs": runs,
            "tokens": {},
            "session_path": None,
            "failure_summary": None,
            "mode": mode,
            "session_reported": True,
            "target_repo": "acme/api",
        },
    )
    kv.put(state.dispatch_key("i1", 5), {"daemon_owner": "d1"})
    engine = FakeEngine()
    if scripted:
        scripted(engine)
    env = runner_env(
        redis_client, kv=kv, tracker=tracker, cfg=CFG, clock=clock, engine=engine, call_id="fc-9"
    )
    return env, kv, tracker, engine


def runs(n, **kw):
    return lambda engine: [engine.run(**kw) for _ in range(n)]


def prompts(engine):
    return [m["message"] for m in engine.commands("prompt")]


def test_an_answer_run_executes_in_a_parked_state(redis_client):
    """The implementation path exits immediately on a non-active state — that check is what
    made the reviewer's question unanswerable, and it must not apply here."""
    env, kv, _, engine = make(redis_client, scripted=runs(2))
    outcome = run_attempt(env, "i1", 2, 5)
    assert outcome != "done"
    assert prompts(engine), "the agent was never prompted"


def test_an_answer_run_is_published_as_an_answer_session(redis_client):
    env, _, _, _ = make(redis_client, scripted=runs(2))
    run_attempt(env, "i1", 2, 5)
    meta = env.sessions.meta("i1:5")
    assert meta["kind"] == "answer"
    assert meta["state"] == "ended"


def test_the_answer_prompt_is_used_not_the_implementation_prompt(redis_client):
    """FR-014 and the whole safety story: the implementation prompt tells the agent to open
    pull requests and move the ticket."""
    env, _, _, engine = make(redis_client, scripted=runs(2))
    run_attempt(env, "i1", 2, 5)
    assert prompts(engine)[0] == "Answer the reviewer on ENG-1."
    assert all("implement" not in p for p in prompts(engine))


def test_an_answer_run_is_bounded_by_its_own_turn_cap(redis_client):
    """FR-009: answer_max_turns is 2 here while agent.max_turns is 20."""
    env, _, _, engine = make(redis_client, scripted=runs(6))
    run_attempt(env, "i1", 2, 5)
    assert len(prompts(engine)) == 2


def test_a_finished_answer_run_returns_the_issue_to_parked(redis_client):
    """FR-011. Routing it through the clean-continuation path would queue it for retry and
    redispatch it into a state it must not work in — a loop under a reviewer."""
    env, kv, _, _ = make(redis_client, scripted=runs(2))
    run_attempt(env, "i1", 2, 5)
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == PARKED
    assert entry["mode"] is None
    assert entry["runner_call_id"] is None


def test_a_failed_answer_run_also_returns_to_parked_without_backoff(redis_client):
    """FR-010: an unanswered question must not burn implementation retry budget."""
    env, kv, _, _ = make(redis_client, scripted=runs(1, stop_reason="error", error="boom"))
    outcome = run_attempt(env, "i1", 2, 5)
    entry = kv.get(state.issue_key("i1"))
    assert outcome == "failed"
    assert entry["status"] == PARKED
    assert entry["attempt"] == 2, "attempts are for implementation, not for answering"
    assert entry.get("retry_at") is None


def test_the_answer_run_never_clears_the_handover_marker(redis_client):
    """SC-004 across the runner boundary too."""
    env, kv, _, _ = make(redis_client, scripted=runs(2))
    run_attempt(env, "i1", 2, 5)
    assert kv.get(state.issue_key("i1"))["session_reported"] is True


def test_rework_supersedes_an_answer_run(redis_client):
    """The reviewer moving the ticket outranks the question — the full implementation path
    takes over, and this run must get out of the way."""
    env, kv, _, engine = make(redis_client, issue_state="Rework", scripted=runs(2))
    assert run_attempt(env, "i1", 2, 5) == "done"
    assert prompts(engine) == []


def test_a_normal_run_is_unaffected_by_the_answer_path(redis_client):
    """FR-017/SC-005: mode absent ⇒ exactly the previous behaviour."""
    env, kv, _, engine = make(redis_client, issue_state="Todo", mode=None, scripted=runs(25))
    run_attempt(env, "i1", 2, 5)
    assert prompts(engine)[0].startswith("implement ENG-1")
    assert len(prompts(engine)) == 20  # agent.max_turns, not answer_max_turns


def test_an_answer_run_gets_the_prs_preview_links(redis_client):
    """Someone asks on the PR to open the app in Seiji: the agent answers with the build's
    link (the merger listed it, `preview_links`) instead of holding a device nobody sees."""
    from symphony import preview_links

    env, kv, _, engine = make(redis_client, scripted=runs(2))
    kv.put(state.issue_key("i1"), {**kv.get(state.issue_key("i1")),
                                   "pr_numbers": {"acme/api": 7}})
    builds = [{"sha": "b" * 40, "sha12": "b" * 12, "url": "https://symphony.example.com/new",
               "at": 2},
              {"sha": "a" * 40, "sha12": "a" * 12, "url": "https://symphony.example.com/old",
               "at": 1}]
    preview_links.save(env.sessions.client, env.sessions.ns, "acme/api", 7, None, builds)
    run_attempt(env, "i1", 2, 5)
    first = prompts(engine)[0]
    assert first.startswith("Answer the reviewer on ENG-1.")
    assert "## Preview links" in first
    assert (f"- acme/api#7 at `{'b' * 12}`: https://symphony.example.com/new\n"
            f"- acme/api#7 at `{'a' * 12}`: https://symphony.example.com/old") in first
    assert "reply with the link" in first


def test_an_answer_run_without_preview_builds_gets_no_links_section(redis_client):
    env, kv, _, engine = make(redis_client, scripted=runs(2))
    kv.put(state.issue_key("i1"), {**kv.get(state.issue_key("i1")),
                                   "pr_numbers": {"acme/api": 8}})
    run_attempt(env, "i1", 2, 5)
    assert prompts(engine)[0] == "Answer the reviewer on ENG-1."
