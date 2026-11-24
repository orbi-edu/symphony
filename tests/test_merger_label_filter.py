"""Label filter: when active, the merger reviews only PRs with at least one of the
configured labels or the review-bypass label.

Tests: filter off (unlabeled PR picked), filter on (unlabeled not picked, labeled picked,
review-bypass picked), per-repo override, train immunity, live toggle, config parsing,
workflow API validation."""

from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest
from tests.test_merger_support import (
    NS,
    FakeGitHub,
    FakeTracker,
    ScriptedAgents,
    World,
    done_review,
    done_verify,
    finding,
    ids_in,
    make_merger,
    verdict,
)

from symphony.config import ConfigError, parse_workflow
from symphony.entry.merger import MergerFleet, _effective_label_filter
from symphony.merger import comments as cm
from symphony.merger.review import ReviewWorker, eligible, pick
from symphony.merger.store import MergerStore
from symphony.merger.train import Train
from symphony.sessions import SessionStore

APP = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(12))

WORKFLOW = "---\ntracker:\n  kind: memory\n---\nbody\n"


# -- helpers -----------------------------------------------------------------

@pytest.fixture
def env(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"))
    world.commit("main", {"src/app.py": APP}, "app: many functions", base="main")
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=tracker, notes=notes)
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, tracker=tracker, notes=notes,
                           worker=ReviewWorker(m, "1"), sessions=SessionStore(redis_client, NS))


def open_pr(env, n=7, branch="eng-12-avg", files=None, title="ENG-12 add avg", labels=()):
    env.world.commit(branch, files or {"src/app.py": APP.replace("return 1\n", "return 1 / 0\n", 1)},
                     "app: change f1")
    env.gh.open_pr(n, branch, title, labels=labels)
    env.gh.tick()


def setup_simple_review(env):
    """Script agents for a simple passing review."""
    def reviewer(role, prompt, cwd):
        return [finding("src/app.py:6", "f1 divides by zero", severity="nit", cls="nit"), done_review()]

    def oracle(role, prompt, cwd):
        out = [verdict(fid, severity="nit", cls="nit") for fid in ids_in(prompt)]
        return [*out, done_verify()]

    env.agents.scripts.update(reviewer=reviewer, oracle=oracle)


# -- filter off: unlabeled PR is picked (today's behaviour) ------------------

def test_filter_off_unlabeled_pr_is_picked(env):
    """With label_filter_active=False, an unlabeled PR is picked as before."""
    env.m.label_filter_active = False
    setup_simple_review(env)
    open_pr(env, labels=())
    result = env.worker.step()
    assert result is not None and "#7" in result


# -- filter on: unlabeled PR not picked --------------------------------------

def test_filter_on_unlabeled_pr_not_picked(env):
    """With label_filter_active=True, an unlabeled PR is not picked."""
    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony", "symphony-learnings")
    open_pr(env, labels=())
    result = env.worker.step()
    assert result is None


def test_filter_on_unlabeled_pr_not_in_queue(env):
    """An unlabeled PR does not appear in the queue when the filter is active."""
    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony", "symphony-learnings")
    open_pr(env, labels=())
    # pick() populates the queue
    policy = env.m.policy()
    p = pick(env.m, policy, "worker:1")
    assert p is None
    queue = env.m.store.queue()
    assert queue == []


def test_filter_on_symphony_label_picked(env):
    """A PR with the 'symphony' label is picked when the filter is active."""
    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony", "symphony-learnings")
    setup_simple_review(env)
    open_pr(env, labels=("symphony",))
    result = env.worker.step()
    assert result is not None and "#7" in result


def test_filter_on_learnings_label_picked(env):
    """A PR with the 'symphony-learnings' label is picked when the filter is active."""
    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony", "symphony-learnings")
    setup_simple_review(env)
    open_pr(env, labels=("symphony-learnings",))
    result = env.worker.step()
    assert result is not None and "#7" in result


