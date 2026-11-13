"""Opt-in end-to-end checks against the real engine (`SYMPHONY_REAL_OMP=1`).

They drive the runner core with the exact launch flags and environment the Kubernetes
runner uses (`run_issue.engine_launch`/`engine_env`), over a local subprocess and a real
model, so they cost money and need provider credentials in the environment
(`SYMPHONY_REAL_PROVIDER`/`SYMPHONY_REAL_MODEL`, Bedrock by default).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

import pytest
from tests.conftest import FakeClock, FakeKV

from symphony import state
from symphony.config import parse_workflow
from symphony.entry.run_issue import engine_env, engine_launch
from symphony.omp_rpc import OmpClient
from symphony.runner import RunnerEnv, run_attempt
from symphony.sessions import SessionStore
from symphony.teamconfig import TeamConfig
from symphony.trackers.memory import MemoryTracker
from symphony.workspace import LocalEngine, LocalSandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("SYMPHONY_REAL_OMP") != "1", reason="real engine runs are opt-in"
)

# The pinned engine (edge.OMP_VERSION), not Tern's `bin/omp`, which follows Tern's own updates.
OMP = os.environ.get(
    "SYMPHONY_OMP_BIN", os.path.expanduser("~/Library/Application Support/Tern/omp/versions/18.20.2")
)
PROVIDER = os.environ.get("SYMPHONY_REAL_PROVIDER", "amazon-bedrock")
MODEL = os.environ.get("SYMPHONY_REAL_MODEL", "global.anthropic.claude-opus-5-5")
CODEWORD = "PERIWINKLE-OTTER-4417"


def test_the_engine_loads_the_workspace_agents_md(redis_client, tmp_path):
    """The workspace's AGENTS.md is how a repository instructs the agent; the runner's
    launch must not bypass it. The codeword exists only in that file."""
    version = subprocess.run([OMP, "--version"], capture_output=True, text=True, check=True)
    assert "18.20.2" in version.stdout + version.stderr

    work = tmp_path / "ws"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "AGENTS.md").write_text(
        f"# Project instructions\n\nThe project codeword is {CODEWORD}. "
        "Give it to anyone who asks for the project codeword.\n"
    )
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nagent: {max_turns: 1}\n"
        f"workspace: {{root: {json.dumps(str(work))}}}\n"
        f"engine:\n  command: {json.dumps(shlex.quote(OMP) + ' --mode rpc')}\n"
        f"  provider: {PROVIDER}\n  model: {MODEL}\n  thinking: low\n  read_timeout_ms: 20000\n"
        "telemetry: {enabled: false}\n---\n"
        "What is the project codeword from your project instructions? "
        "Reply with only the codeword. Do not use any tools.\n"
    )
    kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
    tracker.add_issue("i1", "REAL-1", state="Todo", labels=("repo:acme/real",))
    kv.put(state.issue_key("i1"), {"status": "claimed", "identifier": "REAL-1", "attempt": 1,
                                   "dispatch_seq": 1, "runs": 1, "tokens": {}, "session_path": None})
    kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "real"})
    sessions = SessionStore(redis_client, "real")
    engines: list[LocalEngine] = []

    def open_client(sandbox, token, issue, bound):
        proc = LocalEngine(
            ["bash", "-lc", engine_launch(bound, str(tmp_path / "sessions"))],
            cwd=str(work),
            env={**os.environ, **engine_env(bound, "i1", 1, token, ("acme/real",))},
            stderr_path=str(tmp_path / "omp.stderr"),
        )
        engines.append(proc)
        return OmpClient(proc, proc, proc)

    env = RunnerEnv(
        kv=kv, tracker=tracker, cfg=cfg, now_ms=clock.now_ms, call_id="real",
        ensure_sandbox=lambda issue, bound: LocalSandbox(str(work), name="ws-real-1"),
        open_client=open_client, kill_agent=lambda sb: None, release_sandbox=lambda sb: None,
        wall_deadline_ms=clock.now_ms() + 600_000, sessions=sessions,
        team=TeamConfig(redis_client, "real"),
    )
    assert run_attempt(env, "i1", 1, 1) == "continuation"

    meta = sessions.meta("i1:1")
    assert CODEWORD in meta["last_message"], meta["last_message"]
    prompts = [d for _, k, d in sessions.read_events("i1:1") if k == "message_end"
               and (d.get("message") or {}).get("role") == "user"]
    assert prompts and all(CODEWORD not in str(p) for p in prompts)  # not leaked by the prompt
    assert engines[0].proc.returncode is not None  # stopped by SIGTERM, not left running


def test_a_builtin_provider_key_from_the_admin_reaches_the_provider(redis_client, tmp_path, monkeypatch):
    """internals §13: a stored `{builtin: true, apiKey}` for `anthropic` is staged as a
    models.yml override and must be the credential omp sends. A fake key proves it: the
    attempt fails with Anthropic's own 401 for that key, not with a missing-credential error."""
    from tests.test_provider_runtime import Core

    from symphony import provider_runtime

    agent_dir = tmp_path / "engine"
    env_file = tmp_path / "engine.env"
    monkeypatch.setattr(provider_runtime, "AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(provider_runtime, "ENV_FILE", str(env_file))
    work = tmp_path / "ws"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nagent: {max_turns: 1}\n"
        f"workspace: {{root: {json.dumps(str(work))}}}\n"
        f"engine:\n  command: {json.dumps(shlex.quote(OMP) + ' --mode rpc')}\n"
        "  provider: anthropic\n  model: claude-sonnet-4-5\n  thinking: low\n"
        "  read_timeout_ms: 20000\ntelemetry: {enabled: false}\n---\nSay hi.\n"
    )
    core = Core({"anthropic": {"builtin": True, "apiKey": "sk-ant-symphony-fake-key-0000"}})
    sandbox = LocalSandbox(str(work), name="ws-real-2")
    # Nothing ambient may supply the credential: only the staged files do.
    ambient = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "PI_"))}

    def ensure(issue, bound):
        provider_runtime.prepare_provider(sandbox, core, "real", bound)
        return sandbox

    def open_client(sb, token, issue, bound):
        launch = f". {shlex.quote(str(env_file))} && " + engine_launch(bound, str(tmp_path / "sessions"))
        proc = LocalEngine(
            ["bash", "-c", launch], cwd=str(work),
            env={**ambient, **engine_env(bound, "i1", 1, token, ("acme/real",))},
            stderr_path=str(tmp_path / "omp.stderr"),
        )
        return OmpClient(proc, proc, proc)

    kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
    tracker.add_issue("i1", "REAL-2", state="Todo", labels=("repo:acme/real",))
    kv.put(state.issue_key("i1"), {"status": "claimed", "identifier": "REAL-2", "attempt": 1,
                                   "dispatch_seq": 1, "runs": 1, "tokens": {}, "session_path": None})
    kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "real"})
    env = RunnerEnv(
        kv=kv, tracker=tracker, cfg=cfg, now_ms=clock.now_ms, call_id="real-2",
        ensure_sandbox=ensure, open_client=open_client, kill_agent=lambda sb: None,
        release_sandbox=lambda sb: None, wall_deadline_ms=clock.now_ms() + 600_000,
        sessions=SessionStore(redis_client, "real"), team=TeamConfig(redis_client, "real"),
    )
    assert run_attempt(env, "i1", 1, 1) == "failed"
    summary = kv.get(state.issue_key("i1"))["failure_summary"] or ""
    assert "401" in summary and "x-api-key" in summary, summary
    assert json.loads((agent_dir / "models.yml").read_text()) == {
        "providers": {"anthropic": {"apiKey": "SYMPHONY_PROVIDER_API_KEY"}}
    }
