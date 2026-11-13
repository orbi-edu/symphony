"""First-run setup: a daemon on an install that is not set up idles and reports why — it
never crash-loops — and starts working as soon as the WORKFLOW and the credential are there."""

import threading
import time

import pytest
from structlog.testing import capture_logs
from tests.conftest import FakeKV
from tests.test_dispatch import FakeEffects, cfg

from symphony import edge, state, wake
from symphony.config import ConfigHolder, parse_workflow
from symphony.entry.daemon import NO_WORKFLOW_REASON, await_workflow, run_loop

LINEAR = """---
tracker:
  kind: linear
  provider: {team_key: ENG, api_key: $LINEAR_API_KEY}
---
x
"""
GITHUB = """---
tracker:
  kind: github
  provider: {owner: acme, project_number: 3}
---
x
"""


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("LINEAR_API_KEY", *edge.GITHUB_APP_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# -- what "no usable tracker" means -------------------------------------------


def test_a_missing_linear_key_is_setup_incomplete_not_an_invalid_policy(clean_env):
    holder = ConfigHolder()
    assert holder.update(LINEAR, {})  # the policy itself applies
    with pytest.raises(edge.TrackerNotConfigured) as exc:
        edge.make_tracker(holder.config)
    assert "LINEAR_API_KEY" in exc.value.reason
    assert exc.value.code == "setup_incomplete" and exc.value.status_code == 409


def test_an_empty_key_and_a_missing_scope_are_setup_incomplete(clean_env):
    clean_env.setenv("LINEAR_API_KEY", "   ")
    with pytest.raises(edge.TrackerNotConfigured, match="LINEAR_API_KEY"):
        edge.make_tracker(parse_workflow(LINEAR))
    clean_env.setenv("LINEAR_API_KEY", "lin_secret_value")
    unscoped = parse_workflow(LINEAR.replace("team_key: ENG, ", ""))
    with pytest.raises(edge.TrackerNotConfigured) as exc:
        edge.make_tracker(unscoped)
    assert "team_key" in exc.value.reason
    assert "lin_secret_value" not in exc.value.reason  # reasons never carry the credential


def test_the_github_tracker_needs_the_app_credentials(clean_env):
    with pytest.raises(edge.TrackerNotConfigured) as exc:
        edge.make_tracker(parse_workflow(GITHUB))
    assert all(name in exc.value.reason for name in edge.GITHUB_APP_ENV)
    for name in edge.GITHUB_APP_ENV:
        clean_env.setenv(name, "x")
    assert edge.make_tracker(parse_workflow(GITHUB)) is not None


def test_a_credential_that_appears_is_used_at_the_next_call(clean_env):
    """Local mode applies `secrets.env` to the process at once: no restart, no cache."""
    gate, kv, cfg_ = edge.SetupGate(), FakeKV(), parse_workflow(LINEAR)
    tracker, ready = edge.usable_tracker(cfg_, kv, gate, 1_000)
    assert not ready and isinstance(tracker, edge.UnconfiguredTracker)
    assert kv.get(state.SETUP_KEY)["status"] == "incomplete"
    clean_env.setenv("LINEAR_API_KEY", "lin_now_set")
    tracker, ready = edge.usable_tracker(cfg_, kv, gate, 2_000)
    assert ready and not isinstance(tracker, edge.UnconfiguredTracker)
    assert kv.get(state.SETUP_KEY) == {"status": "ready", "reason": None, "at": 2_000}


def test_new_github_app_credentials_replace_the_cached_one(clean_env):
    for name in edge.GITHUB_APP_ENV:
        clean_env.setenv(name, "old")
    first = edge._tracker_credential()
    assert edge._tracker_credential() is first  # renewed in place, not re-minted per tick
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "new")
    second = edge._tracker_credential()
    assert second is not first and second.installation_id == "new"


# -- reporting ----------------------------------------------------------------


def test_setup_incomplete_is_logged_at_most_once_a_minute_and_written_every_step():
    gate, kv = edge.SetupGate(), FakeKV()
    with capture_logs() as logs:
        for t in range(0, 125_000, 5_000):  # 25 steps over ~2 minutes
            gate.update(kv, t, "The Linear API key is not set (LINEAR_API_KEY is empty or missing).")
            assert kv.get(state.SETUP_KEY)["at"] == t
        gate.update(kv, 130_000, None)
    incomplete = [e for e in logs if e["event"] == "daemon_setup_incomplete"]
    assert [e["reason"] for e in incomplete][:1] and len(incomplete) == 3  # t=0, 60 s, 120 s
    assert [e["event"] for e in logs][-1] == "daemon_setup_complete"