def test_filter_on_bypass_label_picked(env):
    """A PR with only review-bypass is always picked, even when the filter is active."""
    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony", "symphony-learnings")
    open_pr(env, labels=(cm.BYPASS,))
    result = env.worker.step()
    assert result is not None and "#7" in result and "bypass" in result.lower()


# -- per-repo override -------------------------------------------------------

def test_per_repo_override_true_beats_global_false():
    """A per-repo override true forces the filter on even when global is false."""
    cfg = parse_workflow(WORKFLOW.replace(
        "---\nbody",
        "merger:\n  label_filter:\n    enabled: false\n    repos:\n      acme/app: true\n---\nbody"
    ))
    active, labels = _effective_label_filter(cfg, "acme/app")
    assert active is True
    assert labels == ("symphony", "symphony-learnings")


def test_per_repo_override_false_beats_global_true():
    """A per-repo override false forces the filter off even when global is true."""
    cfg = parse_workflow(WORKFLOW.replace(
        "---\nbody",
        "merger:\n  label_filter:\n    enabled: true\n    repos:\n      acme/app: false\n---\nbody"
    ))
    active, labels = _effective_label_filter(cfg, "acme/app")
    assert active is False
    assert labels == ()


def test_per_repo_override_absent_uses_global():
    """Without a per-repo override, the global setting applies."""
    cfg = parse_workflow(WORKFLOW.replace(
        "---\nbody",
        "merger:\n  label_filter:\n    enabled: false\n    repos:\n      acme/lib: true\n---\nbody"
    ))
    # acme/app has no override → uses global (false)
    active, _ = _effective_label_filter(cfg, "acme/app")
    assert active is False
    # acme/lib has override → true
    active2, _ = _effective_label_filter(cfg, "acme/lib")
    assert active2 is True


# -- train immunity ----------------------------------------------------------

def test_pr_in_train_not_dropped_when_filter_turns_on(env):
    """An unlabeled PR reviewed while the filter was off is merged by the train after the
    filter turns on: the filter gates intake only."""
    env.m.label_filter_active = False
    setup_simple_review(env)
    open_pr(env, labels=())
    assert env.worker.step() == "#7 reviewed"
    assert env.m.store.rec(7)["state"] == "reviewed"

    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony",)

    assert env.worker.step() is None  # nothing new to take in
    assert env.m.store.rec(7)["state"] == "reviewed"  # the filter left the record alone
    out = Train(env.m).step()
    assert out is not None and out.startswith("#7 merged")
    assert [n for n, _, _ in env.gh.merges] == [7]


def test_fleet_reconcile_flips_the_filter_on_a_running_merger(redis_client):
    """Republishing the workflow reaches the running Merger on the next reconcile: the same
    Merger object changes behaviour and the status payload follows, with no restart."""
    from tests.test_merger_fleet import _noop_build_fn

    base = WORKFLOW.replace("---\nbody", "kubernetes:\n  default_repo: acme/app\n{extra}---\nbody")
    on = parse_workflow(base.format(extra=""))
    off = parse_workflow(base.format(extra="merger:\n  label_filter:\n    enabled: false\n"))
    fleet = MergerFleet(client=redis_client, ns=NS, cfg=on, env={}, stop=threading.Event(),
                        build_fn=_noop_build_fn(redis_client),
                        review_semaphore=threading.Semaphore(1), reviewers=0, poll_s=0.1)
    try:
        fleet.reconcile(on)
        m = fleet._repos["acme/app"]["merger"]
        unlabeled = {"number": 7, "labels": [], "draft": False, "base": m.main,
                     "head_sha": "abc", "ready_at": "2026-11-01"}
        assert m.store.snapshot()["label_filter"] == {"active": True,
                                                       "labels": ["symphony", "symphony-learnings"]}
        assert eligible(m, unlabeled, {}, {}, {}, "aaa") is False

        fleet.reconcile(off)
        assert fleet._repos["acme/app"]["merger"] is m  # the same Merger, not rebuilt
        assert m.store.snapshot()["label_filter"] == {"active": False, "labels": []}
        assert eligible(m, unlabeled, {}, {}, {}, "aaa") is True
    finally:
        fleet.stop_all()


