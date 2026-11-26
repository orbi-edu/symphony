"""A runner's workspace pod carries the eviction guards exactly while an attempt uses it:
a reused pod regains them before anything runs in it, and a pod whose preparation fails
is released, so it neither loses its working tree nor pins a node."""

from types import SimpleNamespace

import pytest
from tests.conftest import FakeKV

from symphony import kube, state
from symphony import runner as runner_mod
from symphony.config import ConfigHolder
from symphony.entry import run_issue
from symphony.trackers.base import Issue

WORKFLOW = "---\ntracker: {kind: memory}\n---\nx\n"
GUARD = "karpenter.sh/do-not-disrupt"
ISSUE = Issue(
    id="i1", identifier="ENG-1", title="t", description=None, state="Todo",
    state_type="active", target_repo="acme/app",
)


def _cfg():
    holder = ConfigHolder()
    holder.update(WORKFLOW, {})
    return holder.config


def _pod(image: str, annotations: dict | None = None):
    container = SimpleNamespace(name=kube.WORKSPACE_CONTAINER, image=image, env_from=None)
    return SimpleNamespace(
        metadata=SimpleNamespace(annotations=annotations),
        spec=SimpleNamespace(containers=[container]),
        status=SimpleNamespace(phase="Running"),
    )


class _Core:
    """The pod and session-volume API the runner reaches: reads, creates, deletes and
    annotation patches. Deletes and patches are recorded in order; `created` keeps the
    annotations of every pod this runner created."""

    def __init__(self, events: list, pod=None) -> None:
        self.events = events
        self.pod = pod
        # A reused pod comes with its session volume, carrying the same record.
        self.pvc = pod.metadata if pod is not None else None
        self.created: list[dict] = []

    def read_namespaced_pod(self, name, ns):
        if self.pod is None:
            raise kube._api_exception()(status=404)
        return self.pod

    def read_namespaced_persistent_volume_claim(self, name, ns):
        if self.pvc is None:
            raise kube._api_exception()(status=404)
        return SimpleNamespace(metadata=self.pvc)

    def create_namespaced_persistent_volume_claim(self, ns, body):
        self.pvc = SimpleNamespace(annotations=body["metadata"].get("annotations"))

    def delete_namespaced_persistent_volume_claim(self, name, ns):
        self.events.append(("delete_pvc",))
        self.pvc = None

    def create_namespaced_pod(self, ns, body):
        annotations = body["metadata"].get("annotations")
        self.created.append(annotations)
        self.pod = _pod(body["spec"]["containers"][0]["image"], annotations)

    def patch_namespaced_pod(self, name, ns, body):
        self.events.append(("patch", body["metadata"]["annotations"]))

    def delete_namespaced_pod(self, name, ns):
        self.events.append(("delete",))
        self.pod = None


class _RacedCore(_Core):
    """Another runner creates the pod first: this create gets a 409 and reattaches."""

    def create_namespaced_pod(self, ns, body):
        super().create_namespaced_pod(ns, body)
        raise kube._api_exception()(status=409)


class _KV(FakeKV):
    client = None


@pytest.fixture
def runner(monkeypatch):
    """A runner Job whose attempt only prepares the sandbox. Returns the cluster events,
    a function that runs the Job and returns the prepared sandboxes, and the store."""
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", "i1")
    monkeypatch.setenv("SYMPHONY_ATTEMPT", "1")
    monkeypatch.setenv("SYMPHONY_SEQ", "4")
    kv = _KV()
    kv.put(state.issue_key("i1"), {
        "status": "running", "identifier": "ENG-1", "attempt": 1, "dispatch_seq": 4,
    })
    kv.put(state.dispatch_key("i1", 4), {"daemon_owner": "d1"})
    events: list = []
    prepared: list = []

    def load(holder: ConfigHolder, path=None):
        holder.update(WORKFLOW, {})

    def attempt(env, issue_id, attempt, seq):
        try:
            prepared.append(env.ensure_sandbox(ISSUE, env.cfg))
        except RuntimeError:
            prepared.append(None)
        return "failed"

    def run(core: _Core, **fakes):
        monkeypatch.setattr(run_issue.kube, "get_clients", lambda: (core, None))
        for name, fake in {
            "workspace_matches_repo": lambda sb, root, repo: events.append(("exec",)) or True,
            "kill_agent_processes": lambda sb: events.append(("exec",)) or True,
            "prepare_provider": lambda *a: None,
            "run_hook": lambda *a, **kw: None,
            **fakes,
        }.items():
            monkeypatch.setattr(run_issue, name, fake)
        run_issue.main()
        return prepared

    monkeypatch.setattr(run_issue, "RedisKV", lambda: kv)
    monkeypatch.setattr(run_issue, "load_config", load)
    monkeypatch.setattr(
        run_issue, "AdminStore", lambda *a: SimpleNamespace(observe_workflow=lambda *a: None)
    )
    monkeypatch.setattr(runner_mod, "run_attempt", attempt)
    return events, run, kv


