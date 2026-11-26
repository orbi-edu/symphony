"""A ticket whose repository set changes gets a new workspace, never the old one: the pod and
its session volume record the set they were built for, a different set discards both, the
credential is minted for the new set, and a run whose set changes mid-way ends at its next
turn boundary to be dispatched again (concepts § Multi-repository tickets)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException
from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, FakeSandbox, runner_env
from tests.test_dispatch import FakeEffects
from tests.test_dispatch import cfg as daemon_cfg

from symphony import ghapp, kube, state, telemetry
from symphony.config import Config, parse_workflow
from symphony.effects_local import LocalEffects, _Attempt
from symphony.entry.run_issue import installation_credential
from symphony.orchestrator import CONTINUATION_DELAY_MS, tick
from symphony.runner import run_attempt
from symphony.sessions import SessionStore
from symphony.teamconfig import TeamConfig
from symphony.trackers.memory import MemoryTracker

CFG = parse_workflow(
    "---\ntracker: {kind: memory}\nagent: {max_turns: 5}\n"
    "engine: {provider: acme, model: acme-1}\n---\nwork on {{ issue.identifier }}\n"
)


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).isoformat().replace("+00:00", "Z")


# -- the workspace's record ----------------------------------------------------------------
def _object(repos: str | None):
    annotations = {kube.REPOS_ANNOTATION: repos} if repos is not None else None
    return SimpleNamespace(metadata=SimpleNamespace(annotations=annotations),
                           status=SimpleNamespace(phase="Running"))


class WorkspaceCore:
    """The pod and session-volume calls of a Kubernetes core API, over two dicts."""

    def __init__(self, pod: str | None, volume: str | None) -> None:
        self.pods = {"ws-eng-1": _object(pod)}
        self.volumes = {"ws-eng-1-omp": _object(volume)}
        self.deleted: list[str] = []

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404)
        return self.pods[name]

    def delete_namespaced_pod(self, name, ns):
        if self.pods.pop(name, None) is None:
            raise ApiException(status=404)
        self.deleted.append(name)

    def read_namespaced_persistent_volume_claim(self, name, ns):
        if name not in self.volumes:
            raise ApiException(status=404)
        return self.volumes[name]

    def delete_namespaced_persistent_volume_claim(self, name, ns):
        if self.volumes.pop(name, None) is None:
            raise ApiException(status=404)
        self.deleted.append(name)


def test_the_pod_and_the_session_volume_record_the_repository_set():
    cfg = Config(tracker_kind="memory")
    pod = kube._workspace_manifest(cfg, "ws-eng-1", "img", ("acme/Web", "acme/api"))
    volume = kube._session_pvc_manifest(cfg, "ws-eng-1", ("acme/Web", "acme/api"))
    record = {kube.REPOS_ANNOTATION: "acme/api acme/web"}
    # One annotations dict on the pod: the record next to the eviction guards, neither lost.
    assert pod["metadata"]["annotations"] == {**record, **kube.BUSY_ANNOTATIONS}
    assert volume["metadata"]["annotations"] == record


def test_a_label_change_between_attempts_discards_the_pod_and_its_session_volume():
    core = WorkspaceCore(pod="acme/app", volume="acme/app")
    previous = kube.discard_if_built_for_other(
        core, "ns", Config(tracker_kind="memory"), "ws-eng-1",
        core.read_namespaced_pod("ws-eng-1", "ns"), ("acme/tools",))
    assert previous == ["acme/app"]
    assert core.deleted == ["ws-eng-1", "ws-eng-1-omp"] and not core.pods and not core.volumes


def test_a_reaped_pod_still_leaves_the_record_on_its_session_volume():
    core = WorkspaceCore(pod=None, volume="acme/app")
    del core.pods["ws-eng-1"]
    previous = kube.discard_if_built_for_other(
        core, "ns", Config(tracker_kind="memory"), "ws-eng-1", None, ("acme/tools",))
    assert previous == ["acme/app"] and core.deleted == ["ws-eng-1-omp"]


@pytest.mark.parametrize(("recorded", "repos"), [
    ("acme/api acme/web", ("acme/web", "acme/API")),  # same set: order and case are not a change
    (None, ("acme/tools",)),  # built before the record existed: the clone check decides
])
def test_an_unchanged_or_unrecorded_set_keeps_the_workspace(recorded, repos):
    core = WorkspaceCore(pod=recorded, volume=recorded)
    kept = kube.discard_if_built_for_other(
        core, "ns", Config(tracker_kind="memory"), "ws-eng-1",
        core.read_namespaced_pod("ws-eng-1", "ns"), repos)
    assert kept is None and core.deleted == []


def test_the_credential_is_minted_for_the_set_the_ticket_names_now(monkeypatch):
    minted = []
    monkeypatch.setattr(ghapp.InstallationCredential, "from_env",
                        classmethod(lambda cls, environ, now, **kw: minted.append(kw["target_repos"])))
    tracker = MemoryTracker(default_repo="acme/app")
    cfg = Config(tracker_kind="memory")
    environ = {"GITHUB_APP_ID": "1"}
    installation_credential(tracker.add_issue("i1", "ENG-1"), cfg, environ, lambda: 0)
    installation_credential(tracker.add_issue("i1", "ENG-1", labels=("repo:acme/tools",)),
                            cfg, environ, lambda: 0)
    assert minted == [("acme/app",), ("acme/tools",)]


# -- the local mode ----------------------------------------------------------------------
def test_the_local_mode_rebuilds_a_workspace_built_for_another_set(redis_client, tmp_path):
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nhooks:\n  after_create: |\n"
        "    git init -q . && git remote add origin \"https://github.com/${SYMPHONY_TARGET_REPO}.git\"\n"
        "---\nx\n"
    )
    tracker = MemoryTracker()
    fx = LocalEffects(cfg, kv=FakeKV(), tracker_for=lambda c: tracker,
                      sessions=SessionStore(redis_client, "test"), team=TeamConfig(redis_client, "test"),
                      root=tmp_path)
    record = _Attempt(call_id="c1", issue_id="i1", seq=1, sandbox_name="ws-eng-1")

    first = fx._ensure_sandbox(record, tracker, tracker.add_issue("i1", "ENG-1", labels=("repo:acme/app",)), cfg)
    session = fx.session_dir("ws-eng-1")
    session.mkdir(parents=True)
    (session / "s.jsonl").write_text("{}\n")
    again = fx._ensure_sandbox(record, tracker, tracker.add_issue("i1", "ENG-1", labels=("repo:acme/app",)), cfg)
    assert first.replaced_repos is None and again.replaced_repos is None
    assert (session / "s.jsonl").exists(), "the same set reuses the workspace and its session"

    moved = fx._ensure_sandbox(record, tracker, tracker.add_issue("i1", "ENG-1", labels=("repo:acme/tools",)), cfg)
    assert moved.replaced_repos == ["acme/app"]
    assert not session.exists(), "the old engine session goes with the old workspace"
    code, origin = moved.exec_script("git remote get-url origin")
    assert code == 0 and origin.strip() == "https://github.com/acme/tools.git"


# -- the runner --------------------------------------------------------------------------
class RebuiltSandbox(FakeSandbox):
    def __init__(self, replaced) -> None:
        super().__init__()
        self.replaced_repos = replaced


@pytest.fixture
def attempt(redis_client):
    def make(sandbox, session_path="/sessions/old.jsonl", cfg=CFG):
        kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
        tracker.add_issue("i1", "ENG-1", state="Todo", labels=("repo:acme/tools",),
                          updated_at=iso(clock.now_ms()))
        kv.put(state.issue_key("i1"), {
            "status": "claimed", "identifier": "ENG-1", "attempt": 1, "dispatch_seq": 1,
            "runs": 1, "tokens": {}, "session_path": session_path, "failure_summary": None,
        })
        kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "d1"})
        engine = FakeEngine()
        env = runner_env(redis_client, kv=kv, tracker=tracker, cfg=cfg, clock=clock,
                         engine=engine, sandbox=sandbox)
        return env, kv, tracker, engine, clock

    return make


def test_a_rebuilt_workspace_starts_a_new_engine_session_and_says_so(attempt):
    env, kv, _, engine, _ = attempt(RebuiltSandbox(["acme/app"]), cfg=replace(CFG, max_turns=1))
    engine.run()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert engine.commands("switch_session") == []
    entry = kv.get(state.issue_key("i1"))
    assert entry["repos_changed_from"] == ["acme/app"]
    report = telemetry.render_session_report(entry)
    assert "rebuilt when the ticket's repositories changed (was `acme/app`)" in report


def test_an_unchanged_set_resumes_the_engine_session(attempt):
    env, kv, _, engine, _ = attempt(RebuiltSandbox(None), cfg=replace(CFG, max_turns=1))
    engine.run()
    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert [c["sessionPath"] for c in engine.commands("switch_session")] == ["/sessions/old.jsonl"]
    assert "repos_changed_from" not in kv.get(state.issue_key("i1"))


def test_a_set_that_changes_mid_run_ends_it_at_the_turn_boundary_and_redispatches(attempt):
    env, kv, tracker, engine, clock = attempt(FakeSandbox(), session_path=None)

    def relabel(_engine):
        tracker.add_issue("i1", "ENG-1", state="Todo", labels=("repo:acme/tools", "repo:acme/web"),
                          updated_at=iso(clock.now_ms() + 1_000))
        return True

    engine.push(relabel)
    engine.run()
    for _ in range(4):
        engine.run()

    assert run_attempt(env, "i1", 1, 1) == "continuation"
    assert engine.prompted == 1, "no turn runs in the workspace of the old set"
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == "retry_queued" and entry["target_repos"] == ["acme/tools"]

    clock.advance(CONTINUATION_DELAY_MS)
    fx = FakeEffects()
    tick(kv, tracker, daemon_cfg(), clock.now_ms(), fx, owner="d1")
    assert fx.spawned == [("i1", 1)], "redispatched at once: the ticket changed, no cooldown"
