"""Security and source-boundary contracts for the administrative read surface."""

import datetime as dt
import json
from fnmatch import fnmatchcase
from types import SimpleNamespace

import pytest
from tests.test_reflector import FakeGitHub, distill_nothing

from symphony import state
from symphony.admin_read import AdminProjection, ProjectionError
from symphony.config import parse_workflow
from symphony.reflector import run_retrospective


class RedisRead:
    def __init__(self, kv, failed_prefix=None):
        self.kv = kv
        self.failed_prefix = failed_prefix

    def get(self, key):
        if self.failed_prefix and key.startswith(self.failed_prefix):
            raise RuntimeError("password=do-not-expose")
        value = self.kv.get(key)
        return json.dumps(value) if value is not None else None

    def scan(self, cursor, match, count):
        if self.failed_prefix and match.startswith(self.failed_prefix):
            raise RuntimeError("password=do-not-expose")
        return 0, [key for key in self.kv.keys() if fnmatchcase(key, match)]


class Workflow:
    def __init__(self, unavailable=False):
        self.unavailable = unavailable

    def read(self):
        if self.unavailable:
            raise RuntimeError("token=do-not-expose")
        return {"values": {"kubernetes.default_repo": "acme/default"}}


def projection(kv, *, failed_prefix=None, workflow=None, core=None, batch=None):
    store = SimpleNamespace(paused=lambda: False, run_history=lambda issue_id, seq: None)
    return AdminProjection(
        SimpleNamespace(client=RedisRead(kv, failed_prefix)),
        store,
        core,
        batch,
        "only-this-namespace",
        workflow or Workflow(),
    )


def claim(kv, issue_id="i1"):
    now = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    state.claim_issue(kv, issue_id, "ENG-1", now)
    state.heartbeat(kv, now)
    return now


def test_requested_dispatch_never_returns_successor_or_unfenced_legacy_entry(kv):
    now = claim(kv)
    entry = kv.get(state.issue_key("i1"))
    entry.update(
        dispatch_seq=3, attempt=2, title="Current generation", last_clean_exit_at=now - 60_000
    )
    kv.put(state.issue_key("i1"), entry)
    reader = projection(kv)
    assert reader.run("i1", 2) is None
    assert reader.run("i1", 3)["title"] == "Current generation"
    assert all(event["title"] != "Clean exit recorded" for event in reader.run("i1", 3)["events"])
    del entry["dispatch_seq"]
    kv.put(state.issue_key("i1"), entry)
    assert reader.run("i1", 2) is None
    with pytest.raises(ProjectionError) as error:
        projection(kv, failed_prefix="issue:").run("i1", 2)
    assert error.value.status_code == 503
    assert "do-not-expose" not in str(error.value)


def test_run_projection_withholds_credentials_failure_text_and_unsafe_pr(kv):
    claim(kv)
    entry = kv.get(state.issue_key("i1"))
    entry.update(
        status="blocked",
        title="Waiting for review",
        target_repo="acme/repo",
        failure_summary="password=do-not-expose",
        bridge_token="do-not-expose",
        session_path="/private/do-not-expose.jsonl",
        tool_arguments={"secret": "do-not-expose"},
        pr_url="https://github.com@evil.example/acme/repo/pull/2",
    )
    entry.pop("tokens")
    kv.put(state.issue_key("i1"), entry)
    run = projection(kv).run("i1", 1)
    assert run["status"] == "blocked" and run["title"] == "Waiting for review"
    assert run["repo"] == "acme/repo"
    assert run["prUrl"] is None
    assert run["tokens"] is None and run["cost"] is None
    assert run["runtime"] is None and run["model"] is None
    assert "do-not-expose" not in json.dumps(run)
    entry["pr_url"] = "https://github.com/acme/repo/pull/23#discussion_r123"
    kv.put(state.issue_key("i1"), entry)
    assert projection(kv).run("i1", 1)["prUrl"] == entry["pr_url"]


