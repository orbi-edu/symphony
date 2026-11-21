"""Provisioning: labels on the board and in each repository, and a default merger policy PR."""

import json

import httpx
from tests.test_dispatch import cfg

from symphony import discovery, provision
from symphony.trackers.memory import MemoryTracker


class FakeGitHub:
    """The REST calls provisioning makes, against an in-memory account."""

    def __init__(self, repos: dict[str, dict]):
        self.repos = repos  # name -> {labels: set, branches: set, files: dict, pulls: list}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        for name, repo in self.repos.items():
            base = f"/repos/{name}"
            if not path.startswith(base + "/"):
                continue
            rest = path[len(base):]
            body = json.loads(request.content or b"{}")
            if rest == "/labels" and method == "GET":
                return httpx.Response(200, json=[{"name": n} for n in sorted(repo["labels"])])
            if rest == "/labels" and method == "POST":
                repo["labels"].add(body["name"])
                return httpx.Response(201, json={"name": body["name"]})
            if rest == "/pulls" and method == "GET":
                head = request.url.params["head"].split(":", 1)[1]
                return httpx.Response(200, json=[p for p in repo["pulls"] if p["head"] == head])
            if rest == "/pulls" and method == "POST":
                pr = {"head": body["head"], "html_url": f"https://github.test/{name}/pull/{len(repo['pulls']) + 1}"}
                repo["pulls"].append(pr)
                return httpx.Response(201, json=pr)
            if rest.startswith("/git/ref/heads/"):
                if repo.get("empty"):
                    return httpx.Response(409, json={"message": "Git Repository is empty."})
                return httpx.Response(200, json={"object": {"sha": "a" * 40}})
            if rest == "/git/refs" and method == "POST":
                branch = body["ref"].removeprefix("refs/heads/")
                if branch in repo["branches"]:
                    return httpx.Response(422, json={"message": "Reference already exists"})
                repo["branches"].add(branch)
                return httpx.Response(201, json={})
            if rest.startswith("/contents/") and method == "PUT":
                repo["files"][rest[len("/contents/"):]] = body["content"]
                return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "Not Found"})


def account(*names):
    return {n: {"labels": {"reviewed"}, "branches": set(), "files": {}, "pulls": []} for n in names}


def items():
    return [
        {"name": "acme/web", "default_branch": "main", "archived": False, "merger_policy": False,
         "components": {}},
        {"name": "acme/mono", "default_branch": "trunk", "archived": False, "merger_policy": True,
         "components": {"api": {"path": "api", "runtime": "dotnet10"},
                        "data-science": {"path": "data_science", "runtime": "polyglot"}}},
        {"name": "acme/old", "default_branch": "main", "archived": True, "merger_policy": False},
    ]


def run(kv, clock, gh, tracker, c=None, found=None):
    found = found if found is not None else items()
    provision.run(kv, tracker, c or cfg(required_labels=("symphony",)), found, "token",
                  httpx.Client(transport=httpx.MockTransport(gh)), clock.now_ms)
    return {i["name"]: i for i in found}


def test_every_live_repository_gets_its_labels_and_the_board_its_repo_and_component_labels(
        kv, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    gh, tracker = FakeGitHub(account("acme/web", "acme/mono", "acme/old")), MemoryTracker()
    by_name = run(kv, clock, gh, tracker)
    for name in ("acme/web", "acme/mono"):
        assert {*provision.MERGER_LABELS, "symphony", "symphony-learnings"} <= gh.repos[name]["labels"]
    assert "reviewed" not in by_name["acme/web"]["provisioning"]["labels_created"]  # existed
    assert gh.repos["acme/old"]["labels"] == {"reviewed"}  # archived: untouched
    assert tracker.labels == {"repo:acme/web", "repo:acme/mono", "component:api",
                              "component:data-science"}


def test_a_repository_without_a_policy_gets_one_pull_request_once(kv, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    gh = FakeGitHub(account("acme/web", "acme/mono"))
    by_name = run(kv, clock, gh, MemoryTracker())
    [pr] = gh.repos["acme/web"]["pulls"]
    assert pr["head"] == provision.POLICY_BRANCH
    assert by_name["acme/web"]["provisioning"]["policy_pr"] == pr["html_url"]
    assert provision.POLICY_PATH in gh.repos["acme/web"]["files"]
    assert gh.repos["acme/mono"]["pulls"] == []  # it has a policy already
    # Later passes: within a day nothing is redone; after it, the existing PR is found, even
    # if a person closed it, and no second one is opened.
    calls = len(gh.calls)
    run(kv, clock, gh, MemoryTracker())
    assert len(gh.calls) == calls
    clock.advance(provision.RECHECK_MS)
    run(kv, clock, gh, MemoryTracker())
    assert len(gh.repos["acme/web"]["pulls"]) == 1


def test_a_failure_is_recorded_on_the_repository_and_retried_on_the_next_pass(
        kv, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")

    def forbidden(request):
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    by_name = run(kv, clock, forbidden, MemoryTracker(), found=items()[:1])
    assert "answered 403" in by_name["acme/web"]["provisioning"]["error"]
    gh = FakeGitHub(account("acme/web"))
    by_name = run(kv, clock, gh, MemoryTracker(), found=items()[:1])
    assert by_name["acme/web"]["provisioning"]["error"] is None and gh.repos["acme/web"]["pulls"]


def test_an_empty_repository_waits_for_its_first_commit_without_an_error(
        kv, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    gh = FakeGitHub(account("acme/web"))
    gh.repos["acme/web"]["empty"] = True
    by_name = run(kv, clock, gh, MemoryTracker(), found=items()[:1])
    assert by_name["acme/web"]["provisioning"]["error"] is None
    assert by_name["acme/web"]["provisioning"]["policy_pr"] is None
    assert gh.repos["acme/web"]["pulls"] == []
    gh.repos["acme/web"]["empty"] = False  # the first push
    clock.advance(provision.RECHECK_MS)
    by_name = run(kv, clock, gh, MemoryTracker(), found=items()[:1])
    assert by_name["acme/web"]["provisioning"]["policy_pr"] == gh.repos["acme/web"]["pulls"][0]["html_url"]


def test_the_discovery_pass_stores_what_provisioning_did(kv, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")

    def github(request):
        if request.url.path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [
                {"full_name": "acme/web", "default_branch": "main", "archived": False}]})
        if request.url.path == "/repos/acme/web/git/trees/main":
            return httpx.Response(200, json={"tree": [{"path": "package.json", "type": "blob"}]})
        return httpx.Response(404, json={})

    seen = []
    refresher = discovery.Refresher(
        kv, lambda: "token", clock.now_ms, http=httpx.Client(transport=httpx.MockTransport(github)),
        after=lambda found, _cfg: [i.update(provisioning={"policy_pr": "https://x/1"}) or seen.append(i)
                                   for i in found])
    refresher.run_once(cfg())
    [item] = kv.get(discovery.DISCOVERY_KEY)["items"]
    assert item["provisioning"] == {"policy_pr": "https://x/1"} and seen
