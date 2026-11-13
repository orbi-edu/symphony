"""First-run setup in local mode: `secrets.env` (setup_secrets_local), the WORKFLOW file as the
published document (workflow_file), and a fresh stack that goes from "setup incomplete" to
dispatching a Linear ticket through `PUT /v1/setup/tracker`, without a restart."""

from __future__ import annotations

import os
import queue
import stat
import subprocess
import threading

import httpx
import pytest
import redis.asyncio
from scripts.mocks.linear import server as linear_mock
from scripts.mocks.linear.store import DEFAULT_API_KEY
from tests.test_local_stack import LocalFakeEngine, until

from symphony import state
from symphony.admin_workflow import WorkflowError
from symphony.entry.local import LocalStack
from symphony.setup_secrets import SecretStoreError
from symphony.setup_secrets_local import LocalSecretStore
from symphony.workflow_file import file_workflow

NS = "test"
NAMES = ("LINEAR_API_KEY", "LINEAR_WEBHOOK_SECRET", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
         "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET", "SLACK_BOT_TOKEN", "SLACK_CHANNEL",
         "SLACK_WEBHOOK_URL", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_WEBHOOK_URL")
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc'def\"$HOME`x`\n-----END RSA PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The store writes the process environment; monkeypatch restores every name after."""
    for name in NAMES:
        # setenv first so monkeypatch records the original (possibly unset) value and
        # removes what the store wrote; a bare delenv of an unset name records nothing.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


# -- secrets.env ------------------------------------------------------------------------
def test_put_writes_a_private_sourceable_file_and_applies_at_once(tmp_path):
    changed: list[set[str]] = []
    store = LocalSecretStore(tmp_path / "secrets.env", on_change=changed.append)
    assert store.mode == "local" and not store.has("linear", "LINEAR_API_KEY")

    assert store.put("linear", {"LINEAR_API_KEY": "lin_api_one"}) == []
    assert os.environ["LINEAR_API_KEY"] == "lin_api_one" and store.has("linear", "LINEAR_API_KEY")
    assert changed == [{"LINEAR_API_KEY"}]
    store.put("github-app", {"GITHUB_APP_ID": "7", "GITHUB_APP_PRIVATE_KEY": PEM})

    path = tmp_path / "secrets.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    sourced = subprocess.run(
        ["sh", "-c", f'set -a; . "{path}"; printf %s "$GITHUB_APP_PRIVATE_KEY"'],
        capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, check=True,
    ).stdout
    assert sourced == PEM, "values survive a shell `.` byte for byte, quotes and newlines included"

    store.remove("linear", ["LINEAR_API_KEY"])
    assert "LINEAR_API_KEY" not in os.environ and "LINEAR_API_KEY" not in path.read_text()


def test_a_new_process_loads_the_file_and_the_host_environment_wins(tmp_path, monkeypatch):
    LocalSecretStore(tmp_path / "secrets.env").put(
        "slack", {"SLACK_BOT_TOKEN": "xoxb-file", "SLACK_CHANNEL": "C1"}
    )
    for name in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("SLACK_CHANNEL", "C-host")

    store = LocalSecretStore(tmp_path / "secrets.env")
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-file"
    assert os.environ["SLACK_CHANNEL"] == "C-host", "the host's value is not replaced by the file"
    with pytest.raises(SecretStoreError) as refused:
        store.put("slack", {"SLACK_CHANNEL": "C2"})
    assert (refused.value.code, refused.value.status) == ("host_env_overrides", 409)
    assert "C-host" not in str(refused.value) and "C2" not in str(refused.value)


def test_writes_outside_a_group_or_to_the_read_only_group_are_refused(tmp_path):
    store = LocalSecretStore(tmp_path / "secrets.env")
    for group, values, code in (
        ("linear", {"SLACK_BOT_TOKEN": "x"}, "unknown_secret"),
        ("nope", {"X": "y"}, "unknown_group"),
        ("model-provider", {"AWS_REGION": "us-east-1"}, "read_only_group"),
        ("linear", {"LINEAR_API_KEY": "  "}, "invalid_secret"),
    ):
        with pytest.raises(SecretStoreError) as refused:
            store.put(group, values)
        assert refused.value.code == code and refused.value.status == 422
    assert not (tmp_path / "secrets.env").exists()


def test_a_deferred_write_reaches_the_stack_on_restart(tmp_path):
    changed: list[set[str]] = []
    store = LocalSecretStore(tmp_path / "secrets.env", on_change=changed.append)
    store.put("linear", {"LINEAR_WEBHOOK_SECRET": "s1"}, restart=False)
    store.put("api", {"GITHUB_WEBHOOK_SECRET": "s2"}, restart=False)
    assert changed == [] and os.environ["GITHUB_WEBHOOK_SECRET"] == "s2"
    assert store.restart(["linear", "api"]) == []
    assert changed == [{"LINEAR_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET"}]
    store.restart(["linear"])
    assert len(changed) == 1, "nothing new to apply"


