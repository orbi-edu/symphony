"""The tracker methods the assistant adds: `search` (its `tracker_search`) and
`add_blocked_by` (its ticket links), on the memory tracker and against the Linear and GitHub
mocks."""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from scripts.mocks.github.server import MockServer
from scripts.mocks.linear import server as linear_mock
from scripts.mocks.linear.store import DEFAULT_API_KEY

from symphony import ghapp
from symphony.trackers.base import TrackerInputError
from symphony.trackers.github import GitHubTracker
from symphony.trackers.linear import LinearTracker
from symphony.trackers.memory import MemoryTracker

ACTIVE = ["Todo", "In Progress", "Rework"]
TERMINAL = ["Done", "Canceled", "Duplicate"]


# -- memory -----------------------------------------------------------------------------------
def test_memory_search_is_newest_first():
    tracker = MemoryTracker()
    tracker.create_issue("Fix the importer", "crash on empty file", labels=[])
    tracker.create_issue("Docs", "the importer guide", labels=[])
    tracker.create_issue("Unrelated", "nothing", labels=[])
    hits = tracker.search("IMPORTER")
    assert [h["title"] for h in hits] == ["Docs", "Fix the importer"]
    assert set(hits[0]) == {"identifier", "title", "state", "url"}
    assert len(tracker.search("importer", limit=1)) == 1


def test_memory_blocked_by_links_any_two_tickets():
    tracker = MemoryTracker()
    a = tracker.create_issue("A", "", labels=[])["identifier"]
    b = tracker.create_issue("B", "", labels=[])["identifier"]
    assert tracker.add_blocked_by(b, a) is True
    assert tracker.add_blocked_by(b, a) is False  # already there
    [issue] = [i for i in tracker._issues.values() if i.identifier == b]
    assert [r["identifier"] for r in issue.blocked_by] == [a] and not issue.dispatchable
    with pytest.raises(TrackerInputError):
        tracker.add_blocked_by(a, a)


# -- Linear -----------------------------------------------------------------------------------
@pytest.fixture
def linear(tmp_path, monkeypatch):
    srv = linear_mock.serve(0, tmp_path / "state")
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    monkeypatch.setenv("LINEAR_API_URL", f"http://127.0.0.1:{srv.server_port}/graphql")
    yield LinearTracker(api_key=DEFAULT_API_KEY, project_slug="", team_key="ENG",
                        active_states=ACTIVE, terminal_states=TERMINAL)
    srv.shutdown()
    srv.server_close()


def test_linear_search_on_the_board(linear):
    made = linear.create_issue("Fix the importer", "It crashes.", labels=[])
    linear.create_issue("Other work", "The importer docs.", labels=[])
    linear.create_issue("Unrelated", "Nothing here.", labels=[])
    hits = linear.search("importer")
    assert {h["title"] for h in hits} == {"Fix the importer", "Other work"}
    hit = next(h for h in hits if h["identifier"] == made["identifier"])
    assert hit["url"] == made["url"] and hit["state"]


def test_linear_blocked_by_is_a_blocks_relation(linear):
    a = linear.create_issue("Blocker", "", labels=[], state="Todo")["identifier"]
    b = linear.create_issue("Blocked", "", labels=[], state="Todo")["identifier"]
    assert linear.add_blocked_by(b, a) is True
    assert linear.add_blocked_by(b, a) is False  # a replay finds the relation
    [issue] = linear.fetch_issues_by_ids([linear._ticket(b)["id"]])
    assert [r["identifier"] for r in issue.blocked_by] == [a] and not issue.dispatchable


# -- GitHub -----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def github(tmp_path_factory):
    srv = MockServer(str(tmp_path_factory.mktemp("gh") / "state"), port=0).start()
    srv.mock.create_repo("acme", "app")
    srv.mock.create_repo("acme", "lib")
    srv.mock.store.install("acme", None, None)
    srv.mock.create_project("acme", 7, priority=True)
    yield srv
    srv.stop()


@pytest.fixture
def gh_tracker(github, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", github.url)
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"{github.url}/graphql")
    app = httpx.get(f"{github.url}/_mock/app").json()
    cred = ghapp.InstallationCredential(
        str(app["id"]), app["private_key"], app["env"]["GITHUB_APP_INSTALLATION_ID"],
        now_ms=lambda: int(time.time() * 1000), exchange=ghapp._http_exchange)
    return GitHubTracker("acme", 7, ACTIVE, TERMINAL, token=cred.token, default_repo="acme/app")


def test_github_search_over_the_owner(gh_tracker):
    made = gh_tracker.create_issue("Fix the importer", "It crashes.", labels=[])
    gh_tracker.create_issue("Unrelated", "Nothing here.", labels=[])
    hits = gh_tracker.search("importer")
    assert [h["identifier"] for h in hits] == [made["identifier"]]
    assert hits[0] == {"identifier": made["identifier"], "title": "Fix the importer",
                       "state": "open", "url": made["url"]}


def test_github_blocked_by_is_an_issue_dependency_across_repositories(gh_tracker):
    a = gh_tracker.create_issue("Library change", "", labels=[], repo="acme/lib")["identifier"]
    b = gh_tracker.create_issue("App change", "", labels=[], repo="acme/app")["identifier"]
    assert gh_tracker.add_blocked_by(b, a) is True
    assert gh_tracker.add_blocked_by(b, a) is False
    number = b.rsplit("-", 1)[1]
    deps = gh_tracker._rest_pages(f"/repos/acme/app/issues/{number}/dependencies/blocked_by")
    assert [d["title"] for d in deps] == ["Library change"]
