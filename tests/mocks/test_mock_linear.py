"""Linear mock: Symphony's own Linear clients (tracker, host tools, merger writer) end to end
against the schema-faithful mock, plus the mock's documented edges (errors, auth, uploads,
webhooks, rate limits, pagination, complexity)."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import graphql
import httpx
import pytest
from scripts.mocks.linear import graph
from scripts.mocks.linear import server as linear_mock
from scripts.mocks.linear.store import DEFAULT_API_KEY
from tests.api_support import LINEAR_SECRET, client_for, make_services
from tests.conftest import FakeKV

from symphony import wake
from symphony.hosttools import HostToolBroker
from symphony.sessions import SessionStore
from symphony.trackers.base import TrackerInputError, TrackerUnavailable
from symphony.trackers.linear import LinearTracker
from symphony.workspace import LocalSandbox

ROOT = Path(__file__).resolve().parents[2]
KEY = DEFAULT_API_KEY
ACTIVE = ["Todo", "In Progress", "Rework"]
TERMINAL = ["Done", "Canceled", "Duplicate"]


class Receiver:
    """A webhook consumer that records raw deliveries."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[dict, bytes]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.deliveries.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/webhooks/linear"


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.server.shutdown()
    r.server.server_close()


@pytest.fixture
def mock(tmp_path, receiver, monkeypatch):
    srv = linear_mock.serve(0, tmp_path / "state", webhook_url=receiver.url,
                            webhook_secret=LINEAR_SECRET)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("LINEAR_API_URL", f"{base}/graphql")
    srv.base = base
    yield srv
    srv.shutdown()
    srv.server_close()


def gql(base: str, query: str, variables: dict | None = None, *, key: str | None = KEY,
        headers: dict | None = None) -> httpx.Response:
    sent = {"Content-Type": "application/json", **(headers or {})}
    if key is not None:
        sent["Authorization"] = key
    return httpx.post(f"{base}/graphql", json={"query": query, "variables": variables or {}},
                      headers=sent)


def control(base: str, method: str, route: str, body: dict | None = None) -> dict:
    resp = httpx.request(method, f"{base}/_mock/{route}", json=body or {})
    assert resp.status_code < 300, resp.text
    return resp.json()


def tracker(**kw) -> LinearTracker:
    return LinearTracker(api_key=KEY, project_slug=kw.pop("project_slug", ""),
                         team_key=kw.pop("team_key", "ENG"), active_states=ACTIVE,
                         terminal_states=TERMINAL, **kw)


def state_id(base: str, name: str) -> str:
    data = gql(base, 'query($n: String!) { workflowStates(filter: {name: {eq: $n}, '
               'team: {key: {eq: "ENG"}}}) { nodes { id } } }', {"n": name}).json()["data"]
    return data["workflowStates"]["nodes"][0]["id"]


