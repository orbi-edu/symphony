"""The assistant's omp runtime over the scripted RPC double (no real omp, no model)."""

from __future__ import annotations

import os

import pytest
from tests.omp_fakes import FakeEngine, aborted_run, assistant

from symphony import sessions as sessions_mod
from symphony.assistant import tools
from symphony.assistant.runtime import TOOLS, AssistRunner, agent_environment
from symphony.config import parse_workflow
from symphony.sessions import SessionStore
from symphony.teamconfig import TeamConfig


class Proc(FakeEngine):
    def close(self):
        self.closed = True

    def stderr_tail(self, lines=3):
        return ""


class Spawner:
    def __init__(self) -> None:
        self.spawned: list[tuple[list, str, dict, Proc]] = []

    def __call__(self, argv, cwd, env, err):
        proc = Proc()
        proc.on_abort = aborted_run
        self.spawned.append((argv, cwd, env, proc))
        return proc


def deltas(proc: Proc, *chunks: str, final: str) -> None:
    proc.push({"type": "message_update", "messageId": "r1", "message": {"role": "assistant"},
               "assistantMessageEvent": {"type": "text_start", "contentIndex": 0}})
    for chunk in chunks:
        proc.push({"type": "message_update", "messageId": "r1", "message": {"role": "assistant"},
                   "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": chunk}})
    proc.run(final)


@pytest.fixture
def runner(redis_client, tmp_path):
    cfg = parse_workflow("---\ntracker: {kind: memory}\nassistant: {enabled: true}\n---\nx")
    spawn = Spawner()
    store = SessionStore(redis_client, "t")

    def tools_for(role):
        out = [tools.search_tool(lambda q, n: [{"identifier": "MEM-1", "title": q}])]
        return out + ([tools.propose_tool(())] if role == "planner" else [])

    run = AssistRunner(store, session_root=str(tmp_path), config=lambda: cfg,
                       team=TeamConfig(redis_client, "t"), tools_for=tools_for, spawn=spawn,
                       environ={"PATH": "/bin", "SLACK_BOT_TOKEN": "xoxb-dummy", "AWS_REGION": "x",
                                "GITHUB_APP_PRIVATE_KEY": "dummy", "REDIS_URL": "redis://x"},
                       silence_s=5, command_timeout_s=5)
    return run, spawn, store


def test_one_process_serves_the_thread_and_streams_text(runner):
    run, spawn, store = runner
    agent = run.open("assist:slack:C1:t1:1", "assistant", {"transport": "slack", "channel": "C1"},
                     lambda actor, text: None)
    [(argv, cwd, env, proc)] = spawn.spawned
    assert argv[argv.index("--tools") + 1] == "read,grep,glob,web_search"
    assert "SLACK_BOT_TOKEN" not in env and "GITHUB_APP_PRIVATE_KEY" not in env
    assert "REDIS_URL" not in env and env["AWS_REGION"] == "x"
    agent_dir = env["PI_CODING_AGENT_DIR"]
    assert oct(os.stat(agent_dir).st_mode & 0o777) == "0o700"
    assert "enableProjectConfig: false" in open(os.path.join(agent_dir, "config.yml")).read()
    meta = store.meta("assist:slack:C1:t1:1")
    assert meta["kind"] == "assist" and meta["role"] == "assistant" and meta["channel"] == "C1"

    seen = []
    deltas(proc, "Hel", "lo", final="Hello.")
    result = agent.ask("hi", seen.append, 10)
    assert result.status == "completed" and result.text == "Hello."
    assert seen == ["Hel", "Hello"]
    assert store.meta(agent.sid)["state"] == "idle"

    deltas(proc, "Again", final="Again.")
    assert agent.ask("follow-up", lambda t: None, 10).text == "Again."
    assert len(spawn.spawned) == 1 and proc.prompted == 2
    agent.close("completed")
    assert store.meta(agent.sid)["state"] == "ended"


def test_the_planner_proposes_once_per_turn(runner):
    run, spawn, store = runner
    agent = run.open("assist:slack:C1:t2:1", "planner", {}, lambda actor, text: None)
    proc = spawn.spawned[0][3]
    plan = {"summary": "s", "tickets": [{"key": "a", "title": "T", "body": "B"}]}
    for turn in range(2):
        proc.push({"type": "message_end", "message": assistant("Here is the plan.")})
        proc.push({"type": "host_tool_call", "id": f"h{turn}", "toolName": "propose_tickets",
                   "arguments": plan})
        proc.run("never reached")
        result = agent.ask("plan it", lambda t: None, 10)
        assert result.status == "completed" and result.proposal == plan
        assert result.text == "Here is the plan."
    bad = {"summary": "s", "tickets": [{"key": "a", "title": "x" * 200, "body": ""}]}
    proc.push({"type": "host_tool_call", "id": "h9", "toolName": "propose_tickets", "arguments": bad})
    proc.run("I could not.")
    result = agent.ask("plan it badly", lambda t: None, 10)
    assert result.proposal is None
    rejected = [m for m in proc.sent if m.get("type") == "host_tool_result" and m["id"] == "h9"]
    assert rejected[0]["isError"] is True
    agent.close("completed")


def test_a_tern_follow_up_reaches_the_engine(runner):
    run, spawn, store = runner
    got = []
    agent = run.open("assist:slack:C1:t3:1", "assistant", {}, lambda actor, text: got.append((actor, text)))
    store.send_intent(agent.sid, "follow_up", actor="operator", text="and the tests?")
    import time

    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    assert got == [("operator", "and the tests?")]
    agent.close("completed")


def test_agent_environment_drops_host_credentials():
    env = agent_environment({"SLACK_APP_TOKEN": "a", "DISCORD_BOT_TOKEN": "b", "LINEAR_API_KEY": "c",
                             "HOME": "/h", "ANTHROPIC_API_KEY": "k"})
    assert env == {"HOME": "/h", "ANTHROPIC_API_KEY": "k"}
    assert "assist" in sessions_mod.KINDS


@pytest.mark.skipif(os.environ.get("SYMPHONY_REAL_OMP") != "1", reason="real engine runs are opt-in")
def test_every_tool_is_a_built_in_the_engine_always_has():
    """An unavailable `--tools` name makes omp exit 2 at start (`find` needs a judge model)."""
    import subprocess

    text = subprocess.run(["omp", "--help"], capture_output=True, text=True, timeout=60).stdout
    section = text.split("Built-in Tools (names accepted by --tools):", 1)[1].split("# Optional", 1)[0]
    rows = {line.split()[0]: line for line in section.splitlines() if " - " in line}
    for name in TOOLS:
        assert name in rows, name
        assert "(when " not in rows[name], f"{name} is not always available: {rows[name].strip()}"
