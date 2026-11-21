"""Repository discovery: what each installed repository needs, filling only what the workflow
leaves unset."""

import json

import httpx
from tests.test_dispatch import FakeEffects, cfg

from symphony import discovery, state
from symphony.orchestrator import tick

RUNTIMES = frozenset({"node", "dotnet9", "dotnet10", "polyglot"})


def files(contents: dict[str, str]):
    return list(contents), lambda path: contents.get(path)


def test_a_monorepo_gets_one_component_per_project_directory():
    paths, read = files({
        "README.md": "", ".github/workflows/ci.yml": "",
        "admin/package.json": "{}",
        "api/global.json": '{"sdk": {"version": "10.0.100"}}', "api/src/Api/Api.csproj": "",
        "ia/pyproject.toml": "",
        "docs/index.md": "", "scripts/package.json": "{}",
    })
    found = discovery.detect(paths, read, RUNTIMES)
    assert found["components"] == {
        "admin": {"path": "admin", "runtime": "node"},
        "api": {"path": "api", "runtime": "dotnet10"},
        "ia": {"path": "ia", "runtime": "polyglot"},
    }
    assert found["runtime"] == "polyglot"


def test_component_names_follow_the_naming_rule_and_paths_keep_the_directory():
    paths, read = files({"Data_Science/pyproject.toml": "", "web.app/package.json": "{}"})
    assert discovery.detect(paths, read, RUNTIMES)["components"] == {
        "data-science": {"path": "Data_Science", "runtime": "polyglot"},
        "web-app": {"path": "web.app", "runtime": "node"},
    }


def test_a_single_project_gets_a_runtime_and_no_components():
    paths, read = files({"package.json": "{}", "src/index.ts": ""})
    assert discovery.detect(paths, read, RUNTIMES) == {"runtime": "node", "components": {}}
    paths, read = files({"pyproject.toml": "", "src/app/__init__.py": "", "tests/test_x.py": ""})
    assert discovery.detect(paths, read, RUNTIMES)["runtime"] == "polyglot"


def test_the_dotnet_version_comes_from_the_project_when_no_sdk_is_pinned():
    paths, read = files({"App.sln": "", "src/App/App.csproj":
                         "<Project><PropertyGroup><TargetFramework>net9.0</TargetFramework>"
                         "</PropertyGroup></Project>"})
    assert discovery.detect(paths, read, RUNTIMES)["runtime"] == "dotnet9"


def test_what_the_workflow_configures_always_wins():
    c = cfg(repo_runtimes={"acme/app": "node"},
            components={"acme/app": {"web": {"path": "web", "runtime": "node"}}})
    doc = {"items": [
        {"name": "acme/app", "runtime": "polyglot", "components": {"api": {"path": "api"}}},
        {"name": "acme/tool", "runtime": "dotnet10", "components": {}},
        {"name": "acme/old", "runtime": "node", "archived": True},
    ]}
    out = discovery.effective(c, doc)
    assert out.repo_runtimes == {"acme/app": "node", "acme/tool": "dotnet10"}
    assert out.components == {"acme/app": {"web": {"path": "web", "runtime": "node"}}}


def _github(trees: dict[str, dict], blobs: dict[str, str], archived=()):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [
                {"full_name": name, "default_branch": "main", "archived": name in archived}
                for name in [*trees, *archived]]})
        for name, tree in trees.items():
            if path == f"/repos/{name}/git/trees/main":
                return httpx.Response(200, json={"tree": [{"path": p, "type": "blob"} for p in tree],
                                                 "truncated": False})
            prefix = f"/repos/{name}/contents/"
            if path.startswith(prefix) and path[len(prefix):] in blobs:
                return httpx.Response(200, text=blobs[path[len(prefix):]])
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_pass_records_every_installed_repository(kv, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    http = _github({"acme/web": ["package.json", ".symphony/merger.md"],
                    "acme/svc": ["global.json", "Svc.csproj"]},
                   {"global.json": '{"sdk": {"version": "9.0.200"}}'}, archived=("acme/old",))
    refresher = discovery.Refresher(kv, lambda: "token", clock.now_ms, http=http)
    refresher.run_once(cfg())
    doc = kv.get(discovery.DISCOVERY_KEY)
    by_name = {i["name"]: i for i in doc["items"]}
    assert by_name["acme/web"]["runtime"] == "node" and by_name["acme/web"]["merger_policy"] is True
    assert by_name["acme/svc"]["runtime"] == "dotnet9"
    assert by_name["acme/old"]["archived"] is True and by_name["acme/old"]["runtime"] is None
    assert not refresher.due()
    clock.advance(discovery.REFRESH_EVERY_MS)
    assert refresher.due()


def test_a_failed_pass_keeps_the_last_good_repositories(kv, clock):
    kv.put(discovery.DISCOVERY_KEY, {"observed_at_ms": 0, "items": [{"name": "acme/web"}]})

    def broken(_):
        raise httpx.ConnectError("down")

    refresher = discovery.Refresher(kv, lambda: "token", clock.now_ms,
                                    http=httpx.Client(transport=httpx.MockTransport(broken)))
    refresher.run_once(cfg())
    doc = kv.get(discovery.DISCOVERY_KEY)
    assert doc["items"] == [{"name": "acme/web"}] and doc["error"] == "ConnectError"


def test_dispatch_uses_the_detected_runtime_of_an_unconfigured_repository(kv, clock, tracker):
    kv.put(discovery.DISCOVERY_KEY, {"observed_at_ms": clock.now_ms(), "items": [
        {"name": "acme/svc", "runtime": "dotnet9", "components": {}}]})
    tracker.add_issue("i1", "ENG-1", labels=("repo:acme/svc",))
    tick(kv, tracker, cfg(), clock.now_ms(), FakeEffects(), owner="d1")
    assert kv.get(state.issue_key("i1"))["runtime"] == "dotnet9"
    json.dumps(kv.get(discovery.DISCOVERY_KEY))  # the stored doc is plain JSON