def test_independent_failures_preserve_available_state_metrics_and_empty_sources(kv):
    now = claim(kv)
    month = dt.datetime.fromtimestamp(now / 1000, dt.UTC).strftime("%Y%m")
    kv.put(state.metrics_key(month), {"rework": 2, "prs_opened": 3})
    result = projection(
        kv, failed_prefix="telemetry:", workflow=Workflow(unavailable=True)
    ).snapshot()
    assert result["sources"]["state"]["status"] == "fresh"
    assert result["runs"][0]["issueId"] == "i1"
    assert result["sources"]["workflow"]["status"] == "unavailable"
    assert result["sources"]["kubernetes"]["status"] == "unavailable"
    assert result["sources"]["telemetry"]["status"] != "fresh"
    assert result["sources"]["learning"]["status"] == "fresh" and result["learning"] == []
    metric = result["telemetry"]["monthly"][0]
    assert metric["rework"] == 2 and metric["prsOpened"] == 3
    assert metric["dispatched"] is None
    assert result["telemetry"]["totals"] is None
    assert "do-not-expose" not in json.dumps(result)
    state_down = projection(kv, failed_prefix="issue:").snapshot()
    assert state_down["sources"]["state"]["status"] == "unavailable"
    assert state_down["sources"]["workflow"]["status"] == "fresh"
    assert state_down["repositories"][0]["name"] == "acme/default"
    assert state_down["repositories"][0]["issues"] is None


def test_namespace_inventory_is_bounded_and_redacts_resource_payloads(kv):
    claim(kv)

    class Core:
        def list_namespaced_pod(self, namespace, **options):
            assert namespace == "only-this-namespace"
            assert options["limit"] == 67 and options["_request_timeout"] == 5
            return {
                "metadata": {"continue": "private-continuation-do-not-expose"},
                "items": [
                    {
                        "metadata": {
                            "name": f"pod-{index}",
                            "labels": {"component": "runner", "secret": "do-not-expose"},
                            "annotations": {"credential": "do-not-expose"},
                        },
                        "spec": {"containers": [{"env": [{"value": "do-not-expose"}]}]},
                        "status": {
                            "phase": "Running",
                            "message": "do-not-expose",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }
                    for index in range(68)
                ],
            }

        def list_namespaced_persistent_volume_claim(self, namespace, **options):
            assert namespace == "only-this-namespace"
            return {"metadata": {}, "items": []}

    result = projection(kv, core=Core()).snapshot()
    assert len(result["resources"]) == 67
    assert result["limits"]["truncated"] is True
    assert result["sources"]["kubernetes"]["status"] == "stale"
    assert all(resource["kind"] == "Pod" for resource in result["resources"])
    pod = next(service for service in result["services"] if service["name"] == "pod-0")
    assert pod == {"name": "pod-0", "role": "runner", "status": "healthy", "detail": "ready"}
    assert result["resources"][0]["detail"] == "ready"
    assert "do-not-expose" not in json.dumps(result)


def test_retrospective_provenance_and_existing_pr_survive_issue_cleanup(kv):
    now = claim(kv)
    entry = kv.get(state.issue_key("i1"))
    entry.update(
        target_repo="acme/repo", bridge_token="do-not-expose", failure_summary="do-not-expose"
    )
    github = FakeGitHub(pr={"number": 7, "html_url": "https://github.com/acme/repo/pull/7"})
    run_retrospective(
        kv,
        github,
        parse_workflow("---\ntracker: {kind: memory}\n---\nx"),
        "i1",
        entry,
        "call-1",
        now,
        distill_nothing,
    )
    kv.delete(state.issue_key("i1"))
    result = projection(kv).snapshot()
    proposal = result["learning"][0]
    assert proposal["identifier"] == "ENG-1"
    assert proposal["provenance"] == "retrospective"
    assert proposal["outcome"] in {"pr_opened", "nothing_to_learn", "completed"}
    assert proposal["repo"] == "acme/repo"
    assert proposal["status"] == "completed"
    assert proposal["prUrl"] == "https://github.com/acme/repo/pull/7"
    assert result["runs"] == []
    assert any(repo["name"] == "acme/repo" for repo in result["repositories"])
    assert "do-not-expose" not in json.dumps(kv.get(state.retro_key("i1")))
