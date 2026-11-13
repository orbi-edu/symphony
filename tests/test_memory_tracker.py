"""The memory tracker implements the whole `TrackerAdapter` write side, so the host tools and
the merger run on it like on Linear or GitHub."""

from __future__ import annotations

import json

import pytest
from tests.conftest import FakeKV

from symphony.hosttools import CREATE_ISSUES_TOOL, EVIDENCE_TOOL, HostToolBroker
from symphony.sessions import SessionStore
from symphony.trackers.base import TrackerInputError
from symphony.trackers.memory import MemoryTracker
from symphony.workspace import LocalSandbox


@pytest.fixture
def tracker():
    t = MemoryTracker()
    t.add_issue("i1", "ENG-1", state="In Progress", labels=("repo:acme/app", "symphony", "p1"))
    return t


def test_move_comment_and_workpad(tracker):
    assert tracker.move("ENG-1", "Rework") is True
    assert tracker.move("i1", "Rework") is False
    assert tracker.move("ENG-404", "Rework") is False
    assert tracker.state_of("eng-1") == {"name": "Rework", "type": "active"}
    tracker.move("ENG-1", "Done")
    assert tracker.state_of("ENG-1") == {"name": "Done", "type": "terminal"}
    assert tracker.state_of("ENG-404") is None

    note = tracker.comment("ENG-1", "a note")
    pad = tracker.upsert_workpad("ENG-1", "## Workpad\n\n- [ ] plan")
    assert pad != note
    assert tracker.upsert_workpad("i1", "## Workpad\n\n- [x] plan") == pad
    assert [c["body"] for c in tracker.comments["i1"]] == ["a note", "## Workpad\n\n- [x] plan"]
    assert tracker.fetch_issue_workpad("ENG-1") == "a note\n\n## Workpad\n\n- [x] plan"
    with pytest.raises(TrackerInputError):
        tracker.comment("ENG-404", "x")


def test_create_issue_evidence_and_identifiers(tracker):
    created = tracker.create_issue("Main CI red", "body", labels=["repo:acme/app", "CI"], repo="acme/app")
    assert created["identifier"].startswith("MEM-")
    assert tracker.state_of(created["identifier"]) == {"name": "Todo", "type": "active"}
    (issue,) = [i for i in tracker._issues.values() if i.identifier == created["identifier"]]
    assert issue.labels == ("repo:acme/app", "ci") and issue.target_repo == "acme/app"

    entry = tracker.attach_evidence("ENG-1", title="Tests", kind="log", filename="t.log",
                                    content_type="text/plain", data=b"ok\n")
    assert tracker.attachments("ENG-1") == [entry] and entry["kind"] == "log"
    assert tracker.identifier_in("feature/eng-1-avg") == "ENG-1"
    assert tracker.identifier_in("ENG-10 is another") is None


def _broker(redis_client, tracker, kv, tmp_path):
    store = SessionStore(redis_client, "test")
    store.open("i1:1", kind="implement", identifier="ENG-1")
    results: list = []
    broker = HostToolBroker(
        store=store, sid="i1:1", kv=kv, tracker=tracker, issue_id="i1", seq=1, identifier="ENG-1",
        respond=lambda cid, text, is_error=False: results.append((text, is_error)), owns=lambda: True,
        input_timeout_s=1, sandbox=LocalSandbox(str(tmp_path)), workspace_root=str(tmp_path),
        required_labels=("symphony",),
    )
    broker.results, broker.store_ = results, store
    return broker


def test_host_tools_run_on_the_memory_tracker(redis_client, tracker, tmp_path):
    kv = FakeKV()
    tree = {"issues": [
        {"key": "api", "title": "API", "description": "", "blocked_by": ["schema"]},
        {"key": "schema", "title": "Schema", "description": "", "labels": ["db"]},
    ]}
    broker = _broker(redis_client, tracker, kv, tmp_path)
    broker._serve({"type": "host_tool_call", "id": "h1", "toolCallId": "t1",
                   "toolName": CREATE_ISSUES_TOOL, "arguments": tree})
    text, is_error = broker.results[-1]
    assert not is_error
    result = json.loads(text)
    assert [r["key"] for r in result] == ["api", "schema"]
    by_key = {r["key"]: r for r in result}
    (api,) = [i for i in tracker._issues.values() if i.identifier == by_key["api"]["identifier"]]
    (schema,) = [i for i in tracker._issues.values() if i.identifier == by_key["schema"]["identifier"]]
    assert set(schema.labels) == {"repo:acme/app", "symphony", "db"}  # not `p1`
    assert tracker.parents[api.id] == "i1" and tracker.relations == [(schema.id, api.id)]
    assert [b["identifier"] for b in api.blocked_by] == [schema.identifier] and not api.dispatchable

    # a retry and a later attempt make nothing new
    assert _broker(redis_client, tracker, kv, tmp_path).create_issues(tree) == result
    assert len(tracker._issues) == 3 and len(tracker.relations) == 1

    (tmp_path / "t.log").write_text("ok\n")
    url = broker.attach_evidence({"path": "t.log", "title": "Tests", "kind": "log"})["url"]
    assert tracker.attachments("ENG-1") == [{"title": "Tests", "url": url, "kind": "log"}]
    assert broker.store_.meta("i1:1")["evidence"] == [{"title": "Tests", "url": url, "kind": "log"}]

    broker._serve({"type": "host_tool_call", "id": "h2", "toolCallId": "t2", "toolName": EVIDENCE_TOOL,
                   "arguments": {"path": "nope.log", "title": "x", "kind": "log"}})
    assert broker.results[-1] == ("no such file in the workspace: nope.log", True)
