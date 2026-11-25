"""The tracker-backed host tools on Linear (internals §3, O5/O2): sub-issue trees and
proof-of-work evidence, run in the runner through `LinearTracker` against Linear's GraphQL
API — here a fake Linear over HTTP."""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest
from tests.conftest import FakeKV

from symphony.hosttools import (
    CREATE_ISSUES_TOOL,
    EVIDENCE_TOOL,
    HostToolBroker,
    ToolInputError,
    tool_definitions,
)
from symphony.sessions import SessionStore
from symphony.trackers.base import TrackerInputError
from symphony.trackers.linear import LinearTracker
from symphony.workspace import LocalSandbox

TICKET = "ticket-uuid"
TEAM = "team-1"


def _invalid_id(spec: dict) -> httpx.Response | None:
    """Linear takes only version 4 UUIDs as a client-supplied id."""
    try:
        ok = uuid.UUID(spec["id"]).version == 4
    except ValueError:
        ok = False
    return None if ok else httpx.Response(400, json={"errors": [{
        "message": "Argument Validation Error",
        "extensions": {"code": "INVALID_INPUT", "userPresentableMessage": "id must be a UUID"}}]})


class FakeLinear:
    """Just enough of Linear's GraphQL API for the host tools, recording every mutation."""

    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.created: list[dict] = []  # issueCreate inputs, in order
        self.relations: list[dict] = []
        self.uploads: list[dict] = []
        self.attachments: list[dict] = []
        self.auth: set[str] = set()
        self.labels = [
            {"id": "lbl-repo-api", "name": "repo:acme/api", "team": {"id": TEAM}},
            {"id": "lbl-agent", "name": "agent", "team": {"id": TEAM}},
            {"id": "lbl-bug-team", "name": "bug", "team": {"id": TEAM}},
            {"id": "lbl-bug-ws", "name": "bug", "team": None},
            {"id": "lbl-docs", "name": "docs", "team": None},
            {"id": "lbl-other", "name": "frontend", "team": {"id": "team-2"}},
        ]
        self.refuse_relations = 0  # answer this many issueRelationCreate with success:false
        self.refuse_creates = 0  # same for issueCreate
        self.ticket_labels = [
            {"id": "lbl-repo-api", "name": "repo:acme/api"},
            {"id": "lbl-agent", "name": "agent"},
            {"id": "lbl-p1", "name": "P1"},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        body = json.loads(request.content)
        query, v = body["query"], body.get("variables") or {}
        if "SymphonyTicket" in query:
            data = {"issue": {"id": TICKET, "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
                              "team": {"id": TEAM}, "labels": {"nodes": self.ticket_labels}}}
        elif "SymphonyLabels" in query:
            names = {n.casefold() for n in v["names"]}
            data = {"issueLabels": {"nodes": [lbl for lbl in self.labels if lbl["name"].casefold() in names]}}
        elif "SymphonyFindIssue" in query:
            found = self.issues.get(v["id"])
            data = {"issues": {"nodes": [found] if found else []}}
        elif "SymphonyCreateIssue" in query:
            spec = v["input"]
            if rejected := _invalid_id(spec):
                return rejected
            if spec["id"] in self.issues:
                return httpx.Response(200, json={"errors": [{"message": "duplicate id"}]})
            if self.refuse_creates:
                self.refuse_creates -= 1
                return httpx.Response(200, json={"data": {"issueCreate": {"success": False, "issue": None}}})
            self.created.append(spec)
            n = len(self.issues) + 2
            issue = {"id": spec["id"], "identifier": f"ENG-{n}", "url": f"https://linear.app/x/ENG-{n}"}
            self.issues[spec["id"]] = issue
            data = {"issueCreate": {"success": True, "issue": issue}}
        elif "SymphonyBlocks" in query:
            if rejected := _invalid_id(v["input"]):
                return rejected
            if self.refuse_relations:
                self.refuse_relations -= 1
                return httpx.Response(200, json={"data": {"issueRelationCreate": {
                    "success": False, "issueRelation": None}}})
            self.relations.append(v["input"])
            data = {"issueRelationCreate": {"success": True, "issueRelation": {"id": v["input"]["id"]}}}
        elif "SymphonyUpload" in query:
            self.uploads.append(v)
            data = {"fileUpload": {"success": True, "uploadFile": {
                "uploadUrl": "https://uploads.example/put/abc?sig=1",
                "assetUrl": "https://uploads.linear.app/abc/shot.png",
                "headers": [{"key": "x-goog-meta-owner", "value": "symphony"}],
            }}}
        elif "SymphonyAttach" in query:
            self.attachments.append(v["input"])
            data = {"attachmentCreate": {"success": True, "attachment": {"id": "att-1", "url": v["input"]["url"]}}}
        else:
            raise AssertionError(f"unexpected query: {query}")
        return httpx.Response(200, json={"data": data})

    def by_title(self, title: str) -> dict:
        return next(spec for spec in self.created if spec["title"] == title)


class FakeUploads:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)