# -- live toggle -------------------------------------------------------------

def test_live_toggle_between_picks(env):
    """Flipping the config between two picks without recreating the Merger changes behaviour."""
    open_pr(env, labels=())

    # Filter on → unlabeled PR not picked
    env.m.label_filter_active = True
    env.m.label_filter_labels = ("symphony",)
    result = env.worker.step()
    assert result is None

    # Filter off → same PR is now picked
    env.m.label_filter_active = False
    setup_simple_review(env)
    result = env.worker.step()
    assert result is not None and "#7" in result


# -- config parsing ----------------------------------------------------------

def test_config_defaults_no_merger_section():
    """Without a merger: section, the defaults apply: filter on, default labels."""
    cfg = parse_workflow(WORKFLOW)
    assert cfg.merger_label_filter_enabled is True
    assert cfg.merger_label_filter_labels == ("symphony", "symphony-learnings")
    assert cfg.merger_label_filter_repos == {}


def test_config_explicit_enabled_false():
    cfg = parse_workflow(WORKFLOW.replace(
        "---\nbody",
        "merger:\n  label_filter:\n    enabled: false\n---\nbody"
    ))
    assert cfg.merger_label_filter_enabled is False


def test_config_custom_labels():
    cfg = parse_workflow(WORKFLOW.replace(
        "---\nbody",
        "merger:\n  label_filter:\n    labels:\n      - my-label\n      - other\n---\nbody"
    ))
    assert cfg.merger_label_filter_labels == ("my-label", "other")


def test_config_repo_override_lowercased():
    cfg = parse_workflow(WORKFLOW.replace(
        "---\nbody",
        "merger:\n  label_filter:\n    repos:\n      Acme/App: true\n---\nbody"
    ))
    assert cfg.merger_label_filter_repos == {"acme/app": True}


def test_config_rejects_empty_labels_when_enabled():
    with pytest.raises(ConfigError, match="nonempty"):
        parse_workflow(WORKFLOW.replace(
            "---\nbody",
            "merger:\n  label_filter:\n    enabled: true\n    labels: []\n---\nbody"
        ))


def test_config_empty_labels_are_allowed_only_while_no_repository_filters():
    off = "merger:\n  label_filter:\n    enabled: false\n    labels: []\n{repos}---\nbody"
    cfg = parse_workflow(WORKFLOW.replace("---\nbody", off.format(repos="")))
    assert cfg.merger_label_filter_labels == ()
    with pytest.raises(ConfigError, match="nonempty"):
        parse_workflow(WORKFLOW.replace(
            "---\nbody", off.format(repos="    repos:\n      acme/app: true\n")))


def test_config_rejects_bad_repo_key():
    with pytest.raises(ConfigError, match="owner/name"):
        parse_workflow(WORKFLOW.replace(
            "---\nbody",
            "merger:\n  label_filter:\n    repos:\n      not-a-repo: true\n---\nbody"
        ))


def test_config_rejects_non_boolean_repo_value():
    with pytest.raises(ConfigError, match="boolean"):
        parse_workflow(WORKFLOW.replace(
            "---\nbody",
            "merger:\n  label_filter:\n    repos:\n      acme/app: \"on\"\n---\nbody"
        ))


def test_config_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown"):
        parse_workflow(WORKFLOW.replace(
            "---\nbody",
            "merger:\n  label_filter:\n    enabled: true\n    bogus: 1\n---\nbody"
        ))


# -- workflow API validation -------------------------------------------------

def _workflow_repository():
    from tests.test_admin_workflow import ConfigMaps

    from symphony.admin_workflow import WorkflowRepository

    core = ConfigMaps()
    return core, WorkflowRepository(core, "symphony")