# -- the WORKFLOW file -------------------------------------------------------------------
WORKFLOW = """---
tracker:
  kind: linear
  provider:
    api_key: $LINEAR_API_KEY
polling: {interval_ms: 30000}
agent: {max_turns: 1}
engine: {provider: acme, model: acme-1}
telemetry: {enabled: false}
kubernetes:
  default_repo: acme/api
  learning: {enabled: false}
---
Work on {{ issue.identifier }}: {{ issue.title }}
"""


def test_the_workflow_file_publishes_through_the_reviewed_path(tmp_path):
    path = tmp_path / "WORKFLOW.md"
    path.write_text(WORKFLOW)
    replaced = []
    repo = file_workflow(path, tmp_path / "run" / "annotations.json", NS,
                         on_replace=lambda: replaced.append(1))
    current = repo.read()
    review = repo.validate(current["revision"], {"tracker.provider.team_key": "ENG"})
    payload = {"baseRevision": current["revision"], "patch": {"tracker.provider.team_key": "ENG"},
               "documentHash": review["documentHash"]}
    revision = repo.publish(payload, "cmd-1", before_write=lambda: None)
    assert "team_key: ENG" in path.read_text() and replaced == [1]
    assert repo.read()["revision"] == revision
    assert repo.publish(payload, "cmd-1", before_write=lambda: None) == revision, "replay is a no-op"

    path.write_text(path.read_text() + "\nedited by hand\n")
    with pytest.raises(WorkflowError) as conflict:
        repo.validate(revision, {"tracker.provider.team_key": "OPS"})
    assert conflict.value.status_code == 412


# -- a fresh stack, set up from the API ---------------------------------------------------
@pytest.fixture
def linear(tmp_path, monkeypatch):
    srv = linear_mock.serve(0, tmp_path / "linear-state")
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("LINEAR_API_URL", f"{base}/graphql")
    yield base
    srv.shutdown()
    srv.server_close()


def test_a_fresh_stack_reports_setup_incomplete_until_the_tracker_step_then_dispatches(
    redis_client, tmp_path, linear,
):
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(WORKFLOW)
    engines: queue.Queue = queue.Queue()
    engine = LocalFakeEngine()
    engine.push({"type": "agent_start"})
    engine.push(lambda e: False)
    engines.put(engine)
    socket_path = redis_client.connection_pool.connection_kwargs["path"]
    aclient = redis.asyncio.Redis(unix_socket_path=socket_path, decode_responses=True, socket_timeout=60)
    stack = LocalStack(
        client=redis_client, aclient=aclient, namespace=NS, root=tmp_path / "root",
        workflow=workflow, open_engine=lambda argv, cwd, env, err: engines.get(timeout=5),
        intent_block_ms=50,
    )
    stack.start_http("127.0.0.1", 0, None)
    loop = threading.Thread(target=stack.run_loop, daemon=True)
    loop.start()
    admin = stack.tokens.create("root", "admin", actor="test")[0]
    http = httpx.Client(base_url=f"http://127.0.0.1:{stack.api_port}",
                        headers={"Authorization": f"Bearer {admin}"}, timeout=15)
    try:
        ticket = httpx.post(f"{linear}/_mock/issues", json={"title": "First ticket", "state": "Todo"}).json()

        setup_state = until(lambda: stack.kv.get(state.SETUP_KEY), what="the setup report")
        assert setup_state["status"] == "incomplete" and "Linear API key" in setup_state["reason"]
        report = http.get("/v1/setup").json()
        assert report["mode"] == "local" and report["complete"] is False
        assert next(s for s in report["steps"] if s["id"] == "tracker")["status"] != "done"
        assert http.get("/v1/overview").json()["daemon"]["setup"] == "incomplete"
        assert not engine.prompted, "nothing dispatches while setup is incomplete"

        saved = http.put("/v1/setup/tracker",
                         json={"kind": "linear", "api_key": DEFAULT_API_KEY, "team_key": "ENG"})
        assert saved.status_code == 200, saved.text
        assert DEFAULT_API_KEY not in saved.text, "the credential is write-only"

        # No restart: the key is in the environment and the WORKFLOW file was republished.
        until(lambda: engine.prompted, timeout=10, what="the ticket to be dispatched")
        secrets = (tmp_path / "root" / "secrets.env")
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
        assert "LINEAR_API_KEY=" in secrets.read_text()
        assert DEFAULT_API_KEY not in workflow.read_text() and "team_key: ENG" in workflow.read_text()
        assert until(lambda: stack.kv.get(state.SETUP_KEY)["status"] == "ready", what="ready")
        tracker_step = next(s for s in http.get("/v1/setup").json()["steps"] if s["id"] == "tracker")
        assert tracker_step["status"] == "done"
        entry = until(lambda: next((e for k, e in state.tracked_issues(stack.kv).items()), None),
                      what="the tracked entry")
        assert entry["identifier"] == ticket["identifier"]
    finally:
        http.close()
        stack.shutdown()
        loop.join(5)