# -- the tracker ----------------------------------------------------------------------------
def test_tracker_fetches_candidates_moves_state_and_keeps_the_workpad(mock):
    base = mock.base
    ready = control(base, "POST", "issues", {"title": "Ship login", "state": "Todo",
                                             "labels": ["symphony", "repo:acme/app"],
                                             "description": "Do it", "priority": 2})
    blocker = control(base, "POST", "issues", {"title": "Schema first", "state": "In Progress",
                                               "labels": ["symphony", "repo:acme/app"]})
    blocked = control(base, "POST", "issues", {"title": "Needs schema", "state": "Todo",
                                               "labels": ["symphony", "repo:acme/app"]})
    control(base, "POST", "issues", {"title": "Finished", "state": "Done", "labels": ["symphony"]})
    t = tracker()
    t.execute_agent_tool("linear_graphql", {
        "query": "mutation($i: IssueRelationCreateInput!) { issueRelationCreate(input: $i) "
                 "{ success } }",
        "variables": {"i": {"issueId": blocker["id"], "relatedIssueId": blocked["id"],
                            "type": "blocks"}}})

    issues = {i.identifier: i for i in t.fetch_issues_by_states(["Todo", "In Progress"])}
    assert set(issues) == {"ENG-1", "ENG-2", "ENG-3"}
    first = issues["ENG-1"]
    assert (first.title, first.description, first.state, first.priority) == (
        "Ship login", "Do it", "Todo", 2)
    assert set(first.labels) == {"symphony", "repo:acme/app"}
    assert first.target_repo == "acme/app" and first.dispatchable
    assert first.branch_name == "symphony/eng-1-ship-login"
    assert first.url == f"{base}/symphony-mock/issue/ENG-1/ship-login"
    assert issues["ENG-3"].blocked_by == ({"id": blocker["id"], "identifier": "ENG-2",
                                           "state": "In Progress"},)
    assert not issues["ENG-3"].dispatchable

    by_id = t.fetch_issues_by_ids([ready["id"], blocked["id"]])
    assert sorted(i.identifier for i in by_id) == ["ENG-1", "ENG-3"]

    # the agent's first moves: In Progress, then the Workpad, edited in place
    t.execute_agent_tool("linear_graphql", {
        "query": "mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: {stateId: $s})"
                 " { success issue { state { name } startedAt } } }",
        "variables": {"id": "ENG-1", "s": state_id(base, "In Progress")}})
    made = t.execute_agent_tool("linear_graphql", {
        "query": "mutation($i: CommentCreateInput!) { commentCreate(input: $i) "
                 "{ success comment { id body user { isMe } } } }",
        "variables": {"i": {"issueId": ready["id"], "body": "## Workpad\n- [ ] plan"}}})
    assert made["commentCreate"]["comment"]["user"]["isMe"] is True
    t.execute_agent_tool("linear_graphql", {
        "query": "mutation($id: String!, $b: String!) { commentUpdate(id: $id, input: {body: $b})"
                 " { success comment { editedAt } } }",
        "variables": {"id": made["commentCreate"]["comment"]["id"],
                      "b": "## Workpad\n- [x] plan"}})
    view = control(base, "GET", "issues/ENG-1")
    assert view["state"] == "In Progress"
    assert [c["body"] for c in view["comments"]] == ["## Workpad\n- [x] plan"]
    assert view["comments"][0]["editedAt"] is not None
    assert t.fetch_issue_workpad("ENG-1") == "## Workpad\n- [x] plan"


def test_tracker_paginates_and_scopes_by_project(mock):
    store = mock.mock.store
    with store.lock:
        team = store.rows("Team")[0]
        todo = store.state_named(team, "Todo")
        bot = store.get("User", store.data["botId"])
        for n in range(53):
            store.create_issue(bot, {"teamId": team["id"], "title": f"Issue {n}",
                                     "stateId": todo["id"],
                                     "projectId": store.data["projectId"] if n < 3 else None})
        store.drain_events()
    assert len(tracker().fetch_issues_by_states(["Todo"])) == 53
    slug = store.table("Project")[store.data["projectId"]]["slugId"]
    scoped = tracker(project_slug=slug, team_key=None).fetch_issues_by_states(["Todo"])
    assert sorted(i.title for i in scoped) == ["Issue 0", "Issue 1", "Issue 2"]


def test_merger_writes_move_comment_create_and_read_attachments(mock):
    base = mock.base
    control(base, "POST", "issues", {"title": "Review me", "state": "In Review"})
    writer = tracker()
    assert writer.move("ENG-1", "Rework") is True
    assert writer.move("ENG-1", "Rework") is False
    assert writer.state_of("ENG-1") == {"name": "Rework", "type": "active", "canceled": False}
    assert writer.comment("ENG-1", "symphony-merger: blocked")
    created = writer.create_issue("Main is red", "CI broke", state="Todo",
                                  labels=["symphony", "repo:acme/new"], repo="acme/new")
    assert created["identifier"] == "ENG-2" and created["url"].endswith("/ENG-2/main-is-red")
    view = control(base, "GET", "issues/ENG-2")
    assert view["state"] == "Todo" and view["labels"] == ["symphony", "repo:acme/new"]
    assert [c["body"] for c in control(base, "GET", "issues/ENG-1")["comments"]] == [
        "symphony-merger: blocked"]
    assert writer.attachments("ENG-1") == []
    assert writer.identifier_in("eng-1-review-me") == "ENG-1"