@pytest.fixture
def linear():
    return FakeLinear()


@pytest.fixture
def make_broker(redis_client, linear, tmp_path):
    store = SessionStore(redis_client, "test")
    store.open("i1:1", kind="implement", identifier="ENG-1")
    kv = FakeKV()
    uploads = FakeUploads()

    def make(owns=lambda: True, seq=1, workspace_root=None):
        def route(request: httpx.Request) -> httpx.Response:
            return (uploads if request.url.host == "uploads.example" else linear)(request)

        tracker = LinearTracker(
            api_key="lin_api_KEY", project_slug="p", active_states=["Todo"], terminal_states=["Done"],
            client=httpx.Client(transport=httpx.MockTransport(route)),
        )
        results: list[tuple[str, str, bool]] = []
        broker = HostToolBroker(
            store=store, sid="i1:1", kv=kv, tracker=tracker, issue_id=TICKET, seq=seq,
            identifier="ENG-1",
            respond=lambda cid, text, is_error=False: results.append((cid, text, is_error)),
            owns=owns, input_timeout_s=1, sandbox=LocalSandbox(str(tmp_path)),
            workspace_root=workspace_root or str(tmp_path), required_labels=("agent",),
        )
        broker.results = results
        return broker

    make.store, make.kv, make.uploads = store, kv, uploads
    return make


TREE = {"issues": [
    {"key": "api", "title": "API endpoint", "description": "Add /v1/things", "blocked_by": ["schema"]},
    {"key": "schema", "title": "DB schema", "description": "Migration", "labels": ["bug"]},
    {"key": "ui", "title": "UI", "description": "Screen", "labels": ["docs"], "blocked_by": ["api", "schema"]},
]}


def test_the_tracker_backed_tools_are_offered_with_linear():
    tracker = LinearTracker(api_key="k", project_slug="p", active_states=[], terminal_states=[])
    names = [t["name"] for t in tool_definitions(tracker)]
    assert names == ["linear_graphql", CREATE_ISSUES_TOOL, EVIDENCE_TOOL,
                     "symphony_ticket_attachments", "symphony_fetch_attachment", "symphony_ask"]


# -- symphony_create_issues ---------------------------------------------------------
def test_sub_issues_are_children_with_inherited_labels_and_blocks_relations(make_broker, linear):
    result = make_broker().create_issues(TREE)

    # blockers are created before what they block, whatever order the agent listed them in
    assert [spec["title"] for spec in linear.created] == ["DB schema", "API endpoint", "UI"]
    for spec in linear.created:
        assert spec["parentId"] == TICKET and spec["teamId"] == TEAM
        assert {"lbl-repo-api", "lbl-agent"} <= set(spec["labelIds"])
        assert "lbl-p1" not in spec["labelIds"]  # only repo: and the required label are inherited
    assert linear.by_title("DB schema")["labelIds"][-1] == "lbl-bug-team"  # the team's label wins
    assert "lbl-docs" in linear.by_title("UI")["labelIds"]

    ids = {spec["title"]: spec["id"] for spec in linear.created}
    pairs = {(r["issueId"], r["relatedIssueId"], r["type"]) for r in linear.relations}
    assert pairs == {
        (ids["DB schema"], ids["API endpoint"], "blocks"),
        (ids["API endpoint"], ids["UI"], "blocks"),
        (ids["DB schema"], ids["UI"], "blocks"),
    }
    assert [r["key"] for r in result] == ["api", "schema", "ui"]  # the agent's order
    assert result[1] == {"key": "schema", "identifier": "ENG-2", "url": "https://linear.app/x/ENG-2"}
    assert linear.auth == {"lin_api_KEY"}  # the runner's credential, used runner-side


def test_retries_and_later_attempts_never_duplicate(make_broker, linear):
    first = make_broker().create_issues(TREE)
    again = make_broker().create_issues(TREE)
    next_attempt = make_broker(seq=2).create_issues(TREE)
    assert first == again == next_attempt
    assert len(linear.created) == 3 and len(linear.relations) == 3


