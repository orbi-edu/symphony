"""GitHub tracker (`tracker.kind: github`) against the GitHub mock: Projects v2 board reads,
state moves, comments and the Workpad, sub-issues with blocks, the main-red ticket, evidence,
the `github_graphql` tool and the webhook wakes. The mock validates every request against
GitHub's REST description and GraphQL schema."""

from __future__ import annotations

import time

import httpx
import pytest
from scripts.mocks.github.server import MockServer
from scripts.mocks.github.state import node_id

from symphony import ghapp, wake
from symphony.api.webhooks import github_wakes
from symphony.config import ConfigError, parse_workflow
from symphony.trackers.base import SubIssue, TrackerInputError
from symphony.trackers.github import GitHubTracker

OWNER = "acme"
NUMBER = 7
ACTIVE = ["Todo", "In Progress", "Rework"]
TERMINAL = ["Done", "Canceled"]


@pytest.fixture(scope="module")
def gh(tmp_path_factory):
    srv = MockServer(str(tmp_path_factory.mktemp("gh") / "state"), port=0).start()
    m = srv.mock
    for name in ("app", "lib", "symphony-k8s"):
        m.create_repo(OWNER, name)
    m.store.install(OWNER, None, None)
    m.create_project(OWNER, NUMBER, priority=True)
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def endpoints(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", gh.url)
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"{gh.url}/graphql")


@pytest.fixture(scope="module")
def token(gh):
    app = httpx.get(f"{gh.url}/_mock/app").json()
    cred = ghapp.InstallationCredential(
        str(app["id"]), app["private_key"], app["env"]["GITHUB_APP_INSTALLATION_ID"],
        now_ms=lambda: int(time.time() * 1000), exchange=ghapp._http_exchange,
    )
    return cred.token


@pytest.fixture
def tracker(token):
    return GitHubTracker(OWNER, NUMBER, ACTIVE, TERMINAL, token=token, default_repo="acme/app")


def ticket(gh, title, *, repo="acme/app", labels=("symphony",), status="Todo", board=True) -> dict:
    body = {"repo": repo, "user": "reporter", "title": title, "body": f"{title} body",
            "labels": list(labels)}
    if board:
        body |= {"project": NUMBER, "status": status}
    return httpx.post(f"{gh.url}/_mock/issues", json=body).json()


def rest(gh, token, method, path, **kw) -> httpx.Response:
    return httpx.request(method, f"{gh.url}{path}", headers={
        "Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json"}, **kw)


def by_identifier(issues, identifier):
    return next(i for i in issues if i.identifier == identifier)


class Records:
    """In-memory `SubIssueRecords` (hosttools keeps them in Redis)."""

    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.relations: set[tuple[str, str]] = set()

    def issue(self, key):
        return self.issues.get(key)

    def save_issue(self, key, record):
        self.issues[key] = record

    def relation(self, blocker, blocked):
        return (blocker, blocked) in self.relations

    def save_relation(self, blocker, blocked):
        self.relations.add((blocker, blocked))


def test_candidates_with_states_labels_priority_and_repos(gh, tracker):
    m = gh.mock
    a = ticket(gh, "Todo ticket")
    b = ticket(gh, "Lib ticket", labels=("symphony", "repo:acme/lib", "Backend"), status="In Progress")
    m.set_item_field(OWNER, NUMBER, b["item_id"], "Priority", 2)
    done = ticket(gh, "Finished", status="Done")
    off_board = ticket(gh, "Not on the board", board=False)
    m.add_project_item(OWNER, NUMBER, draft_title="Just an idea")
    k8s = ticket(gh, "Dashed repo", repo="acme/symphony-k8s")

    found = tracker.fetch_issues_by_states(["todo", "In Progress"])
    ids = {i.identifier for i in found}
    assert f"app-{off_board['issue']['number']}" not in ids
    assert f"app-{done['issue']['number']}" not in ids
    first = by_identifier(found, f"app-{a['issue']['number']}")
    assert (first.state, first.state_type, first.dispatchable) == ("Todo", "active", True)
    assert first.labels == ("symphony",) and first.title == "Todo ticket"
    assert first.description == "Todo ticket body" and first.priority is None
    assert first.target_repos == ("acme/app",) and first.branch_name == f"symphony/{first.identifier}"
    assert first.native_ref == f"acme/app#{a['issue']['number']}"
    assert first.url.endswith(f"/acme/app/issues/{a['issue']['number']}") and first.created_at
    second = by_identifier(found, f"app-{b['issue']['number']}")
    assert second.state == "In Progress" and second.priority == 2
    assert second.labels == ("symphony", "repo:acme/lib", "backend")
    assert second.target_repo == "acme/lib"
    assert by_identifier(found, f"symphony-k8s-{k8s['issue']['number']}").target_repo == "acme/symphony-k8s"

    terminal = tracker.fetch_issues_by_states(TERMINAL)
    closed = by_identifier(terminal, f"app-{done['issue']['number']}")
    assert closed.state_type == "terminal" and not closed.dispatchable
    assert tracker.fetch_issues_by_states([]) == []

    unknown = node_id("I", 999999, 1)  # well-formed, but no such repository
    fetched = tracker.fetch_issues_by_ids(
        [first.id, off_board["issue"]["node_id"], unknown, "I_bm9wZTo5OTk5"])
    assert [i.identifier for i in fetched] == [first.identifier]