def test_the_workpad_is_one_comment_edited_in_place(mock):
    control(mock.base, "POST", "issues", {"title": "Pad", "state": "Todo"})
    writer = tracker()
    first = writer.upsert_workpad("ENG-1", "## Workpad\n\n- [ ] plan")
    assert writer.upsert_workpad("ENG-1", "## Workpad\n\n- [x] plan") == first
    assert [c["body"] for c in control(mock.base, "GET", "issues/ENG-1")["comments"]] == [
        "## Workpad\n\n- [x] plan"]


def test_comments_since_lists_the_comments_after_a_time_by_the_id_comment_returned(mock):
    control(mock.base, "POST", "issues", {"title": "Waits", "state": "In Progress"})
    writer = tracker()
    before = writer.comment("ENG-1", "Symphony is waiting for a person")
    [(_, mark)] = [(c["id"], c["at"]) for c in writer.comments_since("ENG-1", 0)]
    time.sleep(0.01)  # a later millisecond than the mark
    later = writer.comment("ENG-1", "Still waiting for an answer")
    since = writer.comments_since("ENG-1", mark)
    assert [c["id"] for c in since] == [later] and before != later
    assert since[0]["at"] >= mark


# -- the host tools -------------------------------------------------------------------------
@pytest.fixture
def broker(mock, redis_client, tmp_path):
    ticket = control(mock.base, "POST", "issues", {
        "title": "Big plan", "state": "In Progress",
        "labels": ["symphony", "repo:acme/app", "bug"]})
    store = SessionStore(redis_client, "test")
    store.open("i1:1", kind="implement", identifier="ENG-1")
    results: list = []
    b = HostToolBroker(
        store=store, sid="i1:1", kv=FakeKV(), tracker=tracker(), issue_id=ticket["id"], seq=1,
        identifier="ENG-1", respond=lambda cid, text, is_error=False: results.append(text),
        owns=lambda: True, input_timeout_s=1, sandbox=LocalSandbox(str(tmp_path)),
        workspace_root=str(tmp_path), required_labels=("symphony",))
    b.ticket, b.session_store = ticket, store
    return b


def test_create_issues_makes_children_with_blocks_relations(mock, broker):
    tree = {"issues": [
        {"key": "api", "title": "API endpoint", "description": "Add /v1/things",
         "blocked_by": ["schema"]},
        {"key": "schema", "title": "DB schema", "description": "Migration", "labels": ["feature"]},
    ]}
    result = broker.create_issues(tree)
    assert [r["key"] for r in result] == ["api", "schema"]
    by_key = {r["key"]: r for r in result}
    assert by_key["schema"]["identifier"] == "ENG-2" and by_key["api"]["identifier"] == "ENG-3"
    parent = control(mock.base, "GET", "issues/ENG-1")
    assert parent["children"] == ["ENG-2", "ENG-3"]
    schema_issue = control(mock.base, "GET", "issues/ENG-2")
    assert schema_issue["parent"] == "ENG-1"
    assert set(schema_issue["labels"]) == {"repo:acme/app", "symphony", "feature"}  # not `bug`
    assert schema_issue["relations"] == [{"type": "blocks", "issue": "ENG-2",
                                          "relatedIssue": "ENG-3"}]
    # replay: same keys, nothing duplicated
    assert broker.create_issues(tree) == result
    assert len(control(mock.base, "GET", "issues")["issues"]) == 3
    # the tracker sees ENG-3 blocked by the open ENG-2
    (api,) = tracker().fetch_issues_by_ids([by_key["api"]["identifier"]])
    assert [b["identifier"] for b in api.blocked_by] == ["ENG-2"] and not api.dispatchable