def test_a_refused_relation_is_an_error_and_is_retried_by_the_next_call(make_broker, linear):
    """Linear answers HTTP 200 + success:false; caching that would lose the edge forever."""
    tree = {"issues": [
        {"key": "schema", "title": "DB schema", "description": ""},
        {"key": "api", "title": "API", "description": "", "blocked_by": ["schema"]},
    ]}
    linear.refuse_relations = 1
    broker = make_broker()
    broker._serve({"type": "host_tool_call", "id": "h1", "toolCallId": "t1",
                   "toolName": CREATE_ISSUES_TOOL, "arguments": tree})
    (_, text, is_error) = broker.results[-1]
    assert is_error and "blocks" in text
    assert linear.relations == []

    broker._serve({"type": "host_tool_call", "id": "h2", "toolCallId": "t2",
                   "toolName": CREATE_ISSUES_TOOL, "arguments": tree})
    assert broker.results[-1][2] is False
    (relation,) = linear.relations
    assert relation["issueId"] == linear.by_title("DB schema")["id"]
    assert relation["relatedIssueId"] == linear.by_title("API")["id"]
    assert len(linear.created) == 2  # the sub-issues themselves were not duplicated

    broker.create_issues(tree)
    assert len(linear.relations) == 1  # recorded once it succeeded


def test_a_refused_issue_create_is_an_error_and_is_retried(make_broker, linear):
    tree = {"issues": [{"key": "a", "title": "A", "description": ""}]}
    linear.refuse_creates = 1
    with pytest.raises(RuntimeError, match="did not create"):
        make_broker().create_issues(tree)
    assert make_broker.kv.data == {}  # no idempotency record for a refused create
    assert [r["key"] for r in make_broker().create_issues(tree)] == ["a"]
    assert len(linear.created) == 1


def test_a_create_committed_before_a_crash_is_found_not_repeated(make_broker, linear):
    make_broker().create_issues(TREE)
    make_broker.kv.data.clear()  # the runner died after Linear committed, before recording
    result = make_broker().create_issues(TREE)
    assert len(linear.created) == 3
    assert [r["identifier"] for r in result] == ["ENG-3", "ENG-2", "ENG-4"]


def test_blocked_by_may_name_a_sub_issue_from_an_earlier_call(make_broker, linear):
    broker = make_broker()
    broker.create_issues({"issues": [{"key": "schema", "title": "DB schema", "description": ""}]})
    broker.create_issues({"issues": [{"key": "api", "title": "API", "description": "", "blocked_by": ["schema"]}]})
    (relation,) = linear.relations
    assert relation["issueId"] == linear.by_title("DB schema")["id"]
    assert relation["relatedIssueId"] == linear.by_title("API")["id"]


@pytest.mark.parametrize(
    ("issues", "error", "message"),
    [
        ([{"key": "a", "title": "A", "description": "", "blocked_by": ["b"]},
          {"key": "b", "title": "B", "description": "", "blocked_by": ["a"]}], ToolInputError, "cycle"),
        ([{"key": "a", "title": "A", "description": "", "blocked_by": ["ghost"]}], ToolInputError,
         "unknown key"),
        ([{"key": "a", "title": "A", "description": ""}, {"key": "a", "title": "A2", "description": ""}],
         ToolInputError, "duplicate key"),
        ([{"key": "a", "title": "A", "description": "", "labels": ["nope"]}], TrackerInputError,
         "unknown labels: nope"),
        ([{"key": "a", "title": "A", "description": "", "labels": ["frontend"]}], TrackerInputError,
         "unknown labels"),
    ],
)
def test_bad_trees_are_refused_before_anything_is_created(make_broker, linear, issues, error, message):
    with pytest.raises(error, match=message):
        make_broker().create_issues({"issues": issues})
    assert linear.created == [] and linear.relations == []


def test_the_tool_answers_through_omp_and_refuses_superseded_attempts(make_broker, linear):
    broker = make_broker()
    broker._serve({"type": "host_tool_call", "id": "h1", "toolCallId": "t1",
                   "toolName": CREATE_ISSUES_TOOL, "arguments": TREE})
    (_, text, is_error) = broker.results[-1]
    assert not is_error and [r["key"] for r in json.loads(text)] == ["api", "schema", "ui"]

    stale = make_broker(owns=lambda: False)
    stale._serve({"type": "host_tool_call", "id": "h2", "toolCallId": "t2",
                  "toolName": CREATE_ISSUES_TOOL, "arguments": {"issues": [
                      {"key": "x", "title": "X", "description": ""}]}})
    assert stale.results[-1][2] is True and len(linear.created) == 3


