"""Every external endpoint follows its env var (internals §14); defaults stay production."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from symphony import endpoints

NAMES = endpoints.NAMES


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_production():
    assert endpoints.linear_api_url() == "https://api.linear.app/graphql"
    assert endpoints.github_api_url() == "https://api.github.com"
    assert endpoints.github_graphql_url() == "https://api.github.com/graphql"
    assert endpoints.github_server_url() == "https://github.com"
    assert endpoints.slack_api_url() == "https://slack.com/api"
    assert endpoints.discord_api_url() == "https://discord.com/api/v10"
    assert endpoints.overrides() == {}


def test_discord_bot_follows_discord_api_url(monkeypatch):
    from symphony import notify

    seen: list[str] = []

    def fake_post(url, **kw):
        seen.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    for name in ("SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "42")
    monkeypatch.setenv("DISCORD_API_URL", "http://127.0.0.1:4500/api/v10/")
    monkeypatch.setattr(httpx, "post", fake_post)
    notify.reset()
    try:
        assert notify.post("main_red", "endpoint check")
    finally:
        notify.reset()
    assert seen == ["http://127.0.0.1:4500/api/v10/channels/42/messages"]


def test_overrides_are_read_at_call_time_and_stripped(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:4200/")
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", "http://127.0.0.1:4200/graphql")
    assert endpoints.github_api_url() == "http://127.0.0.1:4200"
    assert endpoints.github_graphql_url() == "http://127.0.0.1:4200/graphql"
    assert endpoints.overrides() == {
        "GITHUB_API_URL": "http://127.0.0.1:4200/",
        "GITHUB_GRAPHQL_URL": "http://127.0.0.1:4200/graphql",
    }


def _recording_transport(seen: list[str], payload):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_the_linear_client_follows_linear_api_url(monkeypatch):
    from symphony.trackers.linear import LinearTracker

    monkeypatch.setenv("LINEAR_API_URL", "http://127.0.0.1:4100/graphql")
    seen: list[str] = []
    client = httpx.Client(transport=_recording_transport(seen, {"data": {"ok": True}}))
    LinearTracker("k", "proj", ["Todo"], ["Done"], client=client)._post("query { viewer { id } }", {})
    assert seen == ["http://127.0.0.1:4100/graphql"]


def test_github_rest_clients_follow_github_api_url(monkeypatch):
    from symphony import ghapp, reflector
    from symphony.codehost import GitHubReader
    from symphony.merger.github import RestGitHub

    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:4200/api/v3")
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", "http://127.0.0.1:4200/api/graphql")
    seen: list[str] = []

    GitHubReader(lambda repo: "t", http_get=lambda url, token, params: seen.append(url) or []).open_pulls("a/b")

    client = httpx.Client(transport=_recording_transport(seen, {"data": {}}))
    gh = RestGitHub("a/b", lambda: "t", http=client)
    gh._req("GET", "/repos/a/b/pulls/7")
    gh._graphql("query { viewer { login } }", {})

    # httpx keeps the base path and appends `/`, so `/repos/...` lands under /api/v3
    assert str(reflector.HttpxGitHub("t", "a/b")._client.base_url) == "http://127.0.0.1:4200/api/v3/"

    def fake_post(url, **kw):
        seen.append(url)
        return httpx.Response(201, json={"token": "ghs", "expires_at": "2026-11-16T00:00:00Z"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ghapp._http_exchange("jwt", "42", (), {})
    assert seen[0].startswith("http://127.0.0.1:4200/api/v3/repos/a/b/pulls")
    assert seen[1] == "http://127.0.0.1:4200/api/v3/repos/a/b/pulls/7"
    assert seen[2] == "http://127.0.0.1:4200/api/graphql"
    assert seen[3] == "http://127.0.0.1:4200/api/v3/app/installations/42/access_tokens"


def test_github_server_url_drives_pr_links_and_remote_slugs(monkeypatch):
    from symphony.admin_read import _pr_url
    from symphony.merger.context import Merger
    from symphony.workspace import workspace_matches_repo

    class Sandbox:
        name = "ws"

        def __init__(self, remote):
            self.remote = remote

        def exec_script(self, script, timeout_s=None):
            return 0, self.remote

    holder = SimpleNamespace(repo="acme/app")
    assert Merger.pr_url(holder, 3) == "https://github.com/acme/app/pull/3"
    assert workspace_matches_repo(Sandbox("https://github.com/acme/app.git"), "/w", "acme/app")
    assert _pr_url("https://github.com/acme/app/pull/3") == "https://github.com/acme/app/pull/3"

    monkeypatch.setenv("GITHUB_SERVER_URL", "https://127.0.0.1:4200/")
    assert Merger.pr_url(holder, 3) == "https://127.0.0.1:4200/acme/app/pull/3"
    assert workspace_matches_repo(Sandbox("https://127.0.0.1:4200/acme/app.git"), "/w", "acme/app")
    # The API keeps a PR link only when it points at the configured server.
    assert _pr_url("https://127.0.0.1:4200/acme/app/pull/3") == "https://127.0.0.1:4200/acme/app/pull/3"
    assert _pr_url("https://github.com/acme/app/pull/3") is None


def test_engine_env_carries_only_the_set_endpoints(monkeypatch):
    from symphony.config import Config
    from symphony.entry.run_issue import engine_env

    cfg = Config(tracker_kind="memory")
    assert not set(engine_env(cfg, "i", 1, "tok", ("a/b",))) & set(NAMES)
    monkeypatch.setenv("SLACK_API_URL", "http://127.0.0.1:4300/api")
    env = engine_env(cfg, "i", 1, "tok", ("a/b",))
    assert env["SLACK_API_URL"] == "http://127.0.0.1:4300/api"
    assert not set(env) & (set(NAMES) - {"SLACK_API_URL"})


def test_kube_passes_endpoints_to_jobs_and_workspace_only_when_set(monkeypatch):
    from symphony import kube
    from symphony.config import Config

    cfg = Config(tracker_kind="memory")
    monkeypatch.setenv("SYMPHONY_ORCHESTRATOR_IMAGE", "orch:test")

    def job_env():
        job = kube._job_manifest(cfg, "j", "symphony.entry.run_issue", {}, 60, "runner")
        return {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}

    def pod_env():
        container = kube._workspace_manifest(cfg, "ws", "img")["spec"]["containers"][0]
        return {e["name"]: e["value"] for e in container.get("env", [])}

    assert not set(job_env()) & set(NAMES)
    assert pod_env() == {}

    monkeypatch.setenv("LINEAR_API_URL", "http://linear-mock:4100/graphql")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github-mock:4200")
    want = {"LINEAR_API_URL": "http://linear-mock:4100/graphql", "GITHUB_SERVER_URL": "https://github-mock:4200"}
    assert {k: v for k, v in job_env().items() if k in NAMES} == want
    assert pod_env() == want