def test_workflow_api_publishes_the_three_keys_and_reads_them_back():
    from tests.test_admin_workflow import reviewed

    core, repository = _workflow_repository()
    values = repository.read()["values"]
    assert values["merger.label_filter.enabled"] is True  # on by default
    assert json.loads(values["merger.label_filter.labels"]) == ["symphony", "symphony-learnings"]
    assert json.loads(values["merger.label_filter.repos"]) == {}

    patch = {
        "merger.label_filter.enabled": False,
        "merger.label_filter.labels": json.dumps(["symphony", "ready-for-review"]),
        "merger.label_filter.repos": json.dumps({"Acme/App": True}),
    }
    repository.publish(reviewed(repository, patch), "command-filter", before_write=lambda: None)
    cfg = parse_workflow(core.current.data["WORKFLOW.md"])
    assert cfg.merger_label_filter_enabled is False
    assert cfg.merger_label_filter_labels == ("symphony", "ready-for-review")
    assert cfg.merger_label_filter_repos == {"acme/app": True}
    values = repository.read()["values"]
    assert values["merger.label_filter.enabled"] is False
    assert json.loads(values["merger.label_filter.labels"]) == ["symphony", "ready-for-review"]
    assert json.loads(values["merger.label_filter.repos"]) == {"acme/app": True}


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"merger.label_filter.labels": "[]"}, "nonempty"),
        ({"merger.label_filter.labels": '["symphony", ""]'}, "nonempty string"),
        ({"merger.label_filter.repos": '{"not-a-repo": true}'}, "owner/name"),
        ({"merger.label_filter.repos": '{"acme/app": "yes"}'}, "true or false"),
        ({"merger.label_filter.enabled": "false"}, "boolean"),
    ],
)
def test_workflow_api_rejects_invalid_filter_values_with_a_diagnostic(patch, message):
    from symphony.admin_workflow import WorkflowError

    core, repository = _workflow_repository()
    with pytest.raises(WorkflowError) as error:
        repository.validate(repository.read()["revision"], patch)
    assert error.value.status_code == 422
    (diagnostic,) = error.value.diagnostics
    assert diagnostic["key"] == next(iter(patch))
    assert message in diagnostic["message"]
    assert core.writes == 0


# -- status payload ----------------------------------------------------------

def test_snapshot_includes_label_filter(redis_client):
    store = MergerStore(redis_client, NS, "acme/app")
    store.label_filter = {"active": True, "labels": ["symphony"]}
    snap = store.snapshot()
    assert snap["label_filter"] == {"active": True, "labels": ["symphony"]}


def test_snapshot_omits_label_filter_when_not_set(redis_client):
    store = MergerStore(redis_client, NS, "acme/app")
    snap = store.snapshot()
    assert "label_filter" not in snap


# -- eligible() unit tests ---------------------------------------------------

def _fake_pr(number=7, labels=(), draft=False, base="main", head_sha="abc123", ready_at="2026-11-01"):
    return {"number": number, "labels": list(labels), "draft": draft, "base": base,
            "head_sha": head_sha, "ready_at": ready_at}


def _fake_merger(*, label_filter_active=True, label_filter_labels=("symphony",), main="main"):
    m = SimpleNamespace(
        main=main,
        label_filter_active=label_filter_active,
        label_filter_labels=label_filter_labels,
        clock=lambda: 0,
    )
    return m


def test_eligible_filter_on_rejects_unlabeled():
    m = _fake_merger(label_filter_active=True)
    p = _fake_pr(labels=())
    assert eligible(m, p, {}, {}, {}, "aaa") is False


def test_eligible_filter_on_accepts_labeled():
    m = _fake_merger(label_filter_active=True)
    p = _fake_pr(labels=("symphony",))
    assert eligible(m, p, {}, {}, {}, "aaa") is True


def test_eligible_filter_on_accepts_bypass():
    m = _fake_merger(label_filter_active=True)
    p = _fake_pr(labels=(cm.BYPASS,))
    assert eligible(m, p, {}, {}, {}, "aaa") is True


def test_eligible_filter_off_accepts_unlabeled():
    m = _fake_merger(label_filter_active=False)
    p = _fake_pr(labels=())
    assert eligible(m, p, {}, {}, {}, "aaa") is True