def test_attach_evidence_uploads_and_attaches(mock, broker, tmp_path):
    payload = bytes(range(256)) * 40
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "shot.png").write_bytes(payload)
    url = broker.attach_evidence({"path": "out/shot.png", "title": "Login page",
                                  "kind": "screenshot"})["url"]
    assert url.startswith(f"{mock.base}/uploads/")
    assert httpx.get(url).status_code == 401  # file storage needs the API credential
    got = httpx.get(url, headers={"Authorization": KEY})
    assert got.status_code == 200 and got.content == payload
    assert got.headers["content-type"] == "image/png"
    assert 'filename="shot.png"' in got.headers["content-disposition"]
    view = control(mock.base, "GET", "issues/ENG-1")
    assert view["attachments"] == [{"id": view["attachments"][0]["id"], "title": "Login page",
                                    "subtitle": "screenshot", "url": url}]
    assert tracker().attachments("ENG-1") == [
        {"title": "Login page", "url": url, "kind": "screenshot"}]
    assert broker.session_store.meta("i1:1")["evidence"] == [
        {"title": "Login page", "url": url, "kind": "screenshot"}]


def test_the_agent_reads_back_its_tickets_files_only(mock, broker, tmp_path):
    """An earlier run's draft comes back through the host, with the API key the agent does
    not have; another ticket's file and a link attachment are refused."""
    draft = b"# Draft\n\nv1 of the report\n"
    (tmp_path / "draft.md").write_bytes(draft)
    url = broker.attach_evidence({"path": "draft.md", "title": "Report draft", "kind": "report"})["url"]
    assert httpx.get(url).status_code == 401, "what the agent would get on its own"
    other = control(mock.base, "POST", "issues", {"title": "Other", "state": "Todo",
                                                  "labels": ["symphony"]})
    foreign = tracker().attach_evidence(other["id"], title="Foreign", kind="log",
                                        filename="other.log", content_type="text/plain",
                                        data=b"not yours\n")
    gql(mock.base, "mutation($i: AttachmentCreateInput!) { attachmentCreate(input: $i) "
        "{ success } }", {"i": {"issueId": broker.ticket["id"], "title": "PR",
                               "url": "https://github.com/acme/app/pull/1"}})

    listed = {a["title"]: a for a in broker.ticket_attachments()}
    assert set(listed) == {"Report draft", "PR"}
    assert listed["Report draft"]["host"] == "127.0.0.1" and listed["Report draft"]["kind"] == "report"
    assert listed["PR"]["host"] == "github.com" and listed["PR"]["created_at"]

    assert broker.fetch_attachment({"id": listed["Report draft"]["id"], "path": "prev/draft.md"}) == {
        "path": "prev/draft.md", "bytes": len(draft)}
    assert (tmp_path / "prev" / "draft.md").read_bytes() == draft
    foreign_id = next(a["id"] for a in tracker().ticket_attachments(other["id"])
                      if a["url"] == foreign["url"])
    with pytest.raises(TrackerInputError, match="no attachment"):
        broker.fetch_attachment({"id": foreign_id, "path": "other.log"})
    with pytest.raises(TrackerInputError, match="links to github.com"):
        broker.fetch_attachment({"id": listed["PR"]["id"], "path": "pr.html"})
    assert not (tmp_path / "other.log").exists() and not (tmp_path / "pr.html").exists()


