"""Fleet merger: one process serves every repository with a merger policy.

Tests: discovery merges env + config + Redis; the fleet starts/stops per-repo threads;
a build failure skips and retries; the process-wide semaphore limits concurrent reviews."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock

from symphony.discovery import DISCOVERY_KEY
from symphony.entry.merger import (
    DEFAULT_CONCURRENCY,
    MergerFleet,
    _discovery_repos,
    _env_repos,
    _repo_workdir,
    discover_repos,
)
from symphony.merger.store import MergerStore

# -- helpers -----------------------------------------------------------------

NS = "fleet"
WORKFLOW = "---\ntracker:\n  kind: memory\n---\nbody\n"


@dataclass
class FakeCfg:
    """Minimal config stand-in for discovery tests."""
    default_repo: str | None = None


@dataclass
class FakeMerger:
    """Minimal Merger-like object for fleet tests."""
    repo: str
    store: MergerStore | None = None
    _policy: object = None
    wake: object = field(default_factory=lambda: (lambda reason: None))
    pause: object = field(default_factory=lambda: time.sleep)
    clock: object = field(default_factory=lambda: (lambda: int(time.time() * 1000)))

    def policy(self):
        return self._policy


# -- _repo_workdir -----------------------------------------------------------

def test_default_repo_gets_flat_workdir():
    assert _repo_workdir("/data", "acme/app", "acme/app") == "/data"


def test_default_repo_comparison_is_case_insensitive():
    assert _repo_workdir("/data", "Acme/App", "acme/app") == "/data"


def test_non_default_repo_gets_nested_workdir():
    assert _repo_workdir("/data", "acme/lib", "acme/app") == "/data/repos/acme__lib"


def test_no_default_repo_all_get_nested():
    assert _repo_workdir("/data", "acme/app", None) == "/data/repos/acme__app"


# -- _env_repos --------------------------------------------------------------

def test_env_repos_parses_comma_separated():
    assert _env_repos({"SYMPHONY_MERGER_REPO": "acme/app, acme/lib"}) == ["acme/app", "acme/lib"]


def test_env_repos_ignores_invalid():
    assert _env_repos({"SYMPHONY_MERGER_REPO": "acme/app,bad,,acme/lib"}) == ["acme/app", "acme/lib"]


def test_env_repos_empty():
    assert _env_repos({}) == []


# -- _discovery_repos --------------------------------------------------------

def test_discovery_repos_from_redis(redis_client):
    doc = {"items": [
        {"name": "acme/app", "merger_policy": True, "archived": False},
        {"name": "acme/lib", "merger_policy": False, "archived": False},
        {"name": "acme/old", "merger_policy": True, "archived": True},
        {"name": "acme/svc", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc))
    repos = _discovery_repos(redis_client, NS)
    assert sorted(repos) == ["acme/app", "acme/svc"]


def test_discovery_repos_handles_missing_key(redis_client):
    assert _discovery_repos(redis_client, NS) == []


def test_discovery_repos_handles_bad_json(redis_client):
    redis_client.set(DISCOVERY_KEY, "not json")
    assert _discovery_repos(redis_client, NS) == []


# -- discover_repos ----------------------------------------------------------

def test_discover_repos_union(redis_client):
    doc = {"items": [
        {"name": "acme/disc", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc))
    env = {"SYMPHONY_MERGER_REPO": "acme/env"}
    cfg = FakeCfg(default_repo="acme/default")
    repos = discover_repos(cfg, env, redis_client, NS)
    assert repos == {"acme/env", "acme/default", "acme/disc"}


def test_discover_repos_deduplicates(redis_client):
    doc = {"items": [
        {"name": "acme/app", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc))
    env = {"SYMPHONY_MERGER_REPO": "acme/app"}
    cfg = FakeCfg(default_repo="acme/app")
    repos = discover_repos(cfg, env, redis_client, NS)
    assert repos == {"acme/app"}


# -- MergerFleet: start/stop/reconcile ----------------------------------------

def _make_fleet(redis_client, *, cfg=None, env=None, build_fn=None, reviewers=1,
                concurrency=DEFAULT_CONCURRENCY, stop=None, poll_s=0.1):
    cfg = cfg or FakeCfg(default_repo="acme/app")
    env = env or {}
    stop = stop or threading.Event()
    sem = threading.Semaphore(concurrency)
    if build_fn is None:
        build_fn = _noop_build_fn(redis_client)
    return MergerFleet(
        client=redis_client, ns=NS, cfg=cfg, env=env, stop=stop,
        build_fn=build_fn, review_semaphore=sem, reviewers=reviewers, poll_s=poll_s,
    )


def _noop_build_fn(redis_client):
    """A build function that returns a fake Merger whose store is real (for registration)."""
    def _build(*, repo, pulse):
        store = MergerStore(redis_client, NS, repo)
        m = MagicMock()
        m.repo = repo
        m.store = store
        m.policy.return_value = MagicMock()
        m.git = MagicMock()
        m.git.main = "main"
        m.gh = MagicMock()
        m.agents = MagicMock()
        m.team = MagicMock()
        m.clock.return_value = int(time.time() * 1000)
        m.wake = pulse.fire
        m.pause = pulse.wait
        m.config.return_value = {}
        m.main = "main"
        return m
    return _build


def test_fleet_starts_mergers_for_discovered_repos(redis_client):
    """The fleet starts mergers for each repo from env + config + discovery."""
    doc = {"items": [
        {"name": "acme/disc", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc))
    env = {"SYMPHONY_MERGER_REPO": "acme/env"}
    cfg = FakeCfg(default_repo="acme/default")

    fleet = _make_fleet(redis_client, cfg=cfg, env=env)
    fleet.reconcile(cfg)

    running = fleet.running_repos()
    assert running == {"acme/env", "acme/default", "acme/disc"}

    # Each repo registered in Redis
    for repo in running:
        assert MergerStore(redis_client, NS, repo).alive()

    fleet.stop_all()
    assert fleet.running_repos() == set()


def test_fleet_stops_removed_repos(redis_client):
    """When a repo disappears from discovery, its threads are stopped."""
    doc = {"items": [
        {"name": "acme/a", "merger_policy": True, "archived": False},
        {"name": "acme/b", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc))
    cfg = FakeCfg()
    fleet = _make_fleet(redis_client, cfg=cfg)
    fleet.reconcile(cfg)
    assert fleet.running_repos() == {"acme/a", "acme/b"}

    # Remove acme/b from discovery
    doc2 = {"items": [
        {"name": "acme/a", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc2))
    fleet.reconcile(cfg)
    assert fleet.running_repos() == {"acme/a"}

    fleet.stop_all()


def test_fleet_skips_failed_build_and_retries(redis_client):
    """A repo whose build raises is skipped (not unhealthy) and retried on the next reconcile."""
    fail_repos: set[str] = {"acme/fail"}

    def build_fn(*, repo, pulse):
        if repo in fail_repos:
            from symphony.merger.git import GitError
            raise GitError(["clone"], 128, "not found")
        return _noop_build_fn(redis_client)(repo=repo, pulse=pulse)

    doc = {"items": [
        {"name": "acme/fail", "merger_policy": True, "archived": False},
        {"name": "acme/ok", "merger_policy": True, "archived": False},
    ]}
    redis_client.set(DISCOVERY_KEY, json.dumps(doc))
    cfg = FakeCfg()
    fleet = _make_fleet(redis_client, cfg=cfg, build_fn=build_fn)

    fleet.reconcile(cfg)
    assert fleet.running_repos() == {"acme/ok"}
    assert fleet.healthy()  # failed build doesn't make unhealthy
    assert "acme/fail" in fleet._failed

    # Fix the build
    fail_repos.clear()
    fleet.reconcile(cfg)
    assert fleet.running_repos() == {"acme/ok", "acme/fail"}
    assert "acme/fail" not in fleet._failed

    fleet.stop_all()


def test_fleet_health_true_when_no_repos(redis_client):
    fleet = _make_fleet(redis_client, cfg=FakeCfg())
    assert fleet.healthy()


# -- concurrency semaphore ---------------------------------------------------


def test_semaphore_limits_concurrent_reviews_with_events(redis_client):
    """At most N concurrent review steps run across repos.  Uses events to precisely
    control worker execution and verify the concurrency bound."""
    max_concurrent = 2
    sem = threading.Semaphore(max_concurrent)
    num_workers = 4

    # Each worker signals when it starts its step and waits on a gate to finish
    started = [threading.Event() for _ in range(num_workers)]
    gate = threading.Event()  # opened to let all workers complete
    counter = {"current": 0, "max": 0}
    lock = threading.Lock()

    def worker_fn(idx):
        sem.acquire()
        try:
            with lock:
                counter["current"] += 1
                counter["max"] = max(counter["max"], counter["current"])
            started[idx].set()
            gate.wait(timeout=5)
            with lock:
                counter["current"] -= 1
        finally:
            sem.release()

    threads = [threading.Thread(target=worker_fn, args=(i,), daemon=True) for i in range(num_workers)]
    for t in threads:
        t.start()

    # Wait a bit for threads to try acquiring the semaphore
    time.sleep(0.5)

    # Only max_concurrent should have started
    num_started = sum(1 for e in started if e.is_set())
    assert num_started == max_concurrent
    with lock:
        assert counter["max"] == max_concurrent
        assert counter["current"] == max_concurrent

    # Open the gate, let all workers proceed
    gate.set()
    for t in threads:
        t.join(timeout=2)

    # Now all 4 should have run, but max concurrent was 2
    assert all(e.is_set() for e in started)
    with lock:
        assert counter["max"] == max_concurrent


# -- workdir layout ----------------------------------------------------------

def test_repo_workdir_default():
    """The default repo uses the flat workdir; others get nested dirs."""
    base = "/var/lib/symphony/merger"
    assert _repo_workdir(base, "acme/app", "acme/app") == base
    assert _repo_workdir(base, "acme/lib", "acme/app") == f"{base}/repos/acme__lib"
    assert _repo_workdir(base, "org/svc", "acme/app") == f"{base}/repos/org__svc"