def test_blockers_hold_back_until_terminal(gh, tracker, token):
    blocker = ticket(gh, "Blocker")
    blocked = ticket(gh, "Blocked")
    n = blocked["issue"]["number"]
    added = rest(gh, token, "POST", f"/repos/acme/app/issues/{n}/dependencies/blocked_by",
                 json={"issue_id": blocker["issue"]["id"]})
    assert added.status_code == 201
    [issue] = tracker.fetch_issues_by_ids([blocked["issue"]["node_id"]])
    assert not issue.dispatchable
    assert issue.blocked_by == ({"id": blocker["issue"]["node_id"],
                                 "identifier": f"app-{blocker['issue']['number']}", "state": "Todo"},)
    assert tracker.move(f"app-{blocker['issue']['number']}", "Done") is True
    [issue] = tracker.fetch_issues_by_ids([blocked["issue"]["node_id"]])
    assert issue.blocked_by == () and issue.dispatchable


def test_closed_issue_is_terminal_whatever_its_column(gh, tracker, token):
    t = ticket(gh, "Closed early")
    rest(gh, token, "PATCH", f"/repos/acme/app/issues/{t['issue']['number']}", json={"state": "closed"})
    [issue] = tracker.fetch_issues_by_ids([t["issue"]["node_id"]])
    assert (issue.state, issue.state_type, issue.dispatchable) == ("Todo", "terminal", False)


def test_move_and_state_of(gh, tracker):
    t = ticket(gh, "Move me")
    ident = f"app-{t['issue']['number']}"
    assert tracker.state_of(ident) == {"name": "Todo", "type": "active"}
    assert tracker.move(t["issue"]["node_id"], "In Review") is True
    assert tracker.move(ident, "in review") is False  # already there
    assert tracker.state_of(f"acme/app#{t['issue']['number']}") == {"name": "In Review", "type": "unknown"}
    assert tracker.move(ident, "Done") is True
    assert tracker.state_of(ident) == {"name": "Done", "type": "terminal"}
    with pytest.raises(ValueError, match="no option 'Shipped'"):
        tracker.move(ident, "Shipped")
    assert tracker.move("app-99999", "Todo") is False
    assert tracker.state_of("app-99999") is None
    off = ticket(gh, "Off board", board=False)
    assert tracker.state_of(f"app-{off['issue']['number']}") is None


def test_terminal_moves_close_the_issue_and_other_moves_reopen_it(gh, tracker, token):
    """Found by the mocked e2e: the merger's green-main path set Done on the board and left the
    issue open."""
    t = ticket(gh, "Close me")
    n = t["issue"]["number"]

    def issue():
        return rest(gh, token, "GET", f"/repos/acme/app/issues/{n}").json()

    assert tracker.move(f"app-{n}", "Done") is True
    assert (issue()["state"], issue()["state_reason"]) == ("closed", "completed")
    assert tracker.move(f"app-{n}", "Done") is False  # board and issue already there
    assert tracker.move(f"app-{n}", "Rework") is True
    assert issue()["state"] == "open"
    assert tracker.move(f"app-{n}", "Canceled") is True
    assert (issue()["state"], issue()["state_reason"]) == ("closed", "not_planned")
    assert tracker.move(f"app-{n}", "Done") is True  # the option changes; already closed
    assert issue()["state_reason"] == "not_planned"
    assert tracker.move(f"app-{n}", "In Review") is True
    assert issue()["state"] == "open"
    # the done state is `tracker.states.done`
    renamed = GitHubTracker(OWNER, NUMBER, ACTIVE, TERMINAL, token=token, done_state="Canceled")
    assert renamed.move(f"app-{n}", "Canceled") is True
    assert issue()["state_reason"] == "completed"


