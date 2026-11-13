"""The batch entrypoints on an install that is not set up: the sweep and reflector Jobs say
why once and exit 0; a runner Job fails its attempt with the reason, not a traceback."""

import pytest
from structlog.testing import capture_logs
from tests.conftest import FakeKV
from tests.test_dispatch import cfg

from symphony import edge, state
from symphony.config import ConfigHolder
from symphony.entry import reflect, run_issue, sweep

UNSET_LINEAR = "---\ntracker:\n  kind: linear\n  provider: {team_key: ENG}\n---\nx\n"


@pytest.fixture
def job(monkeypatch):
    """Patch a Job's process edges: the store, the mounted WORKFLOW and the cluster (which a
    setup-incomplete exit must never reach)."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    kv = FakeKV()

    def install(module, workflow):
        def load(holder: ConfigHolder, path=None):
            if workflow is not None:
                holder.update(workflow, {})

        def no_cluster():
            raise AssertionError("a setup-incomplete Job must not touch the cluster")

        monkeypatch.setattr(module, "RedisKV", lambda: kv)
        monkeypatch.setattr(module, "load_config", load)
        monkeypatch.setattr(module.kube, "get_clients", no_cluster)
        return kv

    return install


@pytest.mark.parametrize("workflow", [UNSET_LINEAR, None, ""])
def test_the_sweep_exits_cleanly_with_the_reason(job, workflow):
    job(sweep, workflow)
    with capture_logs() as logs:
        assert sweep.main() == "setup-incomplete"
    [event] = [e for e in logs if e["event"] == "sweep_setup_incomplete"]
    assert event["reason"]
    if workflow == UNSET_LINEAR:
        assert "LINEAR_API_KEY" in event["reason"]


@pytest.mark.parametrize("workflow", [UNSET_LINEAR, None])
def test_the_reflector_exits_cleanly_and_leaves_the_retro_for_the_sweep(job, monkeypatch, workflow):
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", "i1")
    monkeypatch.setenv("SYMPHONY_ENTRY_JSON", '{"identifier": "ENG-1"}')
    kv = job(reflect, workflow)
    started = {"status": "started", "reflector_call_id": None, "learnings_pr": None, "at": 1}
    kv.put(state.retro_key("i1"), started)
    with capture_logs() as logs:
        assert reflect.main() == "setup-incomplete"
    [event] = [e for e in logs if e["event"] == "reflector_setup_incomplete"]
    assert event["issue_id"] == "i1" and event["reason"]
    assert kv.get(state.retro_key("i1")) == started  # the sweep re-fires it once set up


def _claimed(kv, attempt=1, seq=4):
    kv.put(state.issue_key("i1"), {
        "status": "claimed", "identifier": "ENG-1", "attempt": attempt, "dispatch_seq": seq,
        "runner_call_id": "job-4", "bridge_token": None,
    })
    kv.put(state.dispatch_key("i1", seq), {"daemon_owner": "d1"})


def test_a_runner_fails_its_attempt_with_the_setup_reason(job, monkeypatch):
    kv = job(run_issue, UNSET_LINEAR)
    _claimed(kv)
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", "i1")
    monkeypatch.setenv("SYMPHONY_ATTEMPT", "1")
    monkeypatch.setenv("SYMPHONY_SEQ", "4")
    with capture_logs() as logs:
        assert run_issue.main() == "failed"
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == "retry_queued" and entry["attempt"] == 2
    assert entry["failure_summary"].startswith("Setup is incomplete: ")
    assert "LINEAR_API_KEY" in entry["failure_summary"]
    assert entry["runner_call_id"] is None and entry["bridge_token"] is None
    assert [e["event"] for e in logs].count("runner_setup_incomplete") == 1


def test_a_superseded_runner_records_nothing():
    kv = FakeKV()
    _claimed(kv, seq=5)  # a newer dispatch owns the issue
    before = dict(kv.get(state.issue_key("i1")))
    reason = edge.TrackerNotConfigured("x").reason
    assert run_issue.fail_setup_incomplete(kv, cfg(), "i1", 1, 4, reason, 10) == "fenced"
    assert kv.get(state.issue_key("i1")) == before
