"""The agent reads its ticket's attachments (internals §3): `symphony_ticket_attachments`
lists them, `symphony_fetch_attachment` downloads one with the tracker credential on the
host and writes it into the workspace — only the current ticket's, only inside the
workspace, at most 25 MB."""

from __future__ import annotations

import json
import os

import httpx
import pytest
from tests.conftest import FakeKV

from symphony.hosttools import (
    ATTACHMENTS_TOOL,
    FETCH_TOOL,
    HostToolBroker,
    ToolInputError,
    tool_definitions,
)
from symphony.sessions import SessionStore
from symphony.trackers.base import TrackerInputError
from symphony.trackers.github import GitHubTracker
from symphony.trackers.linear import LinearTracker
from symphony.trackers.memory import MemoryTracker
from symphony.workspace import LocalSandbox

DRAFT = b"# Draft\n\nThe first version of the report.\n" + bytes(range(256))


@pytest.fixture
def board():
    tracker = MemoryTracker()
    tracker.add_issue("i1", "ENG-1")
    tracker.add_issue("i2", "ENG-2")
    tracker.attach_evidence("i1", title="Report draft", kind="report", filename="draft.md",
                            content_type="text/markdown", data=DRAFT)
    tracker.attach_evidence("i2", title="Other ticket", kind="log", filename="other.log",
                            content_type="text/plain", data=b"not yours\n")
    return tracker


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def make_broker(redis_client, workspace):
    def make(tracker, owns=lambda: True):
        store = SessionStore(redis_client, "test")
        store.open("i1:1", kind="implement", identifier="ENG-1")
        results: list[tuple[str, str, bool]] = []
        broker = HostToolBroker(
            store=store, sid="i1:1", kv=FakeKV(), tracker=tracker, issue_id="i1", seq=1,
            identifier="ENG-1",
            respond=lambda cid, text, is_error=False: results.append((cid, text, is_error)),
            owns=owns, input_timeout_s=1, sandbox=LocalSandbox(str(workspace)),
            workspace_root=str(workspace),
        )
        broker.results = results
        return broker

    return make


def serve(broker, name, arguments):
    broker._serve({"type": "host_tool_call", "id": "h1", "toolCallId": "t1", "toolName": name,
                   "arguments": arguments})
    _, text, is_error = broker.results[-1]
    return text, is_error


def test_the_ticket_lists_its_own_attachments_with_the_host_only(make_broker, board):
    text, is_error = serve(make_broker(board), ATTACHMENTS_TOOL, {})
    assert not is_error
    assert json.loads(text) == [{"id": "att-1", "title": "Report draft", "kind": "report",
                                 "host": "evidence", "created_at": "2026-01-01T00:00:00.000Z"}]


def test_an_attachment_is_fetched_into_the_workspace(make_broker, board, workspace):
    text, is_error = serve(make_broker(board), FETCH_TOOL, {"id": "att-1", "path": "notes/draft.md"})
    assert not is_error and json.loads(text) == {"path": "notes/draft.md", "bytes": len(DRAFT)}
    assert (workspace / "notes" / "draft.md").read_bytes() == DRAFT
    # again, over the earlier copy
    serve(make_broker(board), FETCH_TOOL, {"id": "att-1", "path": "notes/draft.md"})
    assert (workspace / "notes" / "draft.md").read_bytes() == DRAFT
    assert sorted(p.name for p in (workspace / "notes").iterdir()) == ["draft.md"]


def test_another_tickets_attachment_is_refused(make_broker, board, workspace):
    text, is_error = serve(make_broker(board), FETCH_TOOL, {"id": "att-2", "path": "other.log"})
    assert is_error and "no attachment 'att-2' on this ticket" in text
    assert not (workspace / "other.log").exists()


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md", "inside/../../outside.md",
                                  "link-dir/outside.md", "link-file", "dangling/x.md"])
def test_a_path_outside_the_workspace_is_refused(make_broker, board, workspace, path):
    outside = workspace.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (workspace / "inside").mkdir()
    os.symlink(outside, workspace / "link-dir")
    os.symlink(outside / "target.md", workspace / "link-file")
    os.symlink(workspace.parent / "missing", workspace / "dangling")
    with pytest.raises(ToolInputError, match="outside the workspace|relative path|a symlink"):
        make_broker(board).fetch_attachment({"id": "att-1", "path": path})
    assert list(outside.iterdir()) == []
    assert not (workspace.parent / "outside.md").exists()
    assert not (workspace.parent / "missing").exists()


def test_a_directory_at_the_path_is_refused(make_broker, board, workspace):
    (workspace / "notes").mkdir()
    with pytest.raises(ToolInputError, match="a directory or a symlink"):
        make_broker(board).fetch_attachment({"id": "att-1", "path": "notes"})


def test_an_attachment_over_25_mb_is_refused(make_broker, board, workspace):
    board.attach_evidence("i1", title="Recording", kind="video", filename="run.mp4",
                          content_type="video/mp4", data=b"\0" * (25 * 1024 * 1024 + 1))
    text, is_error = serve(make_broker(board), FETCH_TOOL, {"id": "att-3", "path": "run.mp4"})
    assert is_error and "larger than 25 MB" in text
    assert not (workspace / "run.mp4").exists()


