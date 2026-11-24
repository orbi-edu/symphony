"""A failed runner Job's evidence says when the cluster removed its pod, and why."""

import datetime as dt
from types import SimpleNamespace as NS

from tests.test_dispatch import cfg

from symphony import effects

FAILED = NS(type="Failed", status="True", reason="BackoffLimitExceeded",
            message="Job has reached the specified backoff limit")
LOG = "error='Agent generation termination could not be confirmed' event='runner_crashed'\n"


class Batch:
    def read_namespaced_job(self, name, ns, **_):
        return NS(status=NS(conditions=[FAILED]))


class Core:
    def __init__(self, pods):
        self.pods = pods

    def list_namespaced_pod(self, ns, label_selector, **_):
        assert label_selector == "job-name=sym-run-eng-12"
        return NS(items=self.pods)

    def read_namespaced_pod_log(self, name, ns, **_):
        return LOG


def runner_pod(state, *, deleted=False, phase="Running", reason=None, message=None,
               conditions=(), node="worker-a"):
    return NS(
        metadata=NS(name="sym-run-eng-12-x", creation_timestamp=dt.datetime(2026, 1, 1),
                    deletion_timestamp=dt.datetime(2026, 1, 1, 0, 5) if deleted else None),
        spec=NS(node_name=node),
        status=NS(phase=phase, reason=reason, message=message, conditions=list(conditions),
                  container_statuses=[NS(name="runner", state=state)]),
    )


def running():
    return NS(running=NS(started_at=None), waiting=None, terminated=None)


def evidence(pods):
    return effects.K8sEffects(cfg(), Core(pods), Batch(), "test").failure_evidence("sym-run-eng-12")


def test_a_pod_deleted_while_running_names_the_deletion_the_cause_and_the_node():
    target = NS(type="DisruptionTarget", status="True", reason="EvictionByEvictionAPI",
                message="Eviction API: evicting")
    out = evidence([runner_pod(running(), deleted=True, conditions=[NS(type="Ready", status="True"), target])])
    assert out["pod"] == ("deleted while running; DisruptionTarget: EvictionByEvictionAPI: "
                          "Eviction API: evicting; node worker-a")
    assert out["disruption"] == "EvictionByEvictionAPI"
    assert out["container"] == "running"
    assert out["reason"].startswith("BackoffLimitExceeded")


def test_a_kubelet_eviction_names_the_status_reason():
    out = evidence([runner_pod(NS(running=None, waiting=None, terminated=None), phase="Failed",
                               reason="Evicted", message="The node was low on resource: memory.")])
    assert out["pod"] == "Evicted: The node was low on resource: memory.; node worker-a"
    assert out["disruption"] == "Evicted"


def test_a_waiting_container_records_its_reason():
    out = evidence([runner_pod(NS(running=None, waiting=NS(reason="ContainerCreating"), terminated=None),
                               deleted=True, phase="Pending")])
    assert out["container"] == "waiting: ContainerCreating"
    assert out["pod"] == "deleted while pending; node worker-a"
    assert out["disruption"] == "deleted"


def test_a_job_without_a_pod_says_the_pod_is_missing():
    out = evidence([])
    assert out["pod"] == "missing (deleted, or its node is gone)"
    assert out["disruption"] == "missing"


def test_a_container_that_ended_on_its_own_keeps_its_evidence():
    ended = NS(running=None, waiting=None, terminated=NS(reason="Error", exit_code=1))
    out = evidence([runner_pod(ended, phase="Failed")])
    assert out == {
        "job": "sym-run-eng-12",
        "reason": "BackoffLimitExceeded: Job has reached the specified backoff limit",
        "container": "Error (exit 1)",
        "error": "Agent generation termination could not be confirmed",
        "lines": [LOG.strip()],
    }
