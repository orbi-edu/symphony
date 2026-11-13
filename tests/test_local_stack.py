"""Local mode (`symphony.entry.local`): one process runs the API, the wake-driven tick and the
attempts. Real Redis, real API over uvicorn, real runner core; only the engine is scripted
(`tests.omp_fakes`), except where a real process group is the point."""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import redis.asyncio
from tests.omp_fakes import MAX_BLOCK_S, FakeEngine, aborted_run
from tests.test_ghapp import _key

from symphony import ghapp, state
from symphony.effects_local import launch_engine, stop_process_group
from symphony.entry.local import LocalStack, ensure_admin_token
from symphony.sessions import EV_INTENT_APPLIED, EV_STATE

NS = "test"
WORKFLOW = """---
tracker: {kind: memory}
polling: {interval_ms: 30000}
agent: {max_turns: 1, input_timeout_ms: 1000}
engine: {provider: acme, model: acme-1}
telemetry: {enabled: false}
kubernetes: {learning: {enabled: false}}
hooks:
  after_create: echo "criado por $SYMPHONY_TARGET_REPO" > hook.txt
---
Trabalhe em {{ issue.identifier }}: {{ issue.title }}
"""


class LocalFakeEngine(FakeEngine):
    """The scripted engine, plus what a real process does when it is stopped: the stream ends."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = threading.Event()

    def read_line(self, timeout_s: float) -> str | None:
        deadline = time.monotonic() + min(timeout_s, MAX_BLOCK_S)
        while not self.closed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            line = super().read_line(min(remaining, 0.05))
            if line is not None:
                return line
        return None

    def terminate(self, grace_s: float) -> bool:
        super().terminate(grace_s)
        self.closed.set()
        return True


def wait_sent(kind):
    return lambda engine: bool(engine.commands(kind))


def until(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def local(redis_client, tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_must_not_leak")
    engines: queue.Queue = queue.Queue()
    launched: list[dict] = []

    def open_engine(argv, cwd, env, stderr_path):
        launched.append({"argv": argv, "cwd": cwd, "env": env})
        return engines.get(timeout=5)

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(WORKFLOW)
    socket_path = redis_client.connection_pool.connection_kwargs["path"]
    aclient = redis.asyncio.Redis(unix_socket_path=socket_path, decode_responses=True, socket_timeout=60)
    stack = LocalStack(
        client=redis_client, aclient=aclient, namespace=NS, root=tmp_path / "root",
        workflow=workflow, open_engine=open_engine, intent_block_ms=50,
    )
    stack.start_http("127.0.0.1", 0, None)
    loop = threading.Thread(target=stack.run_loop, daemon=True)
    loop.start()
    until(lambda: stack._swept, what="the first tick")
    token = stack.tokens.create("alice", "operator", actor="test")[0]
    http = httpx.Client(
        base_url=f"http://127.0.0.1:{stack.api_port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    yield SimpleNamespace(stack=stack, engines=engines, launched=launched, http=http, root=tmp_path / "root")
    http.close()
    stack.shutdown()
    loop.join(5)


class SseTail:
    """Collects `(event, data)` from the session's SSE route in a background thread."""

    def __init__(self, local, sid: str) -> None:
        self.events: list[tuple[str, dict]] = []
        self._url = f"/v1/sessions/{sid}/events?after=0-0"
        self._http = local.http
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        kind = None
        with self._http.stream("GET", self._url, timeout=httpx.Timeout(10, read=30)) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("event: "):
                    kind = line[len("event: "):]
                elif line.startswith("data: ") and kind:
                    self.events.append((kind, json.loads(line[len("data: "):])))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def applied(self) -> list[dict]:
        return [d for k, d in self.events if k == EV_INTENT_APPLIED]