BUSY = {
    "symphony.dev/idle-since": None,
    GUARD: "true",
    "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
}


def test_a_reused_workspace_regains_its_guards_before_anything_runs_in_it(runner):
    events, run, _ = runner
    [sandbox] = run(_Core(events, pod=_pod(kube.agent_image_for(_cfg(), None))))
    assert sandbox is not None
    assert events[0] == ("patch", BUSY)
    assert ("exec",) in events[1:]


def test_a_workspace_reattached_after_a_lost_create_race_regains_its_guards(runner):
    events, run, _ = runner
    [sandbox] = run(_RacedCore(events))
    assert sandbox is not None
    assert events[0] == ("patch", BUSY)
    assert ("exec",) in events[1:]


def test_a_workspace_of_another_repository_is_recreated_without_a_release(runner):
    events, run, _ = runner
    core = _Core(events, pod=_pod(kube.agent_image_for(_cfg(), None)))
    [sandbox] = run(
        core,
        workspace_matches_repo=lambda sb, root, repo: events.append(("exec",)) and False,
    )
    assert sandbox is not None
    # Busy, checked, discarded with its session volume, recreated with the guards in its
    # manifest: no idle clock.
    assert events == [("patch", BUSY), ("exec",), ("delete",), ("delete_pvc",)]
    assert core.created == [{kube.REPOS_ANNOTATION: "acme/app", **kube.BUSY_ANNOTATIONS}]
    # An unrecorded set was replaced: the session restarts, with no previous set to name.
    assert sandbox.replaced_repos == []


def test_a_workspace_built_for_another_repository_set_is_discarded(runner):
    events, run, _ = runner
    image = kube.agent_image_for(_cfg(), None)
    core = _Core(events, pod=_pod(image, {kube.REPOS_ANNOTATION: "acme/old"}))
    [sandbox] = run(core)
    assert sandbox is not None
    # Discarded before it is claimed (no busy patch on the old pod), then built for the new
    # set: one annotations dict carries both the repository record and the guards.
    assert events == [("delete",), ("delete_pvc",)]
    assert core.created == [{kube.REPOS_ANNOTATION: "acme/app", **kube.BUSY_ANNOTATIONS}]
    assert sandbox.replaced_repos == ["acme/old"]


def test_a_zombie_workspace_is_recreated_keeping_no_replaced_set(runner):
    events, run, _ = runner
    core = _Core(events, pod=_pod(kube.agent_image_for(_cfg(), None)))
    [sandbox] = run(core, kill_agent_processes=lambda sb: events.append(("exec",)) and False)
    assert sandbox is not None
    assert events == [("patch", BUSY), ("exec",), ("exec",), ("delete",)]
    assert sandbox.replaced_repos is None  # same set: the session is resumed


def test_a_workspace_whose_preparation_fails_is_released(runner):
    events, run, _ = runner

    def failing_hook(sandbox, hook, cfg, **kw):
        if hook == "after_create":
            raise RuntimeError("clone failed")

    assert run(_Core(events), run_hook=failing_hook) == [None]
    [(kind, release)] = events
    assert kind == "patch"
    assert release[GUARD] is None
    assert int(release["symphony.dev/idle-since"]) > 0  # the reaper's clock runs


def test_a_failed_release_does_not_mask_the_preparation_failure(runner):
    events, run, _ = runner

    class _Unreachable(_Core):
        def patch_namespaced_pod(self, name, ns, body):
            raise ConnectionError("api server unreachable")

    def failing_hook(sandbox, hook, cfg, **kw):
        if hook == "after_create":
            raise RuntimeError("clone failed")

    # The attempt still sees the RuntimeError, not the release's ConnectionError.
    assert run(_Unreachable(events), run_hook=failing_hook) == [None]


def test_a_superseded_runner_leaves_a_failed_workspace_to_the_new_owner(runner):
    events, run, kv = runner

    def superseded_then_failing(sandbox, hook, cfg, **kw):
        if hook == "after_create":
            kv.delete(state.dispatch_key("i1", 4))  # a newer dispatch owns the issue now
            raise RuntimeError("clone failed")

    assert run(_Core(events), run_hook=superseded_then_failing) == [None]
    assert events == []  # no release patch strips the new owner's guards
