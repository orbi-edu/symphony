"""First-run setup (`/v1/setup`) against the vendor mocks and a fake Kubernetes.

The fake Kubernetes keeps the install's pre-created Secrets and the (empty) workflow
ConfigMap; the real `KubernetesSecretStore` and `WorkflowRepository` run on it. A "restart"
copies the stored Secrets into the process env, the way a restarted API pod would see them.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from kubernetes.client import V1ConfigMap, V1ObjectMeta, V1Secret
from kubernetes.client.exceptions import ApiException
from scripts.mocks.discord import server as discord_mock
from scripts.mocks.github.server import MockServer
from scripts.mocks.linear import server as linear_mock
from scripts.mocks.linear.store import DEFAULT_API_KEY as LINEAR_KEY
from scripts.mocks.slack import server as slack_mock
from tests.api_support import FakeMerger, bearer, client_for, make_services, tokens_for

from symphony import state, wake
from symphony.admin_workflow import WorkflowRepository
from symphony.setup_secrets import GROUPS, SECRET_NAMES, KubernetesSecretStore

DEFAULT_WORKFLOW = """---
tracker:
  kind: memory
---
Work on {{ issue.identifier }}.
"""
FULL_PERMISSIONS = {"contents": "write", "pull_requests": "write", "checks": "read",
                    "actions": "write", "issues": "write", "statuses": "read",
                    "organization_projects": "write", "metadata": "read"}
ENV_NAMES = [name for names in GROUPS.values() for name in names]


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


@pytest.fixture(scope="module")
def vendors(tmp_path_factory):
    linear = linear_mock.serve(0, None)
    slack = slack_mock.serve(0, None)
    discord = discord_mock.serve(0, None)
    github = MockServer(str(tmp_path_factory.mktemp("gh") / "state"), port=0).start()
    github.mock.create_repo("acme", "app")
    github.mock.create_repo("acme", "lib")
    github.mock.create_project("acme", 7, priority=False)
    urls = SimpleNamespace(linear=_serve(linear), slack=_serve(slack), discord=_serve(discord),
                           github=github.url, github_server=github)
    yield urls
    for server in (linear, slack, discord):
        server.shutdown()
    github.stop()


class FakeCore:
    """The namespace as the install manifests leave it: empty integration Secrets, an empty
    workflow ConfigMap, and a `model-provider` Secret."""

    def __init__(self, missing=()):
        self.secrets = {
            name: V1Secret(metadata=V1ObjectMeta(name=name), data=None)
            for group, name in SECRET_NAMES.items() if name not in missing
        }
        self.secrets["symphony-api"].data = {"SYMPHONY_BOOTSTRAP_TOKEN": "c2VjcmV0"}
        self.config_map = V1ConfigMap(
            metadata=V1ObjectMeta(name="symphony-workflow", resource_version="1"), data=None
        )
        self.secret_patches = 0

    def read_namespaced_secret(self, *, name, namespace, _request_timeout):
        if name not in self.secrets:
            raise ApiException(status=404)
        return copy.deepcopy(self.secrets[name])

    def patch_namespaced_secret(self, *, name, namespace, body, _request_timeout):
        if name not in self.secrets:
            raise ApiException(status=404)
        self.secret_patches += 1
        data = dict(self.secrets[name].data or {})
        for key, value in (body.get("stringData") or {}).items():
            data[key] = base64.b64encode(value.encode()).decode()
        for key, value in (body.get("data") or {}).items():
            if value is None:
                data.pop(key, None)
        self.secrets[name].data = data

    def read_namespaced_config_map(self, *, name, namespace, _request_timeout):
        return copy.deepcopy(self.config_map)

    def replace_namespaced_config_map(self, *, name, namespace, body, _request_timeout):
        if body.metadata.resource_version != self.config_map.metadata.resource_version:
            raise ApiException(status=409)
        self.config_map = copy.deepcopy(body)
        self.config_map.metadata.resource_version = str(int(body.metadata.resource_version) + 1)
        return copy.deepcopy(self.config_map)

    def stored(self, name: str) -> dict[str, str]:
        data = self.secrets[name].data or {}
        return {k: base64.b64decode(v).decode() for k, v in data.items()}


class FakeApps:
    def __init__(self, mergers=("symphony-merger",)):
        self.mergers = list(mergers)
        self.patched: list[str] = []
        self.forbidden: set[str] = set()

    def list_namespaced_deployment(self, *, namespace, label_selector, _request_timeout):
        assert label_selector == "app=symphony,component=merger"
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=n))
                                      for n in self.mergers])

    def patch_namespaced_deployment(self, *, name, namespace, body, _request_timeout):
        if name in self.forbidden:
            raise ApiException(status=403)
        annotations = body["spec"]["template"]["metadata"]["annotations"]
        assert annotations["symphony.dev/restarted-at"].endswith("Z")
        self.patched.append(name)


@pytest.fixture
def setup(redis_client, vendors, monkeypatch, tmp_path):
    default = tmp_path / "WORKFLOW.default.md"
    default.write_text(DEFAULT_WORKFLOW)
    monkeypatch.setenv("SYMPHONY_DEFAULT_WORKFLOW", str(default))
    monkeypatch.setenv("LINEAR_API_URL", f"{vendors.linear}/graphql")
    monkeypatch.setenv("GITHUB_API_URL", vendors.github)
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"{vendors.github}/graphql")
    monkeypatch.setenv("SLACK_API_URL", f"{vendors.slack}/api")
    monkeypatch.setenv("DISCORD_API_URL", f"{vendors.discord}/api/v10")
    monkeypatch.delenv("SYMPHONY_PUBLIC_URL", raising=False)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    core, apps = FakeCore(), FakeApps()
    store = KubernetesSecretStore(core, "test", apps)
    merger = FakeMerger([])
    services = make_services(redis_client, secrets=store, merger=merger,
                             workflow=WorkflowRepository(core, "test"))
    services.linear_secret = services.github_secret = None  # read the env, as in production
    client = client_for(services)
    tokens = tokens_for(services)

    def restart():
        """What a restarted pod sees: the stored Secrets in its environment."""
        for group, name in SECRET_NAMES.items():
            if group in GROUPS:
                for key, value in core.stored(name).items():
                    if key in ENV_NAMES:
                        monkeypatch.setenv(key, value)

    yield SimpleNamespace(client=client, services=services, core=core, apps=apps, store=store,
                          admin=bearer(tokens["admin"]), viewer=bearer(tokens["viewer"]),
                          tokens=tokens, restart=restart, vendors=vendors, merger=merger,
                          redis=redis_client, monkeypatch=monkeypatch)
    services.linear_secret = services.github_secret = None


def steps(env) -> dict:
    body = env.client.get("/v1/setup", headers=env.viewer).json()
    return {step["id"]: step for step in body["steps"]} | {"_": body}


def app_body(env, **changes) -> dict:
    app = httpx.get(f"{env.vendors.github}/_mock/app").json()
    return {"app_id": str(app["id"]), "installation_id": app["env"]["GITHUB_APP_INSTALLATION_ID"],
            "private_key": app["private_key"]} | changes


def install(env, permissions=FULL_PERMISSIONS):
    env.vendors.github_server.mock.store.install("acme", ["acme/app"], permissions)


def assert_nothing_leaked(env, *secrets):
    """No credential in Redis (keys or values) or in the audit."""
    dump = json.dumps({k: env.redis.dump(k).hex() if env.redis.type(k) != "string" else env.redis.get(k)
                       for k in env.redis.keys("*")})
    audit = env.client.get("/v1/audit", headers=env.admin).text
    for secret in secrets:
        assert secret not in dump and secret.encode().hex() not in dump
        assert secret not in audit


def test_a_fresh_install_is_not_set_up(setup):
    env = setup
    body = env.client.get("/v1/setup", headers=env.viewer).json()
    assert body["complete"] is False and body["mode"] == "kubernetes"
    by_id = {s["id"]: s for s in body["steps"]}
    assert [s["id"] for s in body["steps"]] == [
        "tracker", "github_app", "repositories", "models", "notifications", "assistant",
        "webhooks", "skills"]
    assert {i: by_id[i]["status"] for i in by_id} == {
        "tracker": "todo", "github_app": "todo", "repositories": "todo",
        "models": "error", "notifications": "skipped", "assistant": "skipped",
        "webhooks": "skipped", "skills": "skipped"}
    assert by_id["models"]["error"]["code"] == "model_credential_missing"
    assert all(s["required"] is (s["id"] not in ("notifications", "assistant", "webhooks",
                                                  "skills"))
               for s in body["steps"])
    assert env.client.put("/v1/setup/repositories", headers=env.viewer,
                          json={"default_repo": "acme/app"}).status_code == 403


def test_linear_tracker_is_verified_then_stored_and_published(setup):
    env = setup
    bad = env.client.put("/v1/setup/tracker", headers=env.admin,
                         json={"kind": "linear", "api_key": "lin_api_wrong", "team_key": "ENG"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "linear_unauthorized"
    unknown = env.client.put("/v1/setup/tracker", headers=env.admin,
                             json={"kind": "linear", "api_key": LINEAR_KEY, "team_key": "NOPE"})
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "linear_team_not_found"
    assert env.core.secret_patches == 0 and env.core.config_map.data is None
    both = env.client.put("/v1/setup/tracker", headers=env.admin, json={
        "kind": "linear", "api_key": LINEAR_KEY, "team_key": "ENG", "project_slug": "x"})
    assert both.status_code == 422

    ok = env.client.put("/v1/setup/tracker", headers=env.admin,
                        json={"kind": "linear", "api_key": LINEAR_KEY, "team_key": "ENG"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["kind"] == "linear" and "Todo" in body["states"]
    assert body["restarted"] == ["symphony-daemon", "symphony-merger", "symphony-assistant", "symphony-api"]
    assert env.apps.patched == body["restarted"]
    assert LINEAR_KEY not in ok.text
    assert env.core.stored("linear-api-key") == {"LINEAR_API_KEY": LINEAR_KEY}
    published = env.core.config_map.data["WORKFLOW.md"]
    assert "kind: linear" in published and "$LINEAR_API_KEY" in published
    assert LINEAR_KEY not in published
    assert "Work on {{ issue.identifier }}." in published  # seeded from the image default
    assert wake.wait(env.redis, "test", wake.DAEMON, 0.01) == ["setup:workflow"]

    assert steps(env)["tracker"]["status"] == "todo"  # this pod has not restarted yet
    env.restart()
    tracker = steps(env)["tracker"]
    assert tracker["status"] == "done" and tracker["detail"]["team_key"] == "ENG"
    assert tracker["detail"]["credential"] is True and "Todo" in tracker["detail"]["states"]
    audit = env.client.get("/v1/audit", headers=env.admin).json()["items"]
    assert audit[0]["action"] == "setup.tracker" and audit[0]["target"] == "linear:ENG"
    assert_nothing_leaked(env, LINEAR_KEY)


def test_github_app_reports_repositories_and_missing_permissions(setup):
    env = setup
    install(env, {"contents": "write", "pull_requests": "write", "metadata": "read"})
    bad = env.client.put("/v1/setup/github-app", headers=env.admin,
                         json=app_body(env, private_key="not a key"))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_private_key"
    assert "not a key" not in bad.text
    missing = env.client.put("/v1/setup/github-app", headers=env.admin,
                             json=app_body(env, installation_id="999999"))
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "github_installation_not_found"
    assert env.core.secret_patches == 0

    ok = env.client.put("/v1/setup/github-app", headers=env.admin, json=app_body(env))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["repositories"] == ["acme/app"]
    assert set(body["missing_permissions"]) == {
        "checks: read", "actions: write", "statuses: read", "issues: write"}
    key = app_body(env)["private_key"]
    assert key not in ok.text
    assert env.core.stored("github-app")["GITHUB_APP_PRIVATE_KEY"] == key
    env.restart()
    step = steps(env)["github_app"]
    assert step["status"] == "error" and step["error"]["code"] == "github_permissions_missing"
    install(env)
    # Saving again drops the cached verification, so the new permissions show at once.
    env.client.put("/v1/setup/github-app", headers=env.admin, json=app_body(env))
    assert steps(env)["github_app"]["status"] == "done"
    assert_nothing_leaked(env, key)


def test_github_tracker_needs_the_app_and_a_status_field(setup):
    env = setup
    body = {"kind": "github", "owner": "acme", "project_number": 7}
    first = env.client.put("/v1/setup/tracker", headers=env.admin, json=body)
    assert first.status_code == 409 and first.json()["error"]["code"] == "github_app_required"
    install(env)
    env.client.put("/v1/setup/github-app", headers=env.admin, json=app_body(env))
    env.restart()
    no_field = env.client.put("/v1/setup/tracker", headers=env.admin,
                              json=body | {"status_field": "Lane"})
    assert no_field.json()["error"]["code"] == "github_status_field_missing"
    no_project = env.client.put("/v1/setup/tracker", headers=env.admin,
                                json=body | {"project_number": 99})
    assert no_project.json()["error"]["code"] == "github_project_not_found"
    ok = env.client.put("/v1/setup/tracker", headers=env.admin, json=body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["states"] and ok.json()["restarted"] == []
    assert steps(env)["tracker"]["status"] == "done"


def test_repositories_need_the_app_installation_and_a_live_merger(setup):
    env = setup
    install(env)
    env.client.put("/v1/setup/github-app", headers=env.admin, json=app_body(env))
    env.restart()
    outside = env.client.put("/v1/setup/repositories", headers=env.admin,
                             json={"default_repo": "acme/lib"})
    assert outside.status_code == 422 and outside.json()["error"]["code"] == "repo_not_installed"
    ok = env.client.put("/v1/setup/repositories", headers=env.admin,
                        json={"default_repo": "Acme/App"})
    assert ok.status_code == 200
    assert ok.json() == {"default_repo": "acme/app", "base_branches": {},
                         "repo_runtimes": {}, "components": {},
                         "mergers": [], "without_merger": ["acme/app"], "restarted": []}
    step = steps(env)["repositories"]
    assert step["status"] == "error" and step["error"]["code"] == "merger_missing"
    env.merger._repos = ["acme/app"]
    env.merger.dead = {"acme/app"}
    assert steps(env)["repositories"]["status"] == "error"  # registered but not beating
    env.merger.dead = set()
    assert steps(env)["repositories"]["status"] == "done"
    cleared = env.client.put("/v1/setup/repositories", headers=env.admin, json={"default_repo": None})
    assert cleared.json()["default_repo"] is None


def test_models_count_stored_keys_and_host_credentials(setup):
    env = setup
    assert steps(env)["models"]["status"] == "error"
    env.core.secrets["model-provider"].data = {"AWS_REGION": "dXMtZWFzdC0x"}
    models = steps(env)["models"]
    assert models["status"] == "done"
    assert {r["credential"] for r in models["detail"]["roles"]} == {"host"}


def test_notifications_are_tested_before_they_are_stored(setup):
    env = setup
    bad = env.client.put("/v1/setup/notifications", headers=env.admin,
                         json={"slack": {"bot_token": "xoxb-wrong", "channel": "C0SYMPHONY"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "notification_unverified"
    assert env.core.stored("symphony-slack") == {}
    ok = env.client.put("/v1/setup/notifications", headers=env.admin, json={
        "slack": {"bot_token": slack_mock.DEFAULT_TOKEN, "channel": "C0SYMPHONY"},
        "discord": {"webhook_url": f"{env.vendors.discord}{discord_mock.DEFAULT_WEBHOOK_PATH}"},
    })
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["slack"]["configured"] and body["slack"]["last_test"]["ok"]
    assert body["discord"]["configured"] and body["discord"]["last_test"]["ok"]
    assert slack_mock.DEFAULT_TOKEN not in ok.text
    assert env.core.stored("symphony-slack") == {
        "SLACK_BOT_TOKEN": slack_mock.DEFAULT_TOKEN, "SLACK_CHANNEL": "C0SYMPHONY"}
    env.restart()
    assert steps(env)["notifications"]["status"] == "done"
    tested = env.client.post("/v1/setup/notifications/test", headers=env.admin).json()
    assert tested["results"]["slack"]["ok"] and tested["results"]["discord"]["ok"]
    removed = env.client.put("/v1/setup/notifications", headers=env.admin, json={"discord": None})
    assert removed.json()["discord"]["configured"] is False
    assert removed.json()["slack"]["configured"] is True  # absent key: unchanged
    assert env.core.stored("symphony-discord") == {}
    assert_nothing_leaked(env, slack_mock.DEFAULT_TOKEN)


def test_webhook_secrets_are_shown_once_and_deliveries_complete_the_step(setup):
    env = setup
    env.monkeypatch.setenv("SYMPHONY_PUBLIC_URL", "https://symphony.example.test/")
    first = env.client.post("/v1/setup/webhooks", headers=env.admin)
    assert first.status_code == 200 and first.headers["Cache-Control"] == "no-store"
    body = first.json()
    assert body["linear"]["url"] == "https://symphony.example.test/v1/webhooks/linear"
    assert body["github"]["url"] == "https://symphony.example.test/v1/webhooks/github"
    assert "pull_request" in body["github"]["events"]
    assert len(body["linear"]["secret"]) == 64 and body["linear"]["secret"] != body["github"]["secret"]
    again = env.client.post("/v1/setup/webhooks", headers=env.admin)
    assert again.status_code == 409 and again.json()["error"]["code"] == "webhooks_exist"
    rotated = env.client.post("/v1/setup/webhooks", headers=env.admin, json={"rotate": True}).json()
    assert rotated["github"]["secret"] != body["github"]["secret"]
    env.restart()
    assert steps(env)["webhooks"]["status"] == "todo"
    payload = json.dumps({"zen": "hi"}).encode()
    signature = hmac.new(rotated["github"]["secret"].encode(), payload, hashlib.sha256).hexdigest()
    delivery = env.client.post("/v1/webhooks/github", content=payload, headers={
        "X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "ping"})
    assert delivery.status_code == 202
    hooks = steps(env)["webhooks"]
    assert hooks["status"] == "done"  # GitHub is the only source in use without a Linear tracker
    assert hooks["detail"]["github"]["last_delivery_at"]
    assert_nothing_leaked(env, rotated["github"]["secret"], rotated["linear"]["secret"])


def test_store_errors_are_clear(redis_client, vendors, monkeypatch, tmp_path):
    default = tmp_path / "WORKFLOW.default.md"
    default.write_text(DEFAULT_WORKFLOW)
    monkeypatch.setenv("SYMPHONY_DEFAULT_WORKFLOW", str(default))
    monkeypatch.setenv("LINEAR_API_URL", f"{vendors.linear}/graphql")
    core, apps = FakeCore(missing=("linear-api-key",)), FakeApps()
    services = make_services(redis_client, secrets=KubernetesSecretStore(core, "test", apps),
                             workflow=WorkflowRepository(core, "test"))
    client, admin = client_for(services), bearer(tokens_for(services)["admin"])
    missing = client.put("/v1/setup/tracker", headers=admin,
                         json={"kind": "linear", "api_key": LINEAR_KEY, "team_key": "ENG"})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "secret_missing"
    assert core.config_map.data is None  # nothing published when the key could not be stored
    core.secrets["linear-api-key"] = V1Secret(metadata=V1ObjectMeta(name="linear-api-key"))
    apps.forbidden = {"symphony-merger"}
    forbidden = client.put("/v1/setup/tracker", headers=admin,
                           json={"kind": "linear", "api_key": LINEAR_KEY, "team_key": "ENG"})
    assert forbidden.status_code == 503
    error = forbidden.json()["error"]
    assert error["code"] == "restart_forbidden" and "symphony-merger" in error["message"]


def test_mcp_setup_status_and_overview_daemon_setup(setup):
    env = setup
    env.redis.set(state.SETUP_KEY, json.dumps({"status": "incomplete", "reason": "no tracker",
                                               "at": 1}))
    overview = env.client.get("/v1/overview", headers=env.viewer).json()
    assert overview["daemon"]["setup"] == "incomplete"
    result = env.client.post("/mcp", headers=env.viewer, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "setup_status", "arguments": {}}}).json()["result"]
    assert result["structuredContent"]["complete"] is False
    assert [s["id"] for s in result["structuredContent"]["steps"]][0] == "tracker"


def test_assistant_is_verified_then_published_and_restarted(setup):
    env = setup
    put = lambda body: env.client.put("/v1/setup/assistant", headers=env.admin, json=body)  # noqa: E731
    slack = {"app_token": slack_mock.DEFAULT_APP_TOKEN, "channels": ["C0SYMPHONY"]}
    assert put({"slack": slack}).json()["error"]["code"] == "slack_bot_token_missing"
    assert put({}).json()["error"]["code"] == "invalid_request"
    assert put({"slack": {**slack, "channels": ["#general"]}}).status_code == 422
    assert env.client.put("/v1/setup/notifications", headers=env.admin, json={
        "slack": {"bot_token": slack_mock.DEFAULT_TOKEN, "channel": "C0SYMPHONY"},
        "discord": {"bot_token": discord_mock.DEFAULT_TOKEN,
                    "channel_id": discord_mock.DEFAULT_CHANNEL["id"]}}).status_code == 200
    env.restart()
    env.apps.patched.clear()
    # A wrong app token, or the bot token where the app token goes, stores nothing.
    for token in ("xapp-wrong", slack_mock.DEFAULT_TOKEN):
        bad = put({"slack": {**slack, "app_token": token}})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "slack_app_token_rejected"
        assert token not in bad.text
    assert "SLACK_APP_TOKEN" not in env.core.stored("symphony-slack") and not env.apps.patched
    assert put({"slack": {"channels": ["C0SYMPHONY"]}}).json()["error"]["code"] == \
        "slack_app_token_missing"

    ok = put({"slack": slack, "discord": {"channels": ["1100000000000000001"]},
              "repositories": ["Acme/App"]})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {
        "slack": {"connected": True, "channels": ["C0SYMPHONY"]},
        "discord": {"connected": True, "channels": ["1100000000000000001"], "intent": "full"},
        "repositories": ["acme/app"],
        "restarted": ["symphony-daemon", "symphony-merger", "symphony-assistant", "symphony-api"]}
    assert env.apps.patched == ok.json()["restarted"]
    assert env.core.stored("symphony-slack")["SLACK_APP_TOKEN"] == slack_mock.DEFAULT_APP_TOKEN
    values = env.services.workflow.read()["values"]
    assert values["assistant.enabled"] is True
    assert json.loads(values["assistant.slack.channels"]) == ["C0SYMPHONY"]
    assert json.loads(values["assistant.discord.channels"]) == ["1100000000000000001"]
    assert json.loads(values["assistant.repositories"]) == ["acme/app"]
    env.restart()
    step = steps(env)["assistant"]
    assert step["status"] == "todo"  # no heartbeat yet
    assert step["detail"]["slack"] == {"configured": True, "connected": False,
                                       "channels": ["C0SYMPHONY"]}
    assert step["detail"]["discord"]["intent"] == "full"
    key = f"sym:{env.services.namespace}:assistant:heartbeat"
    env.redis.set(key, json.dumps({"at": 5, "state": "running", "missing": [],
                                   "slack": {"connected": True}, "discord": {"connected": False},
                                   "threads": 2, "active": 1, "queued": 0}))
    step = steps(env)["assistant"]
    assert step["status"] == "done" and step["error"] is None
    assert step["detail"] | {} == {
        "enabled": True, "running": True, "heartbeat_at": 5, "threads": 2,
        "repositories": ["acme/app"],
        "slack": {"configured": True, "connected": True, "channels": ["C0SYMPHONY"]},
        "discord": {"configured": True, "connected": False,
                    "channels": ["1100000000000000001"], "intent": "full"}}

    # Slack alone again: the stored app token is reused, Discord gets no channels, the
    # repositories stay, and only the assistant restarts.
    env.apps.patched.clear()
    again = put({"slack": {"channels": ["C0SYMPHONY"]}})
    assert again.status_code == 200 and again.json()["discord"] is None
    assert again.json()["repositories"] == ["acme/app"]
    assert again.json()["restarted"] == env.apps.patched == ["symphony-assistant"]
    assert json.loads(env.services.workflow.read()["values"]["assistant.discord.channels"]) == []

    # Discord without the message-content intent is refused with how to fix it.
    httpx.post(f"{env.vendors.discord}/_mock/application", json={"flags": 0})
    try:
        refused = put({"discord": {"channels": ["1100000000000000001"]}})
    finally:
        httpx.post(f"{env.vendors.discord}/_mock/application",
                   json={"flags": discord_mock.DEFAULT_APP_FLAGS})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "message_content_intent_missing"
    assert "Message Content Intent" in refused.json()["error"]["message"]
    assert_nothing_leaked(env, slack_mock.DEFAULT_APP_TOKEN, slack_mock.DEFAULT_TOKEN,
                          discord_mock.DEFAULT_TOKEN)


def test_base_branches_are_validated_published_and_restarted(setup):
    """PUT /v1/setup/repositories with base_branches validates each branch against the
    GitHub App, publishes the WORKFLOW, restarts the Deployments, and returns the map.
    A nonexistent branch gives 422 branch_not_found. A null value clears the entry.
    An unchanged map does not restart. A branch with a slash is URL-encoded."""
    from scripts.mocks.github import gitrepo

    env = setup
    install(env)
    env.client.put("/v1/setup/github-app", headers=env.admin, json=app_body(env))
    env.restart()

    # Create a branch with a slash in the mock repo.
    repo_obj = env.vendors.github_server.mock.find_repo("acme", "app")
    repo_path = env.vendors.github_server.mock.path(repo_obj)
    main_sha = gitrepo.branch_sha(repo_path, "main")
    gitrepo.update_ref(repo_path, "refs/heads/release/1.0", main_sha)

    # Set a base branch with a slash (URL-encoded path).
    env.apps.patched.clear()
    ok = env.client.put("/v1/setup/repositories", headers=env.admin, json={
        "default_repo": "Acme/App", "base_branches": {"acme/app": "release/1.0"}
    })
    assert ok.status_code == 200
    body = ok.json()
    assert body["default_repo"] == "acme/app"
    assert body["base_branches"] == {"acme/app": "release/1.0"}
    # Deployments were restarted (github-app group → daemon, merger, assistant, api).
    assert len(env.apps.patched) > 0

    # The step detail includes base_branches.
    step = steps(env)["repositories"]
    assert step["detail"]["base_branches"] == {"acme/app": "release/1.0"}

    # Unchanged map → NO restart.
    env.apps.patched.clear()
    same = env.client.put("/v1/setup/repositories", headers=env.admin, json={
        "base_branches": {"acme/app": "release/1.0"}
    })
    assert same.status_code == 200
    assert same.json()["restarted"] == []
    assert env.apps.patched == []

    # A nonexistent branch is refused.
    bad = env.client.put("/v1/setup/repositories", headers=env.admin, json={
        "base_branches": {"acme/app": "nonexistent-branch"}
    })
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "branch_not_found"

    # Clearing: null value removes the entry.
    env.apps.patched.clear()
    cleared = env.client.put("/v1/setup/repositories", headers=env.admin, json={
        "base_branches": {"acme/app": None}
    })
    assert cleared.status_code == 200
    assert cleared.json()["base_branches"] == {}
    assert len(env.apps.patched) > 0  # changed from non-empty to empty → restart

    # Without base_branches field, the existing map is returned unchanged.
    env.client.put("/v1/setup/repositories", headers=env.admin, json={
        "base_branches": {"acme/app": "main"}
    })
    no_field = env.client.put("/v1/setup/repositories", headers=env.admin, json={
        "default_repo": "acme/app"
    })
    assert no_field.status_code == 200
    assert no_field.json()["base_branches"] == {"acme/app": "main"}