def test_commands_still_apply_and_tracker_bound_ones_fail_cleanly(redis_client):
    """Setup publishes the WORKFLOW through durable commands, so they must run while the
    tracker is missing; one that needs the tracker fails with the reason — it must not sit
    at the head of the queue as an outage would, blocking the publication behind it."""
    from symphony.admin_commands import AdminStore, process_commands
    from symphony.kv import RedisKV

    kv = RedisKV(redis_client)
    now = time.time_ns() // 1_000_000
    assert state.renew_lease(kv, "daemon", now)
    state.heartbeat(kv, now)
    store = AdminStore(redis_client, "test")

    def accept(action, target, key):
        return store.accept(
            actor="admin", role="admin", credential_fingerprint="f" * 64, action=action,
            target=target, reason="first-run setup", idempotency_key=key,
        )

    stuck = accept("reevaluate", {"issueId": "i1", "dispatchSeq": 1}, "k1")
    pause = accept("pause", None, "k2")
    reason = "The Linear board scope is not set (tracker.provider.team_key or project_slug)."
    done = process_commands(
        store, kv, edge.UnconfiguredTracker(reason), cfg(), FakeEffects(), "daemon", now,
        publish_workflow=lambda *a, **k: "rev", authorize=lambda record: True,
    )
    assert done == {"completed": 2}
    assert store.get(stuck["id"])["state"] == "failed"
    assert "setup_incomplete" in store.get(stuck["id"])["result"]
    assert store.get(pause["id"])["state"] == "succeeded"


# -- no WORKFLOW yet (fresh install: empty ConfigMap) --------------------------


@pytest.mark.parametrize("content", [None, "", "not a workflow"])
def test_without_a_workflow_the_daemon_idles_and_reports_setup(tmp_path, content):
    path = tmp_path / "WORKFLOW.md"
    if content is not None:
        path.write_text(content)
    kv, gate, holder = FakeKV(), edge.SetupGate(), ConfigHolder()
    step, configured = await_workflow(holder, kv, "daemon-1", gate, path=str(path))
    assert step([]) is True and not configured.is_set()
    assert kv.get(state.HEARTBEAT_KEY) is not None  # alive, and says so
    assert kv.get(state.SETUP_KEY) == {
        "status": "incomplete", "reason": NO_WORKFLOW_REASON, "at": kv.get(state.HEARTBEAT_KEY),
    }

    path.write_text("---\ntracker: {kind: memory}\n---\nx\n")  # setup seeds the ConfigMap
    assert step([]) is True and configured.is_set()
    assert holder.config is not None and holder.config.tracker_kind == "memory"


def test_a_standby_daemon_does_not_report_for_the_leader(tmp_path):
    kv, gate = FakeKV(), edge.SetupGate()
    assert state.renew_lease(kv, "leader", 10**15)
    step, _ = await_workflow(ConfigHolder(), kv, "standby", gate, path=str(tmp_path / "none"))
    assert step([]) is False
    assert kv.get(state.SETUP_KEY) is None


def test_the_idle_loop_ends_on_the_step_after_the_workflow_appears(tmp_path, redis_client):
    """End to end on the real loop: idle on the safety poll, then a wake (the setup API's)
    runs the next step at once, which finds the WORKFLOW and hands over to normal work."""
    from symphony.kv import RedisKV

    path = tmp_path / "WORKFLOW.md"
    holder = ConfigHolder()
    step, configured = await_workflow(
        holder, RedisKV(redis_client), "daemon-1", edge.SetupGate(), path=str(path)
    )
    thread = threading.Thread(
        target=run_loop, args=(step,), daemon=True,
        kwargs={"client": redis_client, "namespace": "test", "interval_s": lambda: 30.0,
                "stop": configured},
    )
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while redis_client.get(state.SETUP_KEY) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert redis_client.get(state.SETUP_KEY) is not None and thread.is_alive()
        path.write_text("---\ntracker: {kind: memory}\n---\nx\n")
        wake.wake(redis_client, "test", wake.DAEMON, "setup:workflow")
        thread.join(timeout=2)
        assert not thread.is_alive() and holder.config is not None
    finally:
        configured.set()
        wake.wake(redis_client, "test", wake.DAEMON, "shutdown")
        thread.join(timeout=5)