def test_upload_url_enforces_the_signed_request(mock):
    data = gql(mock.base, 'mutation { fileUpload(contentType: "text/plain", filename: "a.log", '
               "size: 3) { success uploadFile { uploadUrl assetUrl headers { key value } "
               "size contentType filename } } }").json()["data"]["fileUpload"]["uploadFile"]
    signed = {h["key"]: h["value"] for h in data["headers"]}
    put = data["uploadUrl"]
    assert httpx.put(put, content=b"abc", headers={"Content-Type": "text/plain"}).status_code == 403
    assert httpx.put(put, content=b"abc", headers={**signed, "Content-Type": "image/png"}
                     ).status_code == 403
    assert httpx.put(put, content=b"abcd", headers={**signed, "Content-Type": "text/plain"}
                     ).status_code == 400
    assert httpx.put(put.replace("Signature=", "Signature=0"), content=b"abc",
                     headers={**signed, "Content-Type": "text/plain"}).status_code == 403
    assert httpx.get(data["assetUrl"], headers={"Authorization": KEY}).status_code == 404
    assert httpx.put(put, content=b"abc", headers={**signed, "Content-Type": "text/plain"}
                     ).status_code == 200
    assert httpx.get(data["assetUrl"], headers={"Authorization": KEY}).content == b"abc"


# -- errors, auth, limits -------------------------------------------------------------------
def test_invalid_operations_get_linear_shaped_errors(mock):
    base = mock.base
    cases = [
        ("{ viewer { nope } }", None, "Cannot query field 'nope' on type 'User'."),
        ('{ issues(bogus: 1) { nodes { id } } }', None, "Unknown argument 'bogus'"),
        ("query($x: Nope) { viewer { id } }", None, "Unknown type 'Nope'."),
        ("query($id: String!) { issue(id: $id) { id } }", {"id": 7}, "has invalid value"),
        ("query($id: String!) { issue(id: $id) { id } }", {}, "to be provided"),
        ("{ viewer { id ", None, "Syntax Error"),
    ]
    for query, variables, message in cases:
        resp = gql(base, query, variables)
        assert resp.status_code == 400, query
        errors = resp.json()["errors"]
        err = next((e for e in errors if message in e["message"]), None)
        assert err is not None, (query, errors)
        ext = err["extensions"]
        assert ext["code"] in ("GRAPHQL_VALIDATION_FAILED", "GRAPHQL_PARSE_FAILED",
                               "BAD_USER_INPUT")
        assert ext["userError"] is True and ext["statusCode"] == 400 and ext["type"]

    missing = gql(base, '{ issue(id: "ENG-404") { id } }')
    assert missing.status_code == 400 and missing.json()["data"] is None
    assert missing.json()["errors"][0]["message"] == "Entity not found: Issue"
    assert missing.json()["errors"][0]["extensions"]["code"] == "INVALID_INPUT"

    with pytest.raises(TrackerUnavailable, match="rejected 400"):
        tracker().execute_agent_tool("linear_graphql", {"query": "{ viewer { nope } }"})


def test_auth_is_the_raw_personal_api_key(mock):
    base = mock.base
    for key in (None, "lin_api_wrong", f"Bearer {KEY}"):
        resp = gql(base, "{ viewer { id } }", key=key)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["message"] == "Authentication required, not authenticated"
        assert resp.json()["errors"][0]["extensions"]["code"] == "AUTHENTICATION_ERROR"
    ok = gql(base, "{ viewer { email } }")
    assert ok.status_code == 200 and ok.json() == {"data": {"viewer": {"email": "symphony@example.com"}}}
    csrf = httpx.post(f"{base}/graphql", content=b'{"query":"{ viewer { id } }"}',
                      headers={"Authorization": KEY, "Content-Type": "text/plain"})
    assert csrf.status_code == 400 and "Cross-Site Request Forgery" in csrf.json()["errors"][0]["message"]
    get = httpx.get(f"{base}/graphql", params={"query": "{ viewer { id } }"},
                    headers={"Authorization": KEY, "Content-Type": "application/json"})
    assert get.status_code == 200 and get.json()["data"]["viewer"]["id"]
    mutation = httpx.get(f"{base}/graphql", params={"query": 'mutation { issueArchive(id: "x") '
                                                             "{ success } }"},
                         headers={"Authorization": KEY, "Content-Type": "application/json"})
    assert mutation.status_code == 405