def create_ticket(local, identifier="DEV-1", title="Criar a.txt") -> str:
    response = local.http.post("/v1/dev/issues", json={"identifier": identifier, "title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_a_memory_ticket_is_dispatched_by_a_wake_and_steered_through_the_api(local):
    engine = LocalFakeEngine()
    engine.on_abort = aborted_run
    engine.push({"type": "agent_start"})
    engine.push({"type": "message_update",
                 "assistantMessageEvent": {"type": "text_delta", "delta": "Criando a.txt"}})
    engine.push(wait_sent("steer"))
    engine.push({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash",
                 "args": {"command": "echo beta > b.txt"}})
    engine.push(wait_sent("abort"))  # holds the run until the human interrupts it
    engine.run("nunca chega aqui")
    local.engines.put(engine)

    created = time.monotonic()
    issue_id = create_ticket(local)
    # The safety poll is 30 s away: only the wake from the dev route can dispatch this fast.
    until(lambda: engine.prompted, timeout=1.0, what="the prompt within 1 s of the ticket")
    assert time.monotonic() - created < 1.0
    sid = f"{issue_id}:1"

    launch = local.launched[0]
    workspace = Path(launch["cwd"])
    assert workspace.parent == local.root.resolve() / "workspaces"
    assert (workspace / "hook.txt").read_text().strip() == "criado por memory/repo"
    assert launch["env"]["SYMPHONY_ISSUE_ID"] == issue_id
    assert launch["env"]["SYMPHONY_DISPATCH_SEQ"] == "1"
    assert "LINEAR_API_KEY" not in launch["env"] and "REDIS_URL" not in launch["env"]
    assert "--session-dir" in launch["argv"][-1]

    tail = SseTail(local, sid)
    until(lambda: "message_update" in tail.kinds(), what="engine events on the SSE route")

    steer = local.http.post(f"/v1/sessions/{sid}/messages",
                            json={"text": "also create b.txt with beta", "mode": "steer"})
    assert steer.status_code == 202
    steer_id = steer.json()["intent_id"]
    until(lambda: engine.commands("steer"), what="the steer reaching the engine")
    assert engine.commands("steer")[0]["message"] == "also create b.txt with beta"
    until(lambda: any(d["intent"] == steer_id for d in tail.applied()), what="intent_applied on SSE")
    until(lambda: "tool_execution_start" in tail.kinds(), what="the steered run continuing")

    interrupt = local.http.post(f"/v1/sessions/{sid}/interrupt", json={"reason": "chega por hoje"})
    assert interrupt.status_code == 202
    until(lambda: engine.commands("abort"), what="the interrupt reaching the engine")
    until(lambda: any(d["kind"] == "interrupt" for d in tail.applied()), what="interrupt applied")
    # Nobody writes after the interrupt: the session parks, then ends at the input timeout.
    tail.thread.join(10)
    states = [d.get("state") for k, d in tail.events if k == EV_STATE]
    assert "idle" in states and states[-1] == "ended"
    entry = local.stack.kv.get(state.issue_key(issue_id))
    assert entry["status"] == "retry_queued" and entry["runner_call_id"] is None
    assert engine.terminated, "the engine is stopped with SIGTERM at the end of the attempt"


def test_moving_the_ticket_to_a_terminal_state_cancels_the_running_engine(local):
    engine = LocalFakeEngine()
    engine.push({"type": "agent_start"})
    engine.push(lambda e: False)  # the agent keeps working until something stops it
    local.engines.put(engine)

    issue_id = create_ticket(local, "DEV-2", "Long task")
    until(lambda: engine.prompted, what="the attempt to start")
    entry = until(lambda: (local.stack.kv.get(state.issue_key(issue_id)) or {}).get("runner_call_id")
                  and local.stack.kv.get(state.issue_key(issue_id)), what="the running entry")
    call_id = entry["runner_call_id"]
    assert local.stack.fx.is_call_running(call_id)
    workspace = Path(local.launched[0]["cwd"])
    assert workspace.is_dir()

    moved = local.http.post(f"/v1/dev/issues/{issue_id}/state", json={"state": "Cancelled"})
    assert moved.status_code == 200 and moved.json()["state_type"] == "terminal"
    until(lambda: engine.terminated, what="the engine to be stopped")
    until(lambda: not local.stack.fx.is_call_running(call_id), what="the attempt thread to end")
    until(lambda: local.stack.kv.get(state.issue_key(issue_id)) is None, what="terminal cleanup")
    assert not workspace.exists(), "a terminal ticket's workspace is removed"
    meta = until(lambda: (m := local.stack.sessions.meta(f"{issue_id}:1")) and m["state"] == "ended" and m,
                 what="the session to end")
    assert meta["state"] == "ended"


def test_with_a_github_app_an_attempt_gets_a_scoped_token_through_its_own_helper_only(local, monkeypatch, tmp_path):
    """Same credential as a workspace pod: minted per attempt for the ticket's repositories,
    in a 0600 file read by a helper. The host's helpers (Homebrew git's system-wide
    osxkeychain) are never asked: they would send their own password for the host and keep
    the attempt's token after it ends. The token goes with the attempt."""
    asked = []

    def exchange(assertion, installation_id, repos, body):
        asked.append((installation_id, body))
        return {"token": "ghs_attempt", "expires_at_ms": int(time.time() * 1000) + 3_600_000}

    monkeypatch.setattr(ghapp, "_http_exchange", exchange)
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "7")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _key())
    engine = LocalFakeEngine()
    engine.push({"type": "agent_start"})
    engine.push(lambda e: False)
    local.engines.put(engine)
    issue_id = create_ticket(local, "DEV-4", "Com o App")
    until(lambda: engine.prompted, what="the attempt to start")

    assert asked == [("7", {"repositories": ["repo"], "permissions": ghapp.WORKSPACE})]
    env = local.launched[0]["env"]
    assert not any(name.startswith("GITHUB_APP_") for name in env), "the signing key stays on the host"
    assert "ghs_attempt" not in json.dumps(env), "the token is not in the environment"
    helper = Path(shlex.split(env["GIT_CONFIG_VALUE_1"].removeprefix("!"))[0])
    token_file = helper.parent / "gh-token"
    assert token_file.read_text().strip() == "ghs_attempt"
    assert token_file.stat().st_mode & 0o777 == 0o600 and helper.parent.stat().st_mode & 0o777 == 0o700

    marker = tmp_path / "system-helper-asked"
    system = tmp_path / "gitconfig-system"
    system.write_text(f"[credential]\n\thelper = \"!touch {marker}; echo password=from-keychain; true\"\n")
    git_env = {**{k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")}, "PATH": os.environ["PATH"],
               "HOME": str(tmp_path), "GIT_CONFIG_SYSTEM": str(system), "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-global")}
    filled = subprocess.run(["git", "credential", "fill"], input="url=http://127.0.0.1:4200/memory/repo.git\n\n",
                            env=git_env, capture_output=True, text=True, check=True).stdout
    assert "password=ghs_attempt" in filled.splitlines()
    assert not marker.exists(), "a host helper was asked before Symphony's"

    local.http.post(f"/v1/dev/issues/{issue_id}/state", json={"state": "Cancelled"})
    until(lambda: not helper.parent.exists(), what="the attempt's credential to be removed")


