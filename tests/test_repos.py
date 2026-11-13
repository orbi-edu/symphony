"""Repository designation resolution: one or several `repo:owner/name` labels.

Pure: string rules over labels the adapter already fetches. No tracker, no network.
"""

import pytest

from symphony.repos import (
    MALFORMED,
    MISSING,
    MIXED_OWNERS,
    NOT_AUTHORIZED,
    resolve_target_repos,
)


def test_single_valid_designation_resolves():
    repos, reason = resolve_target_repos(("symphony", "repo:acme/backend"), default=None)
    assert repos == ("acme/backend",)
    assert reason is None


def test_no_designation_uses_the_configured_default():
    repos, reason = resolve_target_repos(("symphony",), default="acme/mono")
    assert repos == ("acme/mono",)
    assert reason is None


def test_no_designation_and_no_default_is_held_back():
    repos, reason = resolve_target_repos(("symphony",), default=None)
    assert repos == ()
    assert reason == MISSING


def test_the_issues_own_designation_beats_the_default():
    """A default is a convenience for the common case, never an override."""
    repos, reason = resolve_target_repos(("repo:acme/api",), default="acme/mono")
    assert repos == ("acme/api",)
    assert reason is None


@pytest.mark.parametrize(
    "label",
    [
        "repo:",
        "repo:owner",
        "repo:owner/",
        "repo:/name",
        "repo:a/b/c",
        "repo:own er/name",
        "repo:owner/na me",
        "repo:owner/na$me",
    ],
)
def test_malformed_designations_are_rejected(label):
    repos, reason = resolve_target_repos((label,), default="acme/mono")
    assert repos == (), f"{label!r} must not resolve, and must not silently fall back"
    assert reason == MALFORMED


def test_one_malformed_label_among_valid_ones_holds_the_whole_ticket():
    """Dropping the typo and proceeding with the rest would work the ticket without a
    repository somebody named."""
    repos, reason = resolve_target_repos(("repo:acme/api", "repo:oops"), default=None)
    assert repos == () and reason == MALFORMED


def test_several_designations_resolve_in_label_order_primary_first():
    repos, reason = resolve_target_repos(
        ("symphony", "repo:acme/api", "repo:acme/web", "repo:acme/docs"), default=None
    )
    assert reason is None
    assert repos == ("acme/api", "acme/web", "acme/docs")


def test_the_default_is_the_primary_when_it_is_among_the_designations():
    """The board's main repository anchors the workspace when a ticket also touches it."""
    repos, _ = resolve_target_repos(("repo:acme/web", "repo:acme/mono"), default="acme/mono")
    assert repos == ("acme/mono", "acme/web")


def test_the_default_is_not_added_when_the_ticket_names_other_repositories():
    repos, _ = resolve_target_repos(("repo:acme/web", "repo:acme/api"), default="acme/mono")
    assert repos == ("acme/web", "acme/api")


def test_designations_across_owners_are_held_back():
    """One App installation belongs to one account: no credential could cover both."""
    repos, reason = resolve_target_repos(("repo:acme/api", "repo:other/web"), default=None)
    assert repos == () and reason == MIXED_OWNERS


def test_duplicate_and_case_variant_designations_collapse():
    """Labels are lower-cased by the normalization, and the code host treats owner/name
    case-insensitively. A case difference must not read as a second repository."""
    repos, reason = resolve_target_repos(
        ("repo:Acme/Backend", "repo:acme/backend", "repo:acme/backend"), default=None
    )
    assert repos == ("acme/backend",)
    assert reason is None


def test_reasons_are_four_distinct_values():
    """Each demands a different human action — add a label, fix a typo, split the ticket
    by owner, install the application."""
    assert len({MISSING, MALFORMED, MIXED_OWNERS, NOT_AUTHORIZED}) == 4


def test_unrelated_labels_are_ignored():
    repos, reason = resolve_target_repos(("symphony", "bug", "p1", "repo:acme/x"), default=None)
    assert repos == ("acme/x",) and reason is None


def test_correcting_the_designation_restores_resolution():
    """Recomputed on every read, nothing cached, no state to clear."""
    assert resolve_target_repos(("repo:oops",), default=None)[1] == MALFORMED
    assert resolve_target_repos(("repo:acme/fixed",), default=None) == (("acme/fixed",), None)


# -- observability gate ----------------------------------------------------
# `_ObservedTracker` is the only record that an issue was seen and refused, so it has to
# fire for real candidates — and stay silent for everything else. On a team-scoped board
# the "everything else" is the whole team's backlog, once every polling interval.


def test_routing_warnings_respect_the_dispatch_gate():
    from symphony.edge import ObservedTracker
    from symphony.trackers.base import Issue

    def issue(identifier, labels):
        return Issue(
            id=identifier,
            identifier=identifier,
            title="t",
            description="",
            state="Todo",
            state_type="active",
            priority=None,
            created_at=None,
            updated_at=None,
            labels=labels,
            dispatchable=False,
            undispatchable_reason="no-repo-designation",
        )

    class Inner:
        def fetch_issues_by_states(self, states):
            return [issue("ENG-900", ()), issue("ENG-901", ("symphony",))]

    seen = []
    import symphony.edge as edge_mod

    original = edge_mod.log
    edge_mod.log = type("L", (), {"warning": lambda _s, _e, **kw: seen.append(kw["identifier"])})()
    try:
        ObservedTracker(Inner(), ("symphony",)).fetch_issues_by_states(["Todo"])
    finally:
        edge_mod.log = original

    assert seen == ["ENG-901"], "only the labelled issue was ever a candidate"