def test_rate_limit_headers_and_ratelimited_answer(mock):
    resp = gql(mock.base, '{ user(id: "me") { name } }')
    assert resp.headers["X-Complexity"] == "2"  # the docs' own example
    assert int(resp.headers["X-RateLimit-Requests-Remaining"]) == 2499
    assert resp.headers["X-RateLimit-Requests-Limit"] == "2500"
    assert int(resp.headers["X-RateLimit-Requests-Reset"]) > time.time() * 1000
    created = gql(mock.base, '{ user(id: "me") { createdIssues(first: 10) { nodes { id title '
                  "createdAt } } } }")
    assert created.headers["X-Complexity"] == "14"
    default = gql(mock.base, '{ user(id: "me") { createdIssues { nodes { id title createdAt } } } }')
    assert default.headers["X-Complexity"] == "66"

    control(mock.base, "POST", "ratelimit", {"remaining": 0})
    limited = gql(mock.base, "{ viewer { id } }")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    assert limited.json()["errors"][0]["extensions"]["code"] == "RATELIMITED"
    with pytest.raises(TrackerUnavailable, match="rate limited") as exc:
        tracker().fetch_issues_by_states(["Todo"])
    assert exc.value.retry_after_s > 0


def test_unmodelled_parts_fail_loudly(mock):
    control(mock.base, "POST", "issues", {"title": "X"})
    resp = gql(mock.base, "{ issues { nodes { id cycle { id } } } }")
    assert resp.status_code == 501
    assert resp.json()["errors"][0]["extensions"]["code"] == "MOCK_NOT_IMPLEMENTED"
    team = gql(mock.base, "{ teams { nodes { id } } }").json()["data"]["teams"]["nodes"][0]["id"]
    resp = gql(mock.base, "mutation($i: IssueCreateInput!) { issueCreate(input: $i) { success } }",
               {"i": {"teamId": team, "title": "Y", "cycleId": "c"}})
    assert resp.status_code == 501 and "IssueCreateInput.cycleId" in resp.text
    assert len(control(mock.base, "GET", "issues")["issues"]) == 1  # nothing half-created
    resp = gql(mock.base, "{ issues(filter: {hasBlockedByRelations: {eq: true}}) { nodes { id } } }")
    assert resp.status_code == 501


def test_create_takes_only_a_version_4_client_id(mock):
    """Linear's create inputs take "The identifier in UUID v4 format" and reject any other."""
    team = gql(mock.base, "{ teams { nodes { id } } }").json()["data"]["teams"]["nodes"][0]["id"]
    create = "mutation($i: IssueCreateInput!) { issueCreate(input: $i) { success issue { id } } }"
    v5 = str(uuid.uuid5(uuid.NAMESPACE_URL, "symphony:x"))
    resp = gql(mock.base, create, {"i": {"teamId": team, "title": "v5", "id": v5}})
    err = resp.json()["errors"][0]
    assert err["message"] == "Argument Validation Error"
    assert err["extensions"]["code"] == "INVALID_INPUT"
    assert err["extensions"]["userPresentableMessage"] == "id must be a UUID"
    v4 = str(uuid.uuid4())
    resp = gql(mock.base, create, {"i": {"teamId": team, "title": "v4", "id": v4}})
    assert resp.json()["data"]["issueCreate"] == {"success": True, "issue": {"id": v4}}


