"""The attempt as a live session (internals §2–§4): what people following it see, and how
they steer it — events, transcript, intents applied at once, host tools, team config.

Every test drives the real runner core against the responsive fake engine and a real
disposable Redis, because the behaviors here are cross-thread: the intent pump writes to
omp while the turn loop is blocked reading it.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from dataclasses import replace

import pytest
from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, FakeSandbox, aborted_run, runner_env

from symphony import discovery, questions, state
from symphony.config import parse_workflow
from symphony.hosttools import _SECRET_REFUSAL, ASK_FALLBACK, _is_secret_request
from symphony.runner import HUMAN_MESSAGE_HEADER, run_attempt
from symphony.sessions import EV_INPUT_REQUESTED, EV_INPUT_RESOLVED, EV_INTENT_APPLIED, EV_STATE
from symphony.trackers.memory import MemoryTracker

SID = "i1:1"
CFG = parse_workflow(
    "---\ntracker: {kind: memory}\nagent: {max_turns: 1}\n"
    "engine: {provider: acme, model: acme-1}\n---\nwork on {{ issue.identifier }}\n"
)


def asked(env) -> list[dict]:
    """The questions this attempt recorded for the daemon to post (`questions.py`)."""
    client, ns = env.sessions.client, env.sessions.ns
    return [questions.load(client, ns, qid) for qid in client.zrange(f"sym:{ns}:questions", 0, -1)]


@pytest.fixture
def attempt(redis_client):
    def make(cfg=CFG, tracker=None, **overrides):
        kv, clock = FakeKV(), FakeClock()
        tracker = tracker or MemoryTracker()
        tracker.add_issue(
            "i1", "ENG-1", state="Todo", url="https://linear.app/acme/issue/ENG-1",
            labels=("repo:acme/api", "repo:acme/web"),
        )
        kv.put(
            state.issue_key("i1"),
            {"status": "claimed", "identifier": "ENG-1", "attempt": 1, "dispatch_seq": 1,
             "runs": 1, "tokens": {}, "session_path": None, "failure_summary": None},
        )
        kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "d1"})
        engine = FakeEngine()
        env = runner_env(
            redis_client, kv=kv, tracker=tracker, cfg=cfg, clock=clock, engine=engine, **overrides
        )
        return env, kv, engine

    return make


def events(env, kind=None):
    rows = env.sessions.read_events(SID)
    return [(k, d) for _, k, d in rows if kind is None or k == kind]


def rpc_kinds(env):
    return [k for k, _ in events(env) if not k.startswith("symphony.")]


def wait_sent(kind, **match):
    """Gate: the stream pauses until the runner has sent `kind` (matching fields)."""
    def gate(engine):
        return any(all(cmd.get(k) == v for k, v in match.items()) for cmd in engine.commands(kind))
    return gate


def side_effect(fn):
    """Gate that runs `fn` once, from inside the turn, and lets the stream continue."""
    def gate(engine):
        fn()
        return True
    return gate


# -- events, meta, transcript -------------------------------------------------------
def test_every_event_is_published_in_order_and_ui_noise_is_dropped(attempt):
    env, _, engine = attempt()
    script = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Hi"}},
        {"type": "extension_ui_request", "id": "u1", "method": "setWidget", "widgetKey": "autoresearch"},
        {"type": "extension_ui_request", "id": "u2", "method": "notify", "message": "fyi"},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"},
        {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash"},
        {"type": "turn_end"},
    ]
    for frame in script:
        engine.push(frame)
    engine.run(text="Opened https://github.com/acme/web/pull/42 for review.")

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert rpc_kinds(env) == [
        "agent_start", "message_update", "extension_ui_request", "tool_execution_start",
        "tool_execution_end", "turn_end", "message_end", "agent_end",
    ]
    assert all(d.get("method") != "setWidget" for k, d in events(env, "extension_ui_request"))
    assert not events(env, "response"), "command responses are not session events"

    meta = env.sessions.meta(SID)
    assert meta["kind"] == "implement" and meta["identifier"] == "ENG-1"
    assert meta["repo"] == "acme/api" and meta["pod"] == "ws-ENG-1"
    assert meta["turn"] == 1
    assert meta["tokens"] == {"input": 10, "output": 5, "total": 15, "cost": 0.01}
    assert meta["last_message"].startswith("Opened https://github.com/acme/web/pull/42")
    assert meta["pr_url"] == "https://github.com/acme/web/pull/42"
    assert meta["state"] == "ended" and meta["outcome"] == "continuation"
    states = [d["state"] for k, d in events(env, EV_STATE)]
    assert states == ["starting", "running", "ended"]


def test_the_pr_link_follows_the_configured_github_server(attempt, monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://ghe.example.com")
    env, _, engine = attempt()
    engine.run(text="Opened https://github.com/acme/web/pull/7 and https://ghe.example.com/acme/web/pull/42.")
    run_attempt(env, "i1", 1, 1)
    assert env.sessions.meta(SID)["pr_url"] == "https://ghe.example.com/acme/web/pull/42"


def test_the_last_message_is_capped_for_list_views(attempt):
    env, _, engine = attempt()
    engine.run(text="x" * 1000)
    run_attempt(env, "i1", 1, 1)
    assert len(env.sessions.meta(SID)["last_message"]) == 280


def test_transcript_syncs_incrementally_at_each_turn_end(attempt):
    env, _, engine = attempt(cfg=replace(CFG, max_turns=2))
    first = [{"id": "e1", "type": "message"}, {"id": "e2", "type": "message"}]
    second = [{"id": "e3", "type": "message"}]
    engine.push(side_effect(lambda: engine.entries.extend(first)))
    engine.push({"type": "turn_end"})
    engine.run()
    engine.push(side_effect(lambda: engine.entries.extend(second)))
    engine.push({"type": "turn_end"})
    engine.run()

    run_attempt(env, "i1", 1, 1)
    assert env.sessions.transcript(SID) == first + second  # nothing twice, nothing missing
    cursors = [cmd.get("since") for cmd in engine.commands("get_entries")]
    assert cursors[0] is None
    assert "e2" in cursors and cursors[-1] == "e3"


# -- human intents --------------------------------------------------------------------
def test_a_steer_sent_mid_turn_reaches_omp_before_the_turn_ends(attempt):
    env, _, engine = attempt()
    sent_at = {}

    def steer():
        sent_at["t"] = time.monotonic()
        env.sessions.send_intent(SID, "steer", actor="alice", text="Also add a test.")

    engine.push({"type": "agent_start"})
    engine.push({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"})
    engine.push(side_effect(steer))
    engine.push(wait_sent("steer", message="Also add a test."))
    engine.push(side_effect(lambda: sent_at.setdefault("arrived", time.monotonic())))
    engine.push({"type": "turn_end"})
    engine.run()

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert engine.commands("steer")[0]["message"] == "Also add a test."
    assert sent_at["arrived"] - sent_at["t"] < 1.0
    (_, applied), = events(env, EV_INTENT_APPLIED)
    assert applied["kind"] == "steer" and applied["delivered"] == "steer"


def test_human_messages_reach_omp_within_the_latency_target(attempt):
    """internals §6: human message → omp in < 300 ms, measured as p95 over 25 steers sent
    while a turn runs (intent written to Redis → frame received by the engine)."""
    env, _, engine = attempt()
    written: dict[str, float] = {}

    def steer(n):
        def send():
            written[f"m{n}"] = time.monotonic()
            env.sessions.send_intent(SID, "steer", actor="alice", text=f"m{n}")
        return send

    engine.push({"type": "agent_start"})
    for n in range(25):
        engine.push(side_effect(steer(n)))
        engine.push(wait_sent("steer", message=f"m{n}"))
    engine.push({"type": "turn_end"})
    engine.run()

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    arrived = {
        cmd["message"]: at
        for cmd, at in zip(engine.sent, engine.arrived_at, strict=True)
        if cmd.get("type") == "steer"
    }
    latencies = sorted(arrived[key] - written[key] for key in written)
    assert len(latencies) == 25
    p95 = statistics.quantiles(latencies, n=20)[18]
    assert p95 < 0.3, f"p95 {p95 * 1000:.1f} ms"


def test_messages_written_before_the_first_prompt_ride_with_it(attempt):
    """Someone writes while the workspace is still starting: the message is not lost and
    does not race the runner's prompt — it is part of it."""
    env, _, engine = attempt()
    env.sessions.send_intent(SID, "follow_up", actor="alice", text="Prefer small commits.")
    engine.run()
    run_attempt(env, "i1", 1, 1)
    (prompt,) = [cmd["message"] for cmd in engine.commands("prompt")]
    assert prompt == f"work on ENG-1\n\n{HUMAN_MESSAGE_HEADER}\nPrefer small commits."
    assert engine.commands("follow_up") == []
    (_, applied), = events(env, EV_INTENT_APPLIED)
    assert applied["delivered"] == "prompt"