def test_dev_routes_need_operator_and_unknown_tickets_are_404(local):
    viewer = local.stack.tokens.create("leitor", "viewer", actor="test")[0]
    denied = local.http.post("/v1/dev/issues", json={"identifier": "DEV-9", "title": "x"},
                             headers={"Authorization": f"Bearer {viewer}"})
    assert denied.status_code == 403
    missing = local.http.post("/v1/dev/issues/nope/state", json={"state": "Done"})
    assert missing.status_code == 404
    local.engines.put(LocalFakeEngine())
    create_ticket(local, "DEV-3", "Duplicado")
    again = local.http.post("/v1/dev/issues", json={"identifier": "DEV-3", "title": "outra"})
    assert again.status_code == 409
    listed = local.http.get("/v1/dev/issues", headers={"Authorization": f"Bearer {viewer}"})
    assert [i["identifier"] for i in listed.json()["items"]] == ["DEV-3"]


def test_the_bootstrap_admin_token_is_created_once(redis_client):
    from symphony.apitokens import TokenStore

    tokens = TokenStore(redis_client, NS)
    first = ensure_admin_token(tokens, None)
    assert first and tokens.verify(first)["role"] == "admin"
    assert ensure_admin_token(tokens, None) is None, "an admin already exists: nothing printed"


def test_stopping_a_generation_reaches_the_tools_it_spawned(tmp_path):
    marker = tmp_path / "child.pid"
    engine = launch_engine(
        ["bash", "-c", f"sleep 60 & echo $! > {marker}; wait"],
        str(tmp_path), dict(os.environ), str(tmp_path / "stderr.log"),
    )
    child = int(until(lambda: marker.exists() and marker.read_text().strip(), what="the child"))
    pgid = engine.proc.pid
    assert os.getpgid(child) == pgid, "the engine leads its own process group"
    engine.terminate(2.0)
    assert stop_process_group(pgid, grace_s=2.0)
    # Gone means no process of ours is left: ESRCH, or EPERM for a group whose last zombie
    # launchd adopted (macOS).
    with pytest.raises((ProcessLookupError, PermissionError)):
        os.killpg(pgid, 0)