def test_filters_pagination_and_cursors(mock):
    base = mock.base
    for title, labels in (("a", ["bug"]), ("b", ["feature"]), ("c", ["bug", "feature"])):
        control(base, "POST", "issues", {"title": title, "labels": labels})
    page = gql(base, "{ issues(first: 2) { nodes { title } pageInfo { hasNextPage "
               "hasPreviousPage endCursor } } }").json()["data"]["issues"]
    assert [n["title"] for n in page["nodes"]] == ["c", "b"]
    assert page["pageInfo"]["hasNextPage"] and not page["pageInfo"]["hasPreviousPage"]
    rest = gql(base, "query($a: String) { issues(first: 2, after: $a) { nodes { title } pageInfo "
               "{ hasNextPage hasPreviousPage } } }", {"a": page["pageInfo"]["endCursor"]}).json()
    assert [n["title"] for n in rest["data"]["issues"]["nodes"]] == ["a"]
    assert rest["data"]["issues"]["pageInfo"] == {"hasNextPage": False, "hasPreviousPage": True}
    some = gql(base, '{ issues(filter: {labels: {some: {name: {eq: "bug"}}}}) { nodes { title } } }')
    assert [n["title"] for n in some.json()["data"]["issues"]["nodes"]] == ["c", "a"]
    every = gql(base, '{ issues(filter: {and: [{labels: {name: {eq: "feature"}}}, '
                '{title: {in: ["b", "c"]}}], or: [{title: {containsIgnoreCase: "B"}}, '
                '{priority: {lte: 0}}]}) { nodes { title } } }')
    assert [n["title"] for n in every.json()["data"]["issues"]["nodes"]] == ["c", "b"]
    labels = gql(base, '{ issueLabels(filter: {team: {null: true}, name: {startsWith: "s"}}) '
                 "{ nodes { name team { id } } } }").json()["data"]["issueLabels"]["nodes"]
    assert labels == [{"name": "symphony", "team": None}]
    recent = gql(base, '{ issues(filter: {createdAt: {gt: "-PT1H"}}) { nodes { title } } }')
    assert len(recent.json()["data"]["issues"]["nodes"]) == 3


# -- webhooks -------------------------------------------------------------------------------
def test_webhooks_are_signed_and_verified_by_symphony(mock, receiver, redis_client):
    base = mock.base
    issue = control(base, "POST", "issues", {"title": "Hooked", "state": "Todo"})
    control(base, "POST", "issues/ENG-1/state", {"state": "In Progress"})
    control(base, "POST", "issues/ENG-1/comments", {"body": "please also do X"})
    assert control(base, "POST", "webhooks/flush")["flushed"] is True
    assert [json.loads(b)["action"] + ":" + json.loads(b)["type"] for _, b in receiver.deliveries
            ] == ["create:Issue", "update:Issue", "create:Comment"]

    headers, body = receiver.deliveries[1]
    assert headers["linear-signature"] == hmac.new(LINEAR_SECRET.encode(), body,
                                                   hashlib.sha256).hexdigest()
    assert headers["linear-event"] == "Issue" and headers["user-agent"] == "Linear-Webhook"
    assert headers["linear-delivery"] and headers["linear-timestamp"].isdigit()
    payload = json.loads(body)
    assert payload["data"]["identifier"] == "ENG-1" and payload["data"]["id"] == issue["id"]
    assert payload["data"]["state"]["name"] == "In Progress"
    assert payload["updatedFrom"]["stateId"] != payload["data"]["stateId"]
    assert payload["actor"]["email"] == "ada@example.com"
    assert payload["url"] == issue["url"]
    assert set(payload) >= {"action", "type", "data", "updatedFrom", "url", "createdAt",
                            "webhookTimestamp", "webhookId", "organizationId", "actor"}

    # Symphony's own endpoint accepts every delivery as sent
    client = client_for(make_services(redis_client))
    for sent_headers, sent_body in receiver.deliveries:
        resp = client.post("/v1/webhooks/linear", content=sent_body, headers={
            "Linear-Signature": sent_headers["linear-signature"],
            "Content-Type": sent_headers["content-type"]})
        assert resp.status_code == 202, resp.text
    assert wake.wait(redis_client, "test", wake.DAEMON, 0.01)
    tampered = receiver.deliveries[0][1].replace(b"Hooked", b"Hacked")
    resp = client.post("/v1/webhooks/linear", content=tampered, headers={
        "Linear-Signature": receiver.deliveries[0][0]["linear-signature"]})
    assert resp.status_code == 401