def test_runtime_class_reaches_every_agent_pod_and_no_orchestrator_job(monkeypatch):
    """The isolation upgrade path (SPEC D6). Three properties, each a silent failure if
    lost: the workspace pod carries the class (or the upgrade never happened); the
    distill pod carries it too (or retrospectives silently run outside the boundary the
    operator chose); orchestrator Jobs never carry it (they are trusted, and a Sysbox/
    Kata scheduling constraint on them would strand dispatches on runtime nodes)."""
    from symphony.config import Config, parse_workflow
    from symphony.entry.reflect import _distill_pod_manifest
    from symphony.kube import _job_manifest, _workspace_manifest

    cfg = Config(tracker_kind="memory", runtime_class="sysbox-runc")
    assert _workspace_manifest(cfg, "ws-x", "img")["spec"]["runtimeClassName"] == "sysbox-runc"
    assert _distill_pod_manifest(cfg, "d-x")["spec"]["runtimeClassName"] == "sysbox-runc"

    plain = Config(tracker_kind="memory")
    assert "runtimeClassName" not in _workspace_manifest(plain, "ws-x", "img")["spec"]
    assert "runtimeClassName" not in _distill_pod_manifest(plain, "d-x")["spec"]

    monkeypatch.setenv("SYMPHONY_ORCHESTRATOR_IMAGE", "orch:test")
    job = _job_manifest(cfg, "j-x", "symphony.entry.run_issue", {}, 60, "runner")
    assert "runtimeClassName" not in str(job)

    parsed = parse_workflow(
        "---\ntracker: {kind: memory}\nkubernetes: {runtime_class: sysbox-runc}\n---\nx"
    )
    assert parsed.runtime_class == "sysbox-runc"


def test_sysbox_posture_runs_docker_without_privileged():
    """The point of the Sysbox path: privileged stays OFF and the pod still gets the
    /var/lib/docker mount dockerd needs (nested overlayfs falls back to vfs otherwise —
    an explicit emptyDir also keeps the lifecycle in the kubelet's hands instead of
    Sysbox's implicit host mount)."""
    from symphony.config import Config
    from symphony.kube import _workspace_manifest

    manifest = _workspace_manifest(
        Config(tracker_kind="memory", runtime_class="sysbox-runc", privileged_agents=False),
        "ws-x",
        "img",
    )
    container = manifest["spec"]["containers"][0]
    assert container["securityContext"]["privileged"] is False
    assert any(m["mountPath"] == "/var/lib/docker" for m in container["volumeMounts"])
    assert any(v["name"] == "docker-lib" and "emptyDir" in v for v in manifest["spec"]["volumes"])

    # no docker posture at all: no stray mount
    bare = _workspace_manifest(Config(tracker_kind="memory"), "ws-x", "img")
    assert not any(
        m["mountPath"] == "/var/lib/docker" for m in bare["spec"]["containers"][0]["volumeMounts"]
    )


def test_privileged_agents_reach_the_pod_manifest():
    """The K8s successor of the Modal port's `vm_runtime` guard. dockerd inside the
    workspace needs a privileged container; dropping the flag does not break a test or
    raise an error — the agent simply loses the ability to validate anything it
    containerises, which is the failure the completion bar's "validated by you with
    tests" is supposed to prevent. The flag is an operator decision (WORKFLOW.md
    kubernetes.privileged_agents) and must reach the manifest verbatim, both ways."""
    from symphony.config import Config
    from symphony.kube import _workspace_manifest

    on = _workspace_manifest(Config(tracker_kind="memory", privileged_agents=True), "ws-x", "img")
    off = _workspace_manifest(Config(tracker_kind="memory"), "ws-x", "img")
    assert on["spec"]["containers"][0]["securityContext"]["privileged"] is True
    assert off["spec"]["containers"][0]["securityContext"]["privileged"] is False


def test_a_restricted_egress_allowlist_still_admits_a_registry():
    """Guards the incoherent middle, not the policy.

    Empty means unrestricted, which is a decision an operator may take. Non-empty means
    deny-by-default (enforced by the cluster's CNI — see SPEC deviations), and then a
    missing registry breaks every container the agent runs — with an error naming a CDN
    host rather than the pull that needed it. What must never ship is a list that
    restricts egress while omitting the hosts the sandbox cannot work without; either
    end of the spectrum is fine, the middle is a silent outage.
    """
    from symphony.config import parse_workflow

    cfg = parse_workflow(open("WORKFLOW.md", encoding="utf-8").read())
    if not cfg.egress_allowlist:
        return  # unrestricted by choice — see the comment in WORKFLOW.md
    hosts = " ".join(cfg.egress_allowlist)
    assert "docker.io" in hosts, "restricted egress with no registry: `docker pull` always fails"
    assert any("svc" in h or "cluster.local" in h for h in cfg.egress_allowlist), (
        "restricted egress without the in-cluster services: the agent cannot move the ticket"
    )