def test_comment_and_workpad_upsert(gh, tracker, token):
    t = ticket(gh, "Workpad ticket")
    ident = f"app-{t['issue']['number']}"
    note = tracker.comment(ident, "A note from the orchestrator")
    first = tracker.upsert_workpad(t["issue"]["node_id"], "## Workpad\n\n- [ ] plan")
    second = tracker.upsert_workpad(ident, "## Workpad\n\n- [x] plan")
    assert first == second != note
    comments = rest(gh, token, "GET", f"/repos/acme/app/issues/{t['issue']['number']}/comments").json()
    assert [c["body"] for c in comments] == ["A note from the orchestrator", "## Workpad\n\n- [x] plan"]
    assert str(comments[1]["id"]) == first
    pad = tracker.fetch_issue_workpad(ident)
    assert "A note from the orchestrator" in pad and "- [x] plan" in pad
    assert tracker.fetch_issue_workpad("app-99999") == ""


def test_comments_since_lists_comment_ids_as_comment_returns_them(gh, tracker, token):
    t = ticket(gh, "Waits for a person")
    ident = f"app-{t['issue']['number']}"
    note = tracker.comment(ident, "Symphony is waiting for a person")
    [listed] = tracker.comments_since(ident, 0)
    assert listed["id"] == note and listed["at"] > 0
    assert tracker.comments_since(ident, listed["at"]) == []


def test_sub_issues_with_blocks_are_idempotent(gh, tracker, token):
    parent = ticket(gh, "Parent", labels=("symphony", "repo:acme/lib", "planning"))
    rest(gh, token, "POST", "/repos/acme/app/labels", json={"name": "docs"})
    items = [
        SubIssue("code", "Write the code", "Do it."),
        SubIssue("docs", "Write the docs", "Explain it.", labels=("Docs",), blocked_by=("code",)),
    ]
    records = Records()
    made = tracker.create_sub_issues(parent["issue"]["node_id"], items, labels=["symphony"],
                                     records=records)
    assert [r["key"] for r in made] == ["code", "docs"]
    again = tracker.create_sub_issues(parent["issue"]["node_id"], items, labels=["symphony"],
                                      records=records)
    assert again == made
    # records lost (TTL): the body marker still finds the children, nothing is duplicated
    lost = tracker.create_sub_issues(f"app-{parent['issue']['number']}", items, labels=["symphony"],
                                     records=Records())
    assert lost == made
    n = parent["issue"]["number"]
    children = rest(gh, token, "GET", f"/repos/acme/app/issues/{n}/sub_issues").json()
    assert [c["title"] for c in children] == ["Write the code", "Write the docs"]
    assert {lab["name"] for lab in children[1]["labels"]} == {"symphony", "repo:acme/lib", "docs"}
    deps = rest(gh, token, "GET",
                f"/repos/acme/app/issues/{children[1]['number']}/dependencies/blocked_by").json()
    assert [d["number"] for d in deps] == [children[0]["number"]]

    code, docs = tracker.fetch_issues_by_ids([c["node_id"] for c in children])
    assert code.state == "Todo" and code.dispatchable and code.target_repo == "acme/lib"
    assert not docs.dispatchable and docs.blocked_by[0]["identifier"] == code.identifier
    assert made[0]["identifier"] == code.identifier and made[0]["url"] == code.url

    with pytest.raises(TrackerInputError, match="unknown labels in acme/app: nope"):
        tracker.create_sub_issues(parent["issue"]["node_id"],
                                  [SubIssue("x", "X", "x", labels=("nope",))],
                                  labels=[], records=Records())


def test_main_red_ticket_is_created_on_the_board(gh, tracker, token):
    out = tracker.create_issue("main is red", "CI failed on main.", labels=["symphony", "main-red"],
                               state="Todo", repo="acme/lib")
    assert out["identifier"].startswith("lib-") and "/acme/lib/issues/" in out["url"]
    assert tracker.state_of(out["identifier"]) == {"name": "Todo", "type": "active"}
    labels = {lab["name"] for lab in rest(gh, token, "GET", "/repos/acme/lib/labels").json()}
    assert {"symphony", "main-red"} <= labels
    # no repo: the board's default repository
    assert tracker.create_issue("again", "x", labels=[])["identifier"].startswith("app-")
    no_default = GitHubTracker(OWNER, NUMBER, ACTIVE, TERMINAL, token=tracker._token)
    with pytest.raises(TrackerInputError, match="needs a repository"):
        no_default.create_issue("x", "y", labels=[])