def test_a_superseded_attempt_fetches_nothing(make_broker, board, workspace):
    text, is_error = serve(make_broker(board, owns=lambda: False), FETCH_TOOL,
                           {"id": "att-1", "path": "draft.md"})
    assert is_error and "no longer owns" in text and not (workspace / "draft.md").exists()


def test_id_and_path_are_required(make_broker, board):
    text, is_error = serve(make_broker(board), FETCH_TOOL, {"id": "att-1"})
    assert is_error and "id and path are required" in text


# -- trackers without files ------------------------------------------------------------
def _github():
    return GitHubTracker("acme", 7, ["Todo"], ["Done"], token=lambda: "unused")


def test_the_tools_are_offered_only_by_trackers_that_store_files():
    def names(tracker):
        return {tool["name"] for tool in tool_definitions(tracker)}

    linear = LinearTracker(api_key="k", project_slug="p", active_states=[], terminal_states=[])
    assert {ATTACHMENTS_TOOL, FETCH_TOOL} <= names(linear)
    assert {ATTACHMENTS_TOOL, FETCH_TOOL} <= names(MemoryTracker())
    assert not {ATTACHMENTS_TOOL, FETCH_TOOL} & names(_github())


def test_github_lists_nothing_and_cannot_fetch(make_broker):
    tracker = _github()
    assert tracker.ticket_attachments("acme/app-1") == []
    with pytest.raises(TrackerInputError, match="no file attachments"):
        tracker.fetch_attachment("acme/app-1", "x", max_bytes=10)
    broker = make_broker(tracker)
    assert serve(broker, ATTACHMENTS_TOOL, {}) == ("[]", False)
    text, is_error = serve(broker, FETCH_TOOL, {"id": "x", "path": "a.md"})
    assert is_error and "no file attachments" in text


# -- Linear's file storage, over HTTP ---------------------------------------------------
class LinearFiles:
    """Linear's attachments query and its file storage: the download needs the API key."""

    def __init__(self, nodes, files):
        self.nodes, self.files = nodes, files
        self.downloads: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            assert "SymphonyTicketAttachments" in body["query"]
            issue = {"attachments": {"nodes": self.nodes}} if body["variables"]["id"] == "i1" else None
            return httpx.Response(200, json={"data": {"issue": issue}})
        self.downloads.append(request)
        if request.headers.get("authorization") != "lin_api_KEY":
            return httpx.Response(401)
        data = self.files[str(request.url)]
        return httpx.Response(200, content=data, headers={"Content-Length": str(len(data))})


def _linear(nodes, files, monkeypatch):
    monkeypatch.delenv("LINEAR_API_URL", raising=False)
    storage = LinearFiles(nodes, files)
    tracker = LinearTracker(api_key="lin_api_KEY", project_slug="p", active_states=["Todo"],
                            terminal_states=["Done"],
                            client=httpx.Client(transport=httpx.MockTransport(storage)))
    return tracker, storage


UPLOAD = "https://uploads.linear.app/org/file-1/draft.md"
NODES = [
    {"id": "a1", "title": "Draft", "subtitle": "report", "url": UPLOAD, "createdAt": "2026-01-02T03:04:05.000Z"},
    {"id": "a2", "title": "PR", "subtitle": None, "url": "https://github.com/acme/app/pull/1",
     "createdAt": "2026-01-03T00:00:00.000Z"},
    {"id": "a3", "title": "Big", "subtitle": "video", "url": "https://uploads.linear.app/org/f/big.mp4",
     "createdAt": "2026-01-04T00:00:00.000Z"},
]


def test_linear_downloads_a_stored_file_with_the_api_key(monkeypatch):
    tracker, storage = _linear(NODES, {UPLOAD: DRAFT}, monkeypatch)
    assert tracker.ticket_attachments("i1")[0] == {
        "id": "a1", "title": "Draft", "kind": "report", "url": UPLOAD,
        "created_at": "2026-01-02T03:04:05.000Z"}
    assert tracker.fetch_attachment("i1", "a1", max_bytes=1024) == DRAFT
    (download,) = storage.downloads
    assert download.headers["authorization"] == "lin_api_KEY"


def test_linear_never_sends_the_key_to_a_link_or_for_another_ticket(monkeypatch):
    tracker, storage = _linear(NODES, {}, monkeypatch)
    with pytest.raises(TrackerInputError, match="links to github.com"):
        tracker.fetch_attachment("i1", "a2", max_bytes=1024)
    with pytest.raises(TrackerInputError, match="no attachment 'a1' on this ticket"):
        tracker.fetch_attachment("i2", "a1", max_bytes=1024)
    assert storage.downloads == []


def test_linear_refuses_a_file_over_the_cap(monkeypatch):
    big = "https://uploads.linear.app/org/f/big.mp4"
    tracker, _ = _linear(NODES, {big: b"x" * 2048}, monkeypatch)
    with pytest.raises(TrackerInputError, match="larger than"):
        tracker.fetch_attachment("i1", "a3", max_bytes=1024)