def test_interrupt_aborts_the_run_and_parks_until_nobody_writes(attempt):
    env, _, engine = attempt(cfg=replace(CFG, input_timeout_ms=200))
    engine.on_abort = aborted_run
    engine.push({"type": "agent_start"})
    engine.push({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"})
    engine.push(side_effect(lambda: env.sessions.send_intent(SID, "interrupt", actor="alice", reason="wrong file")))
    engine.push(wait_sent("abort"))
    engine.run()  # never reached: the abort ends the run

    started = time.monotonic()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert time.monotonic() - started < 3
    assert engine.commands("abort")
    states = [d["state"] for _, d in events(env, EV_STATE)]
    assert "idle" in states
    (_, applied), = events(env, EV_INTENT_APPLIED)
    assert applied["kind"] == "interrupt"


def test_after_an_interrupt_the_humans_message_is_the_next_prompt(attempt):
    env, _, engine = attempt(cfg=replace(CFG, max_turns=2, input_timeout_ms=5000))
    engine.on_abort = aborted_run
    engine.push({"type": "agent_start"})
    engine.push(side_effect(lambda: env.sessions.send_intent(SID, "interrupt", actor="alice", reason="stop")))
    engine.push(wait_sent("abort"))
    engine.run()
    engine.run(text="ok, using the other file")  # the human-driven turn

    def write_when_idle():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            meta = env.sessions.meta(SID) or {}
            if meta.get("state") == "idle":
                env.sessions.send_intent(SID, "follow_up", actor="alice", text="Edit b.py instead.")
                return
            time.sleep(0.01)

    helper = threading.Thread(target=write_when_idle)
    helper.start()
    started = time.monotonic()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    helper.join()
    assert time.monotonic() - started < 3
    prompts = [cmd["message"] for cmd in engine.commands("prompt")]
    assert prompts[1] == "Edit b.py instead."
    assert engine.commands("follow_up") == []  # held while parked, delivered as the prompt


def test_work_on_a_public_repository_never_names_the_ticket(attempt):
    env, kv, engine = attempt(cfg=replace(CFG, max_turns=1))
    kv.put(discovery.DISCOVERY_KEY, {"items": [{"name": "acme/web", "private": False},
                                               {"name": "acme/api", "private": True}]})
    engine.run()
    run_attempt(env, "i1", 1, 1)
    [first] = [cmd["message"] for cmd in engine.commands("prompt")]
    public = first.split("## Public repositories:", 1)[1]
    assert public.startswith(" `acme/web`\n") and "acme/api" not in public
    assert "Never write `ENG-1`" in public and "`symphony/i1`" in public


def test_intents_for_another_attempt_are_ignored(attempt):
    env, _, engine = attempt()
    env.sessions.send_intent(SID, "steer", actor="alice", text="stale", dispatch_seq=7)

    def recorded(_engine):
        return bool(events(env, EV_INTENT_APPLIED))

    engine.push({"type": "agent_start"})
    engine.push(recorded)
    engine.run()
    run_attempt(env, "i1", 1, 1)
    (_, applied), = events(env, EV_INTENT_APPLIED)
    assert applied["ignored"] is True and applied["kind"] == "steer"
    assert engine.commands("steer") == []


def test_intents_after_losing_ownership_are_ignored(attempt):
    env, kv, engine = attempt()

    def supersede():
        entry = dict(kv.get(state.issue_key("i1")))
        entry["dispatch_seq"] = 2
        kv.data[state.issue_key("i1")] = entry  # a successor took over (raw write)
        env.sessions.send_intent(SID, "steer", actor="alice", text="too late")

    def recorded(_engine):
        return bool(events(env, EV_INTENT_APPLIED))

    engine.push({"type": "agent_start"})
    engine.push(side_effect(supersede))
    engine.push(recorded)
    engine.run()
    assert run_attempt(env, "i1", 1, 1) == "fenced"
    (_, applied), = events(env, EV_INTENT_APPLIED)
    assert applied["ignored"] is True
    assert engine.commands("steer") == []


# -- host tools ---------------------------------------------------------------------
def ask(call_id="h1", question="Postgres or SQLite?", options=("pg", "sqlite")):
    return {
        "type": "host_tool_call", "id": call_id, "toolCallId": f"tc-{call_id}",
        "toolName": "symphony_ask", "arguments": {"question": question, "options": list(options)},
    }


def tool_results(engine, call_id):
    return [c for c in engine.commands("host_tool_result") if c["id"] == call_id]


def test_startup_registers_host_tools_delta_events_and_no_ask_dialog(attempt):
    env, _, engine = attempt()
    engine.run()
    run_attempt(env, "i1", 1, 1)
    (tools,) = engine.commands("set_host_tools")
    names = [t["name"] for t in tools["tools"]]
    assert names == [
        "linear_graphql", "symphony_create_issues", "symphony_attach_evidence", "symphony_ask"
    ]
    assert all(t["loadMode"] == "essential" for t in tools["tools"])
    assert engine.commands("set_event_filter")[0] | {"id": None} == {
        "id": None, "type": "set_event_filter", "events": None, "messageUpdates": "delta",
    }
    assert engine.commands("set_ask_dialog")[0]["enabled"] is False
    order = engine.sent_types()
    assert order.index("set_host_tools") < order.index("prompt")


@pytest.mark.parametrize("enabled", [True, False])
def test_startup_applies_the_workflow_auto_retry_setting(attempt, enabled):
    env, _, engine = attempt(cfg=replace(CFG, auto_retry=enabled))
    engine.run()
    run_attempt(env, "i1", 1, 1)
    assert [c["enabled"] for c in engine.commands("set_auto_retry")] == [enabled]


def test_an_answer_completes_the_pending_symphony_ask(attempt):
    env, _, engine = attempt()

    def answer_when_asked(_engine):
        meta = env.sessions.meta(SID) or {}
        if (meta.get("input_request") or {}).get("id") == "h1":
            env.sessions.send_intent(SID, "answer", actor="alice", request_id="h1", text="pg")
            return True
        return False

    engine.push({"type": "agent_start"})
    engine.push(ask())
    engine.push(answer_when_asked)
    engine.push(lambda e: bool(tool_results(e, "h1")))
    engine.run()

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    (result,) = tool_results(engine, "h1")
    assert result["result"]["content"][0]["text"] == "pg" and result["isError"] is False
    (_, requested), = events(env, EV_INPUT_REQUESTED)
    assert requested["question"] == "Postgres or SQLite?" and requested["options"] == ["pg", "sqlite"]
    (_, resolved), = events(env, EV_INPUT_RESOLVED)
    assert resolved == {"id": "h1", "answer": "pg", "by": "alice"}
    states = [d["state"] for _, d in events(env, EV_STATE)]
    assert states[:4] == ["starting", "running", "waiting_input", "running"]
    [question] = asked(env)
    assert (question["question"], question["options"], question["url"]) == (
        "Postgres or SQLite?", ["pg", "sqlite"], "https://linear.app/acme/issue/ENG-1")
    assert (question["state"], question["answer"], question["via"]) == ("answered", "pg", "symphony")


def test_an_unanswered_ask_returns_the_fallback_and_never_stalls(attempt):
    env, _, engine = attempt(cfg=replace(CFG, input_timeout_ms=150))
    engine.push(ask())
    engine.push(lambda e: bool(tool_results(e, "h1")))
    engine.run()

    started = time.monotonic()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert time.monotonic() - started < 3
    (result,) = tool_results(engine, "h1")
    assert result["result"]["content"][0]["text"] == ASK_FALLBACK
    (_, resolved), = events(env, EV_INPUT_RESOLVED)
    assert resolved["by"] == "timeout" and resolved["answer"] is None
    assert env.sessions.meta(SID)["input_request"] is None
    assert [q["state"] for q in asked(env)] == ["open"]  # still answerable on the ticket



# -- secret-request guard -----------------------------------------------------------

@pytest.mark.parametrize("question", [
    "Or make dev credentials available here (AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN)?",
    "Ou disponibilizar credenciais de dev aqui?",
    "Can you share the Stripe API key?",
    "Qual é a senha do banco? Pode me passar?",
    "Please paste the AWS_SECRET_ACCESS_KEY so I can run the test.",
    "Can you provide the database password?",
    "Send me the private key for the deploy service.",
    "What is the database password?",
    "Qual é a senha do banco?",
])
def test_ask_refuses_secret_requests(attempt, question):
    env, _, engine = attempt()
    engine.push(ask(question=question))
    engine.push(lambda e: bool(tool_results(e, "h1")))
    engine.run()

    run_attempt(env, "i1", 1, 1)
    (result,) = tool_results(engine, "h1")
    assert result["isError"] is True
    assert result["result"]["content"][0]["text"] == _SECRET_REFUSAL
    # Nothing stored, nothing asked.
    assert events(env, EV_INPUT_REQUESTED) == []
    assert asked(env) == []


@pytest.mark.parametrize("question", [
    "Can someone with access to the dev account run `node scripts/sample.js` on branch x and paste the output on the PR?",
    "Alguém com acesso pode rodar o script e colar a saída no ticket?",
    "Should the API return 401 when the session token is expired?",
    "Which database should we use?",
    "The deploy failed — can someone check the logs?",
    "Can someone run the token refresh job and post the output on the ticket?",
    "Which column should hold the password hash, `hash` or `secret_hash`?",
])
def test_ask_allows_non_secret_questions(attempt, question):
    env, _, engine = attempt(cfg=replace(CFG, input_timeout_ms=150))
    engine.push(ask(question=question))
    engine.push(lambda e: bool(tool_results(e, "h1")))
    engine.run()

    run_attempt(env, "i1", 1, 1)
    (result,) = tool_results(engine, "h1")
    # Not refused — gets the timeout fallback since nobody answers.
    assert result["isError"] is False
    assert result["result"]["content"][0]["text"] == ASK_FALLBACK


def test_ask_refuses_a_secret_request_hidden_in_the_options(attempt):
    env, _, engine = attempt()
    engine.push(ask(question="How should I unblock the deploy?", options=["Paste AWS_SECRET_ACCESS_KEY here"]))
    engine.push(lambda e: bool(tool_results(e, "h1")))
    engine.run()

    run_attempt(env, "i1", 1, 1)
    (result,) = tool_results(engine, "h1")
    assert result["isError"] is True
    assert events(env, EV_INPUT_REQUESTED) == []
    assert asked(env) == []


def test_is_secret_request_unit():
    """Direct unit tests for the detection function edge cases."""
    # Must refuse
    assert _is_secret_request("AWS_ACCESS_KEY_ID") is True
    assert _is_secret_request("AWS_SESSION_TOKEN") is True
    assert _is_secret_request("share the credentials") is True
    assert _is_secret_request("forneça a senha") is True
    assert _is_secret_request("STRIPE_API_KEY is needed") is True
    assert _is_secret_request("give me the access key") is True
    assert _is_secret_request("compartilhar credenciais") is True
    # Must allow
    assert _is_secret_request("paste the output on the PR") is False
    assert _is_secret_request("colar a saída no ticket") is False
    assert _is_secret_request("session token is expired") is False
    assert _is_secret_request("which database?") is False

class SecretTracker(MemoryTracker):
    """A tracker whose credential must never leave the runner."""

    API_KEY = "lin_api_TOPSECRET_0123456789"

    def execute_agent_tool(self, name, args):
        result = super().execute_agent_tool(name, args)
        assert self.API_KEY  # the runner holds it; the engine only sees the result
        return {"issue": {"identifier": "ENG-1", "state": "In Progress"}, **result}


def test_linear_graphql_runs_in_the_runner_and_the_key_never_reaches_the_pod(attempt, monkeypatch):
    from symphony.entry.run_issue import engine_env, engine_launch

    monkeypatch.setenv("LINEAR_API_KEY", SecretTracker.API_KEY)
    sandbox = FakeSandbox()
    env, _, engine = attempt(tracker=SecretTracker(), sandbox=sandbox,
                             ensure_sandbox=lambda issue, bound: sandbox)
    engine.push({
        "type": "host_tool_call", "id": "h7", "toolCallId": "tc-7", "toolName": "linear_graphql",
        "arguments": {"query": "mutation { issueUpdate }", "variables": {"id": "ENG-1"}},
    })
    engine.push(lambda e: bool(tool_results(e, "h7")))
    engine.run()

    run_attempt(env, "i1", 1, 1)
    (result,) = tool_results(engine, "h7")
    payload = json.loads(result["result"]["content"][0]["text"])
    assert payload["issue"]["state"] == "In Progress" and result["isError"] is False
    assert env.tracker.mutations[0]["args"]["query"] == "mutation { issueUpdate }"

    bound = env.cfg
    pod_env = engine_env(bound, "i1", 1, "attempt-token", ("acme/api", "acme/web"))
    launch = engine_launch(bound, "/omp-sessions/ws-ENG-1")
    leaked = [
        *(json.dumps(c) for c in engine.sent), *sandbox.scripts, launch, json.dumps(pod_env),
    ]
    assert not any(SecretTracker.API_KEY in text for text in leaked)
    assert "LINEAR_API_KEY" not in pod_env and " -e " not in launch


def test_the_engine_launch_and_environment_carry_model_and_repositories():
    from symphony.entry.run_issue import engine_env, engine_launch

    bound = replace(CFG, engine_provider="amazon-bedrock", engine_model="m-1", engine_thinking="high")
    launch = engine_launch(bound, "/omp-sessions/ws-x")
    assert "--provider amazon-bedrock --model m-1 --thinking high" in launch
    assert "--session-dir /omp-sessions/ws-x" in launch
    pod_env = engine_env(bound, "i1", 3, "tok", ("acme/api", "acme/web"))
    assert pod_env["SYMPHONY_TARGET_REPO"] == "acme/api"
    assert pod_env["SYMPHONY_TARGET_REPOS"] == "acme/api acme/web"
    assert not any("TOKEN_URL" in name or "ATTEMPT_TOKEN" in name for name in pod_env)
    assert pod_env["SYMPHONY_DISPATCH_SEQ"] == "3"


# -- provider rate limits (internals §10) ----------------------------------------------
def test_a_throttled_retry_records_the_model_rate_limit(attempt, redis_client):
    env, _, engine = attempt()
    engine.push({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 8000,
                 "errorMessage": "ThrottlingException: Too many tokens"})
    engine.push({"type": "auto_retry_start", "attempt": 2, "maxAttempts": 3, "delayMs": 1,
                 "errorMessage": "socket hang up"})  # not a rate limit: must not overwrite
    engine.run()
    run_attempt(env, "i1", 1, 1)
    doc = json.loads(redis_client.hget("sym:test:ratelimits", "acme/acme-1"))
    assert doc["source"] == "retry" and doc["stage"] == "throttled"
    assert doc["retry_after_ms"] == 8000 and isinstance(doc["observed_at"], int)


def test_the_usage_limit_is_read_at_each_turn_end(attempt, redis_client):
    env, _, engine = attempt()
    engine.responses["get_state"] = {"success": True, "data": {
        "sessionFile": "/s.jsonl", "sessionId": "s-1",
        "usageLimit": {"stage": "low_priority", "resetsAtSec": 1_900_000_000, "allowanceLeftPercent": 3},
    }}
    engine.push({"type": "turn_end"})
    engine.run()
    run_attempt(env, "i1", 1, 1)
    doc = json.loads(redis_client.hget("sym:test:ratelimits", "acme/acme-1"))
    assert doc["source"] == "usage_limit" and doc["stage"] == "low_priority"
    assert doc["reset_at"] == 1_900_000_000_000 and doc["remaining"] == 3
    assert len(engine.commands("get_state")) == 2  # startup + the one turn end


# -- investigation (internals §11) -----------------------------------------------------
APP_ENV = {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "k", "GITHUB_APP_INSTALLATION_ID": "2"}


def _issue(*labels):
    tracker = MemoryTracker()
    return tracker.add_issue("i9", "ENG-9", labels=("repo:acme/api", *labels))


def test_an_investigation_ticket_gets_a_read_only_token_request():
    from symphony import ghapp
    from symphony.entry.run_issue import installation_credential

    cred = installation_credential(_issue("Investigation"), CFG, APP_ENV, lambda: 0)
    assert cred.permissions == {"contents": "read", "pull_requests": "read", "metadata": "read"}
    assert ghapp.access_token_request(cred.target_repos, cred.permissions) == {
        "repositories": ["api"],
        "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
    }


def test_implementation_tickets_request_the_explicit_workspace_permissions():
    """Never the installation's own scope: it may include `issues`/`projects`, and agent
    code must not edit tickets directly (the tracker tools stay host-side)."""
    from symphony import ghapp
    from symphony.entry.run_issue import installation_credential

    cred = installation_credential(_issue("bug"), CFG, APP_ENV, lambda: 0)
    assert tuple(cred.target_repos) == ("acme/api",)
    assert ghapp.access_token_request(cred.target_repos, cred.permissions) == {
        "repositories": ["api"],
        "permissions": {
            "contents": "write", "pull_requests": "write", "metadata": "read",
            "checks": "read", "actions": "read", "statuses": "read",
        },
    }


def test_a_memory_board_without_a_github_app_runs_without_a_credential():
    from symphony.entry.run_issue import installation_credential

    assert installation_credential(_issue(), CFG, {}, lambda: 0) is None


# -- team configuration ------------------------------------------------------------
def test_the_published_team_model_is_used_and_observed(attempt):
    env, kv, engine = attempt()
    env.team.publish(
        {"models": {"implementer": {"provider": "amazon-bedrock", "model": "team-model", "thinking": "low"}}},
        expected_generation=0, actor="admin", reason="try the new model",
    )
    seen = {}

    def open_client(sb, token, issue, bound):
        seen.update(provider=bound.engine_provider, model=bound.engine_model, thinking=bound.engine_thinking)
        from symphony.omp_rpc import OmpClient
        return OmpClient(engine, engine, engine)

    env.open_client = open_client
    engine.run()
    run_attempt(env, "i1", 1, 1)
    assert seen == {"provider": "amazon-bedrock", "model": "team-model", "thinking": "low"}
    assert kv.get(state.issue_key("i1"))["model"] == "team-model"
    assert env.sessions.meta(SID)["model"] == "amazon-bedrock/team-model"
    assert env.team.observed()["runner:ENG-1"]["generation"] == 1


def test_without_a_publication_the_workflow_model_applies(attempt):
    env, kv, engine = attempt()
    engine.run()
    run_attempt(env, "i1", 1, 1)
    assert kv.get(state.issue_key("i1"))["provider"] == "acme"
    assert env.sessions.meta(SID)["model"] == "acme/acme-1"
    assert env.team.observed()["runner:ENG-1"]["generation"] == 0


# -- graceful engine stop ---------------------------------------------------------------
class _FakeWs:
    def __init__(self):
        self.open = True
        self.closed = False

    def is_open(self):
        return self.open

    def update(self, timeout=1):
        time.sleep(0.005)

    def peek_stdout(self):
        return False

    def peek_stderr(self):
        return False

    def close(self):
        self.closed = True


def test_the_pod_engine_is_sigtermed_by_its_pid_and_awaited():
    from symphony.kube import EngineProc

    ws = _FakeWs()

    class Sandbox:
        name = "ws-x"
        scripts = []

        def exec_script(self, script, timeout_s=None):
            self.scripts.append(script)
            if "kill -TERM" in script:
                ws.open = False  # the engine exits on SIGTERM; its exec stream closes
            return 0, ""

    sandbox = Sandbox()
    proc = EngineProc(ws, sandbox, "/run/symphony/engine-3.pid")
    assert proc.terminate(5) is True
    assert "kill -TERM" in sandbox.scripts[0] and "/run/symphony/engine-3.pid" in sandbox.scripts[0]
    assert ws.closed


def test_a_local_engine_exits_through_its_sigterm_handler(tmp_path):
    from symphony.workspace import LocalEngine

    engine = LocalEngine(
        ["bash", "-c", 'trap "echo flushed; exit 0" TERM; echo ready; while :; do sleep 0.05; done'],
        cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
    )
    assert engine.read_line(5) == "ready"
    assert engine.terminate(5) is True
    assert engine.read_line(5) == "flushed"  # the handler ran: work done on SIGTERM survives
    assert engine.proc.returncode == 0
