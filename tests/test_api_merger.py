"""Merger status and operator actions, one merger per repository."""

from __future__ import annotations

import pytest
from tests.api_support import FakeMerger, bearer, client_for, make_services, tokens_for

from symphony import wake
from symphony.api.control import MergerFleet
from symphony.merger.store import MergerStore, wake_target


def api_for(redis_client, merger):
    services = make_services(redis_client, merger=merger)
    return services, client_for(services), tokens_for(services)


@pytest.fixture
def api(redis_client):
    merger = FakeMerger()
    return (*api_for(redis_client, merger), merger)


def test_status_lists_each_repository_with_its_liveness(redis_client):
    merger = FakeMerger(["acme/lib", "acme/app"], dead={"acme/lib"})
    _, client, tokens = api_for(redis_client, merger)
    body = client.get("/v1/merger", headers=bearer(tokens["viewer"])).json()
    assert [entry["repo"] for entry in body["repos"]] == ["acme/app", "acme/lib"]
    assert [entry["alive"] for entry in body["repos"]] == [True, False]
    snapshot = merger.store("acme/app").snapshot()
    assert body["repos"][0] == snapshot and snapshot["repo"] == "acme/app"


def test_single_repository_needs_no_repo_and_wakes_that_merger(api, redis_client):
    _, client, tokens, merger = api
    response = client.post("/v1/merger/prs/12/bypass", headers=bearer(tokens["operator"]),
                           json={"reason": "hotfix"})
    assert response.status_code == 202
    body = response.json()
    assert (body["action"], body["actor"], body["repo"]) == ("bypass", "operator-person", "acme/app")
    assert merger.actions == [("acme/app", 12, "bypass", "operator-person", "hotfix")]
    assert wake.wait(redis_client, "test", wake_target("acme/app"), 0.01) == ["action:bypass:12"]
    assert wake.wait(redis_client, "test", wake.MERGER, 0.01) == []
    audit = client.get("/v1/audit", headers=bearer(tokens["viewer"])).json()["items"]
    assert audit[0]["action"] == "merger.bypass" and audit[0]["target"] == "pr:acme/app#12"


def test_several_repositories_require_a_registered_repo(redis_client):
    merger = FakeMerger(["acme/app", "acme/lib"])
    _, client, tokens = api_for(redis_client, merger)
    headers = bearer(tokens["operator"])
    missing = client.post("/v1/merger/prs/3/requeue", headers=headers, json={"reason": "flaky"})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "repo_required"
    unknown = client.post("/v1/merger/prs/3/requeue", headers=headers,
                          json={"reason": "flaky", "repo": "acme/other"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_repo"
    malformed = client.post("/v1/merger/prs/3/requeue", headers=headers,
                            json={"reason": "flaky", "repo": "not a repo"})
    assert malformed.status_code == 404
    ok = client.post("/v1/merger/prs/3/requeue", headers=headers,
                     json={"reason": "flaky", "repo": "Acme/Lib"})
    assert ok.status_code == 202 and ok.json()["repo"] == "acme/lib"
    assert merger.actions == [("acme/lib", 3, "requeue", "operator-person", "flaky")]
    assert wake.wait(redis_client, "test", wake_target("acme/lib"), 0.01) == ["action:requeue:3"]
    assert wake.wait(redis_client, "test", wake_target("acme/app"), 0.01) == []


def test_no_registered_repository_is_unknown_repo(redis_client):
    _, client, tokens = api_for(redis_client, FakeMerger([]))
    assert client.get("/v1/merger", headers=bearer(tokens["viewer"])).json() == {"repos": []}
    response = client.post("/v1/merger/prs/3/release", headers=bearer(tokens["operator"]),
                           json={"reason": "stuck"})
    assert response.status_code == 404 and response.json()["error"]["code"] == "unknown_repo"


def test_action_errors(api):
    _, client, tokens, merger = api
    headers = bearer(tokens["operator"])
    assert client.post("/v1/merger/prs/12/explode", headers=headers,
                       json={"reason": "x"}).status_code == 422
    assert client.post("/v1/merger/prs/0/release", headers=headers,
                       json={"reason": "x"}).status_code == 422
    rejected = client.post("/v1/merger/prs/404/release", headers=headers, json={"reason": "x"})
    assert rejected.status_code == 422 and "404" in rejected.json()["error"]["message"]
    assert client.post("/v1/merger/prs/3/release", headers=headers, json={}).status_code == 422
    assert client.post("/v1/merger/prs/3/release", headers=bearer(tokens["viewer"]),
                       json={"reason": "x"}).status_code == 403
    assert merger.actions == []


def test_overview_sums_every_repository(redis_client):
    _, client, tokens = api_for(redis_client, FakeMerger(["acme/app", "acme/lib"]))
    body = client.get("/v1/overview", headers=bearer(tokens["viewer"])).json()
    assert body["merger"] == {"reviewing": 2, "train": 0, "parked": 4}


def test_without_a_merger_the_routes_are_503(redis_client):
    services = make_services(redis_client)
    client, tokens = client_for(services), tokens_for(services)
    response = client.get("/v1/merger", headers=bearer(tokens["viewer"]))
    assert response.status_code == 503 and response.json()["error"]["code"] == "merger_unavailable"
    assert client.get("/v1/overview", headers=bearer(tokens["viewer"])).json()["merger"] == {
        "reviewing": 0, "train": 0, "parked": 0}


def test_real_stores_keep_repositories_apart(redis_client):
    """The production fleet over real MergerStores: registry, liveness, per-repo actions."""
    app_store = MergerStore(redis_client, "test", "acme/app")
    lib_store = MergerStore(redis_client, "test", "acme/lib")
    app_store.register()
    lib_store.register()
    redis_client.delete(lib_store.prefix + "heartbeat")  # the lib merger stopped beating
    _, client, tokens = api_for(redis_client, MergerFleet(redis_client, "test"))
    status = client.get("/v1/merger", headers=bearer(tokens["viewer"])).json()["repos"]
    assert [(e["repo"], e["alive"]) for e in status] == [("acme/app", True), ("acme/lib", False)]
    response = client.post("/v1/merger/prs/5/release", headers=bearer(tokens["operator"]),
                           json={"reason": "free the slot", "repo": "acme/lib"})
    assert response.status_code == 202
    assert [a["pr"] for a in lib_store.pending_actions()] == [5]
    assert app_store.pending_actions() == []