def test_text_evidence_is_a_collapsible_comment(gh, tracker, token):
    t = ticket(gh, "Evidence ticket")
    ident = f"app-{t['issue']['number']}"
    log = b"ok 1 - adds\n```\nnot a fence end\n"
    out = tracker.attach_evidence(ident, title="Test run", kind="log", filename="pytest.log",
                                  content_type="text/plain", data=log)
    assert out["title"] == "Test run" and out["kind"] == "log"
    assert out["url"].startswith(t["issue"]["html_url"] + "#issuecomment-")
    [comment] = rest(gh, token, "GET", f"/repos/acme/app/issues/{t['issue']['number']}/comments").json()
    assert "<details>" in comment["body"] and "````text\nok 1 - adds\n```\nnot a fence end\n````" in comment["body"]
    assert tracker.attachments(ident) == [{"title": "Test run", "url": out["url"], "kind": "log"}]
    tracker.attach_evidence(ident, title="Report", kind="report", filename="r.json",
                            content_type="application/json", data=b'{"ok": true}')
    assert [a["kind"] for a in tracker.attachments(ident)] == ["log", "report"]


def test_binary_and_oversized_evidence_are_refused(gh, tracker):
    t = ticket(gh, "Binary ticket")
    ident = f"app-{t['issue']['number']}"
    with pytest.raises(TrackerInputError, match="GitHub has no API to upload files"):
        tracker.attach_evidence(ident, title="Shot", kind="screenshot", filename="s.png",
                                content_type="image/png", data=b"\x89PNG\r\n")
    with pytest.raises(TrackerInputError, match="not UTF-8"):
        tracker.attach_evidence(ident, title="Log", kind="log", filename="a.log",
                                content_type="text/plain", data=b"\xff\xfe")
    with pytest.raises(TrackerInputError, match="one GitHub comment"):
        tracker.attach_evidence(ident, title="Log", kind="log", filename="a.log",
                                content_type="text/plain", data=b"x" * 70000)
    assert tracker.attachments(ident) == []


def test_github_graphql_tool_runs_with_the_tracker_credential(gh, tracker):
    assert [s.name for s in tracker.agent_tool_specs()] == ["github_graphql"]
    assert tracker.kind == "github"
    out = tracker.execute_agent_tool("github_graphql", {
        "query": "query($o: String!, $n: Int!) { organization(login: $o) { projectV2(number: $n) "
                 "{ title } } }",
        "variables": {"o": OWNER, "n": NUMBER},
    })
    assert out == {"organization": {"projectV2": {"title": "Symphony"}}}
    with pytest.raises(ValueError, match="unknown tool"):
        tracker.execute_agent_tool("linear_graphql", {"query": "{}"})
    assert "GITHUB_APP_PRIVATE_KEY" in tracker.secret_environment_names()


def test_identifier_in_pull_request_text(tracker):
    assert tracker.identifier_in("symphony/symphony-k8s-42") == "symphony-k8s-42"
    assert tracker.identifier_in("refs: Symphony/App-7 and acme/lib#3") == "app-7"
    assert tracker.identifier_in("Closes acme/lib#3") == "lib-3"
    assert tracker.identifier_in("see https://github.com/acme/App/issues/9") == "app-9"
    assert tracker.identifier_in("bump utf-8 handling") is None


def test_closing_keywords_name_a_ticket_of_the_pull_requests_repository(tracker):
    for text in ("Closes #5", "fixes #5", "Fixed: #5", "Resolves #5 and more", "close #5"):
        assert tracker.identifier_in(text, repo="acme/app") == "app-5", text
    assert tracker.identifier_in("Closes #5") is None, "a bare #n needs the repository"
    assert tracker.identifier_in("See #5", repo="acme/app") is None, "only closing keywords"
    assert tracker.identifier_in("Closes #5, see acme/lib#3", repo="acme/app") == "lib-3"
    assert tracker.identifier_in("Fixes acme/lib#3", repo="acme/app") == "lib-3"