# -- symphony_attach_evidence ------------------------------------------------------
def test_evidence_is_uploaded_and_attached_to_the_ticket(make_broker, linear, tmp_path):
    payload = bytes(range(256)) * 50  # binary, NULs included
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "shot.png").write_bytes(payload)
    broker = make_broker()
    assert broker.attach_evidence({"path": "out/shot.png", "title": "Login page", "kind": "screenshot"}) == {
        "url": "https://uploads.linear.app/abc/shot.png"
    }

    assert linear.uploads == [{"contentType": "image/png", "filename": "shot.png", "size": len(payload)}]
    (put,) = make_broker.uploads.requests
    assert put.method == "PUT" and str(put.url) == "https://uploads.example/put/abc?sig=1"
    assert put.content == payload
    assert put.headers["content-type"] == "image/png"
    assert put.headers["x-goog-meta-owner"] == "symphony"
    assert linear.attachments == [{"issueId": TICKET, "title": "Login page", "subtitle": "screenshot",
                                   "url": "https://uploads.linear.app/abc/shot.png"}]
    assert make_broker.store.meta("i1:1")["evidence"] == [
        {"title": "Login page", "url": "https://uploads.linear.app/abc/shot.png", "kind": "screenshot"}
    ]


def test_in_the_local_mode_evidence_is_read_from_the_workspace_directory(make_broker, linear, tmp_path):
    """Found by the mocked certification: the runner gave the broker `workspace.root`
    (`/workspace`, the pod's mount) for a host sandbox too, so every local-mode attach
    failed with "could not read … (exit 2)". A host sandbox works in its own directory."""
    from symphony import runner
    from symphony.config import Config

    (tmp_path / "evidence.txt").write_bytes(b"symphony evidence\n")
    pod_default = Config(tracker_kind="linear")
    assert pod_default.workspace_root == "/workspace"
    root = runner.workspace_root(LocalSandbox(str(tmp_path)), pod_default)
    assert root == str(tmp_path)
    assert runner.workspace_root(object(), pod_default) == "/workspace", "a pod keeps the mount"
    make_broker(workspace_root=root).attach_evidence({"path": "evidence.txt", "title": "t", "kind": "log"})
    assert make_broker.uploads.requests[-1].content == b"symphony evidence\n"


def test_evidence_accumulates_in_the_session(make_broker, tmp_path):
    (tmp_path / "a.log").write_text("ok\n")
    broker = make_broker()
    broker.attach_evidence({"path": "a.log", "title": "Tests", "kind": "log"})
    broker.attach_evidence({"path": "a.log", "title": "Tests again", "kind": "log"})
    assert [e["title"] for e in make_broker.store.meta("i1:1")["evidence"]] == ["Tests", "Tests again"]


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "link-out", "inside/../../outside.txt"])
def test_paths_outside_the_workspace_are_refused(make_broker, linear, tmp_path, path):
    workspace = tmp_path
    (workspace.parent / "outside.txt").write_text("secret")
    (workspace / "inside").mkdir(exist_ok=True)
    link = workspace / "link-out"
    if not link.exists():
        os.symlink(workspace.parent / "outside.txt", link)
    with pytest.raises(ToolInputError, match="outside the workspace"):
        make_broker().attach_evidence({"path": path, "title": "x", "kind": "log"})
    assert linear.uploads == [] and make_broker.uploads.requests == []


def test_files_over_25_mb_and_missing_files_are_refused(make_broker, linear, tmp_path):
    with open(tmp_path / "big.mp4", "wb") as fh:
        fh.truncate(25 * 1024 * 1024 + 1)
    with pytest.raises(ToolInputError, match="larger than 25 MB"):
        make_broker().attach_evidence({"path": "big.mp4", "title": "x", "kind": "video"})
    with pytest.raises(ToolInputError, match="no such file"):
        make_broker().attach_evidence({"path": "nope.png", "title": "x", "kind": "screenshot"})
    assert linear.uploads == []


def test_a_failed_upload_attaches_nothing(make_broker, linear, tmp_path):
    (tmp_path / "r.md").write_text("# report\n")
    make_broker.uploads.status = 403
    broker = make_broker()
    broker._serve({"type": "host_tool_call", "id": "h3", "toolCallId": "t3", "toolName": EVIDENCE_TOOL,
                   "arguments": {"path": "r.md", "title": "Report", "kind": "report"}})
    (_, text, is_error) = broker.results[-1]
    assert is_error and "HTTP 403" in text
    assert linear.attachments == []
    assert "evidence" not in make_broker.store.meta("i1:1")


def test_an_unknown_kind_is_refused(make_broker):
    with pytest.raises(ToolInputError, match="kind must be one of"):
        make_broker().attach_evidence({"path": "a", "title": "x", "kind": "gif"})
