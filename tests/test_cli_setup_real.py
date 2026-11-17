"""`symphony setup` against the REAL setup API (`symphony.api.setup` on uvicorn) and the
Linear, GitHub, Slack and Discord mocks, with the real `KubernetesSecretStore` on the fake
Kubernetes of `test_api_setup.py`. A Deployment restart copies the stored Secrets into the
process environment, the way a restarted pod sees them, so each step's status follows."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from scripts.mocks.discord import server as discord_mock
from scripts.mocks.linear.store import DEFAULT_API_KEY as LINEAR_KEY
from scripts.mocks.slack import server as slack_mock
from tests.api_support import FakeMerger, Server, build_app, make_services, tokens_for
from tests.test_api_setup import (  # noqa: F401 — `vendors` is a fixture
    DEFAULT_WORKFLOW,
    ENV_NAMES,
    FULL_PERMISSIONS,
    FakeApps,
    FakeCore,
    vendors,
)

from symphony.admin_workflow import WorkflowRepository
from symphony.cli import creds, main, render, setup
from symphony.cli.client import Api
from symphony.setup_secrets import GROUPS, SECRET_NAMES, KubernetesSecretStore
from test_cli_real_api import Keychain


class RestartingApps(FakeApps):
    """Restarting a Deployment hands the stored Secrets to the process environment."""

    def __init__(self, restart) -> None:
        super().__init__()
        self._restart = restart

    def patch_namespaced_deployment(self, **kwargs):
        super().patch_namespaced_deployment(**kwargs)
        self._restart()


@pytest.fixture
def env(redis_client, vendors, monkeypatch, tmp_path):  # noqa: F811
    for var in ("SYMPHONY_URL", "SYMPHONY_TOKEN", "SYMPHONY_PUBLIC_URL", *ENV_NAMES):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    default = tmp_path / "WORKFLOW.default.md"
    default.write_text(DEFAULT_WORKFLOW)
    monkeypatch.setenv("SYMPHONY_DEFAULT_WORKFLOW", str(default))
    monkeypatch.setenv("LINEAR_API_URL", f"{vendors.linear}/graphql")
    monkeypatch.setenv("GITHUB_API_URL", vendors.github)
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"{vendors.github}/graphql")
    monkeypatch.setenv("SLACK_API_URL", f"{vendors.slack}/api")
    monkeypatch.setenv("DISCORD_API_URL", f"{vendors.discord}/api/v10")
    kc = Keychain()
    monkeypatch.setattr(creds, "_keyring", lambda: kc)
    core = FakeCore()

    def restart():
        for group, name in SECRET_NAMES.items():
            if group in GROUPS:
                for key, value in core.stored(name).items():
                    if key in ENV_NAMES:
                        monkeypatch.setenv(key, value)

    merger = FakeMerger(["acme/app"])
    services = make_services(redis_client, merger=merger,
                             secrets=KubernetesSecretStore(core, "test", RestartingApps(restart)),
                             workflow=WorkflowRepository(core, "test"))
    services.linear_secret = services.github_secret = None  # read the env, as in production
    tokens = tokens_for(services)
    vendors.github_server.mock.store.install("acme", ["acme/app"], FULL_PERMISSIONS)
    app = httpx.get(f"{vendors.github}/_mock/app").json()
    with Server(build_app(services)) as server:
        def run(capsys, *argv, stdin=None):
            if stdin is not None:
                monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
            code = main.main(list(argv))
            out, err = capsys.readouterr()
            return code, out, err

        yield SimpleNamespace(
            url=server.url, tokens=tokens, core=core, run=run, vendors=vendors,
            app_id=str(app["id"]), installation=app["env"]["GITHUB_APP_INSTALLATION_ID"],
            pem=app["private_key"])


def login(env, capsys, role="admin"):
    code, out, err = env.run(capsys, "login", "--url", env.url, "--token", "-", "--json",
                             stdin=env.tokens[role])
    assert code == 0, out + err


def test_each_step_command_against_the_real_api(env, capsys):
    login(env, capsys)
    code, out, _ = env.run(capsys, "setup", "--status")
    assert code == 0 and out.startswith("Symphony setup is not complete: 4 required steps left.")
    assert f"  ✗ {'Models':<24}  {'error':<7}  No credential for provider " in out

    # GitHub App: the key on stdin; a wrong installation reads as its sentence.
    code, _, err = env.run(capsys, "setup", "github-app", "--app-id", env.app_id,
                           "--installation-id", "999999", stdin=env.pem)
    assert err == f"error: {render.ERROR_TEXT['github_installation_not_found']}\n"
    code, out, err = env.run(capsys, "setup", "github-app", "--app-id", env.app_id,
                             "--installation-id", env.installation, stdin=env.pem)
    assert code == 0, err
    assert out.startswith("GitHub App saved.\nThe installation can reach 1 repository: acme/app.\n")
    assert "Restarted to apply it: " in out

    # Linear: a wrong key, an unknown team (with the API's details), then the real key.
    code, _, err = env.run(capsys, "setup", "tracker", "--kind", "linear", "--team", "ENG",
                           stdin="lin_api_wrong\n")
    assert err == f"error: {render.ERROR_TEXT['linear_unauthorized']}\n"
    code, _, err = env.run(capsys, "setup", "tracker", "--kind", "linear", "--team", "NOPE",
                           stdin=f"{LINEAR_KEY}\n")
    assert err.startswith("error: Linear has no team with this key. Details: ")
    code, out, err = env.run(capsys, "setup", "tracker", "--kind", "linear", "--team", "ENG",
                             stdin=f"{LINEAR_KEY}\n")
    assert code == 0, err
    assert out.startswith("Tracker saved: Linear.\nStates on the board: ")
    assert LINEAR_KEY not in out
    assert env.core.stored("linear-api-key") == {"LINEAR_API_KEY": LINEAR_KEY}

    code, _, err = env.run(capsys, "setup", "repositories", "--default-repo", "acme/lib")
    assert err.startswith(f"error: {render.ERROR_TEXT['repo_not_installed']} Details: ")
    code, out, _ = env.run(capsys, "setup", "repositories", "--default-repo", "Acme/App", "--json")
    assert json.loads(out) == {"default_repo": "acme/app", "base_branches": {},
                               "mergers": ["acme/app"], "without_merger": [], "restarted": []}
    # `--branch` alone edits one entry and leaves the default repository as it is.
    code, out, _ = env.run(capsys, "setup", "repositories", "--branch", "acme/app=main", "--json")
    doc = json.loads(out)
    assert (doc["default_repo"], doc["base_branches"]) == ("acme/app", {"acme/app": "main"})
    code, out, _ = env.run(capsys, "setup", "repositories", "--branch", "acme/app=", "--json")
    doc = json.loads(out)
    assert (doc["default_repo"], doc["base_branches"]) == ("acme/app", {})

    # Notifications: the API sends a test message before it stores the channel.
    code, _, err = env.run(capsys, "setup", "notifications", "slack", "--channel", "C0SYMPHONY",
                           stdin="xoxb-wrong\n")
    assert err.startswith(f"error: {render.ERROR_TEXT['notification_unverified']} Details: ")
    assert env.core.stored("symphony-slack") == {}
    code, out, err = env.run(capsys, "setup", "notifications", "slack", "--channel", "C0SYMPHONY",
                             stdin=f"{slack_mock.DEFAULT_TOKEN}\n")
    assert code == 0, err
    assert out.startswith("Slack saved. The test message was sent.\n")

    # Assistant: the app token on stdin, verified with apps.connections.open and auth.test.
    code, _, err = env.run(capsys, "setup", "assistant", "--slack-channel", "C0SYMPHONY",
                           stdin="xapp-wrong\n")
    assert err == f"error: {render.ERROR_TEXT['slack_app_token_rejected']}\n"
    code, _, err = env.run(capsys, "setup", "assistant", "--discord-channel", "123")
    assert err == f"error: {render.ERROR_TEXT['discord_bot_token_missing']}\n"
    code, out, err = env.run(capsys, "setup", "assistant", "--slack-channel", "C0SYMPHONY",
                             "--repo", "acme/app", "--json",
                             stdin=f"{slack_mock.DEFAULT_APP_TOKEN}\n")
    assert code == 0, out + err
    doc = json.loads(out)
    assert doc["slack"] == {"connected": True, "channels": ["C0SYMPHONY"]}
    assert doc["repositories"] == ["acme/app"] and "symphony-assistant" in doc["restarted"]
    assert slack_mock.DEFAULT_APP_TOKEN not in out
    assert env.core.stored("symphony-slack")["SLACK_APP_TOKEN"] == slack_mock.DEFAULT_APP_TOKEN
    code, out, _ = env.run(capsys, "assistant", "status", "--json")
    status = json.loads(out)
    assert status["status"] == "todo" and status["running"] is False
    assert status["slack"] == {"configured": True, "connected": False, "channels": ["C0SYMPHONY"]}
    code, out, err = env.run(capsys, "setup", "notifications", "discord", "--webhook",
                             stdin=f"{env.vendors.discord}{discord_mock.DEFAULT_WEBHOOK_PATH}\n")
    assert code == 0, err
    code, out, _ = env.run(capsys, "setup", "notifications", "test")
    assert code == 0
    assert out == "  ✓ Slack: test message sent\n  ✓ Discord: test message sent\n"
    code, out, _ = env.run(capsys, "setup", "notifications", "test", "--json")
    results = json.loads(out)["results"]
    assert [(r["channel"], r["ok"], r["error"]) for r in results] == [
        ("slack", True, None), ("discord", True, None)]
    assert all(isinstance(r["at"], int) for r in results)
    code, out, _ = env.run(capsys, "setup", "notifications", "discord", "--remove", "--json")
    assert json.loads(out)["discord"]["configured"] is False
    assert json.loads(out)["slack"]["configured"] is True

    # Webhooks: shown once, then refused, then replaced with --rotate.
    code, out, _ = env.run(capsys, "setup", "webhooks")
    assert code == 0 and out.startswith("Save these secrets now. Symphony shows them only once.")
    assert f"  URL:    {env.url}/v1/webhooks/github\n" in out
    code, _, err = env.run(capsys, "setup", "webhooks")
    assert err == f"error: {render.ERROR_TEXT['webhooks_exist']}\n"
    code, out, _ = env.run(capsys, "setup", "webhooks", "--rotate", "--json")
    assert len(json.loads(out)["github"]["secret"]) == 64

    env.core.secrets["model-provider"].data = {"AWS_REGION": "dXMtZWFzdC0x"}
    code, out, _ = env.run(capsys, "setup", "--status", "--json")
    doc = json.loads(out)
    assert doc["complete"] is True, [(s["id"], s["status"], s["error"]) for s in doc["steps"]]


def test_viewer_cannot_change_the_setup(env, capsys):
    login(env, capsys, "viewer")
    assert env.run(capsys, "setup", "--status")[0] == 0
    code, _, err = env.run(capsys, "setup", "repositories", "--clear-default-repo")
    assert code == 1 and err == f"error: {render.ERROR_TEXT['forbidden']}\n"


def test_the_wizard_sets_up_a_fresh_install(env, tmp_path):
    env.core.secrets["model-provider"].data = {"AWS_REGION": "dXMtZWFzdC0x"}
    key = tmp_path / "app.pem"
    key.write_text(env.pem)
    answers = [env.app_id, env.installation, str(key),  # GitHub App
               "linear", "ENG",                          # tracker
               "",                                       # default repository: acme/app
               "",                                       # models are done: change them? no
               "slack", "bot", "C0SYMPHONY",             # notifications
               "skip",                                   # assistant: not now
               ""]                                       # create the webhook secrets? yes
    secrets = [LINEAR_KEY, slack_mock.DEFAULT_TOKEN]
    out = io.StringIO()
    wizard = setup.Wizard(Api(creds.Credential(url=env.url, token=env.tokens["admin"])), out,
                          ask=lambda _: answers.pop(0), secret=lambda _: secrets.pop(0),
                          style=setup.Style(out))
    assert wizard.run() == 0, out.getvalue()
    text = out.getvalue()
    assert "Default repository: acme/app." in text
    assert "Symphony setup is complete." in text and "Symphony is ready." in text
    assert LINEAR_KEY not in text and slack_mock.DEFAULT_TOKEN not in text
    assert not answers and not secrets