def test_the_merger_reads_a_bare_closing_ref_in_its_repository(tracker):
    from symphony.merger.context import Merger

    merger = Merger(store=None, gh=None, git=None, agents=None, team=None, repo="acme/lib",
                    states={}, tracker=tracker)
    pr = {"head_ref": "feature/shout", "title": "Add shout()", "body": "Fixes #12"}
    assert merger.ticket_of(pr) == "lib-12"
    assert merger.ticket_of({**pr, "title": "app-3: Add shout()"}) == "app-3"


def test_a_pull_request_named_after_the_ticket_is_linked_to_it(tracker):
    """Found by the mocked certification: the agent named its branch `app-5-farewell` and its PR
    "app-5: Add farewell()", as the prompt asks; the merger found no ticket and never moved the
    board item to Merging. A branch or title that starts with the identifier links it."""
    assert tracker.identifier_in("app-5-farewell") == "app-5"
    assert tracker.identifier_in("app-5: Add farewell()") == "app-5"
    assert tracker.identifier_in("symphony-k8s-42/fix") == "symphony-k8s-42"
    assert tracker.identifier_in("Fix app-5 later") is None, "only a leading identifier counts"
    assert tracker.identifier_in("app-5: see acme/lib#3") == "lib-3", "a structured ref still wins"


def test_unknown_project_is_an_outage_with_a_clear_message(token):
    from symphony.trackers.base import TrackerUnavailable

    lost = GitHubTracker(OWNER, 404, ACTIVE, TERMINAL, token=token)
    with pytest.raises(TrackerUnavailable, match="GitHub project acme#404 was not found"):
        lost.fetch_issues_by_states(["Todo"])


def test_ticket_webhooks_wake_the_daemon(gh, tracker, token):
    m = gh.mock
    seen = len(m.hooks.deliveries)
    parent = ticket(gh, "Hooked")
    n = parent["issue"]["number"]
    rest(gh, token, "POST", f"/repos/acme/app/issues/{n}/comments", json={"body": "hi"})
    tracker.move(parent["issue"]["node_id"], "In Progress")
    tracker.create_sub_issues(parent["issue"]["node_id"],
                              [SubIssue("a", "A", "a"), SubIssue("b", "B", "b", blocked_by=("a",))],
                              labels=[], records=Records())
    deliveries = list(m.hooks.deliveries)[seen:]
    events = {d["event"] for d in deliveries}
    assert {"issues", "issue_comment", "projects_v2_item", "sub_issues", "issue_dependencies"} <= events
    for d in deliveries:
        wakes = github_wakes(d["event"], d["payload"])
        assert [target for target, _ in wakes] == [wake.DAEMON], d["event"]
    reasons = {github_wakes(d["event"], d["payload"])[0][1] for d in deliveries}
    assert f"github:issues:opened:acme/app#{n}" in reasons
    assert f"github:issue_comment:created:acme/app#{n}" in reasons
    assert f"github:projects_v2_item:edited:{parent['issue']['node_id']}" in reasons
    assert f"github:sub_issues:sub_issue_added:acme/app#{n}" in reasons


GITHUB_WORKFLOW = """---
tracker:
  kind: github
  provider: {provider}
  active_states: [Todo, In Progress, Rework]
  terminal_states: [Done, Canceled]
---
body
"""


def test_config_accepts_a_github_board():
    cfg = parse_workflow(GITHUB_WORKFLOW.format(provider="{owner: acme, project_number: 7}"))
    assert cfg.tracker_kind == "github" and cfg.provider == {"owner": "acme", "project_number": 7}
    cfg = parse_workflow(GITHUB_WORKFLOW.format(
        provider="{owner: acme, project_number: 7, status_field: Stage, team_key: null}"))
    assert cfg.provider["status_field"] == "Stage"


@pytest.mark.parametrize("provider,message", [
    ("{}", "tracker.provider.owner must be the GitHub organization"),
    ("{owner: 'acme/app', project_number: 7}", "tracker.provider.owner"),
    ("{owner: acme}", "project_number must be the positive number"),
    ("{owner: acme, project_number: 0}", "project_number must be the positive number"),
    ("{owner: acme, project_number: '7'}", "project_number must be the positive number"),
    ("{owner: acme, project_number: true}", "project_number must be the positive number"),
    ("{owner: acme, project_number: 7, status_field: ''}", "status_field must name"),
    ("{owner: acme, project_number: 7, project_slug: x}", "unknown keys for github: project_slug"),
])
def test_config_rejects_bad_github_providers(provider, message):
    with pytest.raises(ConfigError, match=message):
        parse_workflow(GITHUB_WORKFLOW.format(provider=provider))