def test_agent_writes_through_graphql_emit_webhooks(mock, receiver):
    control(mock.base, "POST", "issues", {"title": "Agent"})
    gql(mock.base, 'mutation { commentCreate(input: {issueId: "ENG-1", body: "hi"}) { success } }')
    gql(mock.base, 'mutation { documentCreate(input: {issueId: "ENG-1", title: "Report", '
        'content: "# Findings"}) { success document { url slugId } } }')
    control(mock.base, "POST", "webhooks/flush")
    events = [json.loads(b) for _, b in receiver.deliveries]
    assert [(e["type"], e["action"], e["actor"]["email"]) for e in events] == [
        ("Issue", "create", "ada@example.com"), ("Comment", "create", "symphony@example.com"),
        ("Document", "create", "symphony@example.com")]
    assert events[1]["data"]["issue"]["identifier"] == "ENG-1"


# -- process ------------------------------------------------------------------------------
def test_state_survives_a_restart(tmp_path):
    srv = linear_mock.serve(0, tmp_path)
    store = srv.mock.store
    with store.lock:
        team = store.rows("Team")[0]
        store.create_issue(store.get("User", store.data["humanId"]),
                           {"teamId": team["id"], "title": "Persisted"})
        store.save()
    srv.server_close()
    again = linear_mock.serve(0, tmp_path)
    assert [i["title"] for i in again.mock.store.rows("Issue")] == ["Persisted"]
    again.server_close()


def test_vendored_linear_specs_match_their_sources_rows():
    specs = ROOT / "scripts" / "mocks" / "specs"
    rows = {
        line.split("|")[1].strip().strip("`"): line.split("|")[4].strip().strip("`")
        for line in (specs / "SOURCES.md").read_text().splitlines()
        if line.startswith("| `linear")}
    assert "linear.graphql" in rows and len(rows) == 8
    for name, digest in rows.items():
        assert hashlib.sha256((specs / name).read_bytes()).hexdigest() == digest, name


def test_cli_starts_in_under_two_seconds(tmp_path):
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "scripts.mocks.linear", "--port", "0",
                             "--state", str(tmp_path), "--team", "ENG:Engineering"], cwd=ROOT,
                            stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert time.monotonic() - started < 2.0, line
        url = line.strip().rsplit(" ", 1)[-1]
        teams = httpx.post(url, json={"query": "{ teams { nodes { key } } }"},
                           headers={"Authorization": KEY}).json()["data"]["teams"]["nodes"]
        assert teams == [{"key": "ENG"}]
    finally:
        proc.terminate()
        proc.wait(5)


def test_responses_conform_to_the_schema(mock):
    """Execution happens on the real schema; re-checking one rich answer guards the resolvers'
    value shapes (non-null fields, lists, scalars)."""
    control(mock.base, "POST", "issues", {"title": "Shape", "labels": ["bug"]})
    query = ("{ issues { nodes { id identifier number priority priorityLabel labelIds createdAt "
             "state { id name type position } team { key organization { urlKey } } "
             "labels { edges { cursor node { name color isGroup } } } children { nodes { id } } "
             "comments { pageInfo { hasNextPage startCursor } } } } }")
    body = gql(mock.base, query).json()
    assert "errors" not in body
    result = graphql.graphql_sync(graph.schema(), query, root_value=body["data"],
                                  field_resolver=lambda src, info, **a: src[info.field_name])
    assert result.errors is None and result.data == body["data"]
