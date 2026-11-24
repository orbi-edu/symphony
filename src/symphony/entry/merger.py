"""symphony-merger's entrypoint: one process serves every repository with a merger policy.

A reconcile loop (every ~60 s and on start) discovers repositories from three sources:
`SYMPHONY_MERGER_REPO` (comma-separated), `cfg.default_repo`, and every non-archived repository
the discovery document marks `merger_policy: true`.  For each discovered repository it starts
a full set of loops (coordinator/wake, train, main-watch, N review workers) through the
existing `start_loops`.  Repositories that disappear from discovery get their loops stopped
and threads joined.

Concurrency: up to `SYMPHONY_MERGER_CONCURRENCY` (default 8) agent review steps run at once
across all repositories — a process-wide semaphore around the expensive part of a review
(the `worker.step()` call).  Idle polling never holds a slot because `pick()` returns None
before any agent work, and the semaphore is acquired only when `step()` is entered and
released immediately on return.  The train and main-watch of each repository are not limited.

Workdir: the first/default repository keeps the existing layout (`<workdir>/mirror`, `wt`,
`sessions`) so an existing mirror is not re-cloned.  Every other repository lives under
`<workdir>/repos/<owner>__<name>/`.

Health: `/healthz` stays healthy while every running repository's coordinator beats and its
threads live.  A repository whose build fails does not make the pod unhealthy — it is
skipped and retried on the next reconcile.

Env: REDIS_URL, POD_NAMESPACE, the tracker's credential (`LINEAR_API_KEY` for Linear),
GITHUB_APP_ID / _PRIVATE_KEY / _INSTALLATION_ID,
the `model-provider` Secret's env (agents whose role has no managed provider use it; the others
get their key from `symphony-providers`, read at every agent run),
SYMPHONY_MERGER_WORKDIR (mirror, worktrees, omp sessions), SYMPHONY_MERGER_REPO (owner/name,
comma-separated; empty: WORKFLOW.md's default repo), SYMPHONY_MERGER_REVIEWERS (default 2,
per repository), SYMPHONY_MERGER_CONCURRENCY (default 8, process-wide review semaphore),
SYMPHONY_OMP_BIN (default omp), SYMPHONY_HEALTH_PORT (default 8081, GET /healthz and
GET /metrics).

Until setup gives it a valid WORKFLOW and the GitHub App credential, the process idles
(`configure`): healthy, no heartbeat, `merger_not_configured` once a minute.  'No repository'
is no longer fatal when discovery will provide some.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable

import redis

from .. import kube, metrics
from .. import wake as wakes
from ..config import ConfigError, ConfigHolder
from ..discovery import DISCOVERY_KEY
from ..edge import gateway_url, load_config, log, make_tracker, now_ms, pod_namespace
from ..endpoints import github_server_url
from ..ghapp import CredentialError, InstallationCredential, NotAuthorizedForRepo
from ..mcp_servers import McpLaunch, McpRepository
from ..merger.agents import AgentRunner
from ..merger.context import Merger
from ..merger.git import GitError, Mirror
from ..merger.github import GitHubError, RestGitHub
from ..merger.mainwatch import watch_once
from ..merger.review import ProbeFailed, ReviewWorker
from ..merger.store import HEARTBEAT_TTL_MS, INSTANCE, MergerStore, normalize_repo, wake_target
from ..merger.train import Train, TrainBusy
from ..providers import ProviderRepository
from ..sessions import SessionStore
from ..teamconfig import TeamConfig

POLL_S = 30
STALE_MS = 90_000
PROBE_BACKOFF_S = (60, 120, 300, 600)
GITHUB_APP_ENV = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")
NOT_CONFIGURED_POLL_S = 15
NOT_CONFIGURED_LOG_S = 60
RECONCILE_INTERVAL_S = 60
DEFAULT_CONCURRENCY = 8


class NotConfigured(Exception):
    """Setup is incomplete: `missing` names what (never a value)."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(", ".join(missing))
        self.missing = missing


class Pulse:
    """Wakes every waiting loop at once; `wait` returns early on the next fire."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._gen = 0

    def fire(self, reason: str = "") -> None:
        with self._cond:
            self._gen += 1
            self._cond.notify_all()

    def wait(self, seconds: float) -> None:
        with self._cond:
            gen = self._gen
            self._cond.wait_for(lambda: self._gen != gen, timeout=seconds)


def _repo_workdir(base_workdir: str, repo: str, default_repo: str | None) -> str:
    """Workdir for a repository.  The first/default repository keeps the existing flat layout
    so an existing mirror is not re-cloned.  Others live under repos/<owner>__<name>/."""
    if default_repo and repo.lower() == default_repo.lower():
        return base_workdir
    owner, name = repo.lower().split("/", 1)
    return os.path.join(base_workdir, "repos", f"{owner}__{name}")


def build(client, ns: str, cfg, env=os.environ, pulse: Pulse | None = None, *,
          repo: str | None = None,
          token: Callable[[], str] | None = None, notify_post=None, tracker=None,
          providers: Callable[[str, str], object | None] | None = None,
          mcp: McpLaunch | None = None,
          workdir: str | None = None) -> Merger:
    """The merger for one repository. `token` defaults to the GitHub App installation token
    (scoped to that repository); `notify_post` to `symphony.notify.post`; `tracker` to
    WORKFLOW.md's tracker (none while its credential is missing); `providers` resolves each
    agent role's credential (internals §13), None leaving the pod's env to every agent; `mcp`
    stages each agent role's MCP servers (None: no MCP servers)."""
    if repo is None:
        repo = env.get("SYMPHONY_MERGER_REPO") or cfg.default_repo
    if not repo:
        raise NotConfigured(["repository"])
    base_workdir = env.get("SYMPHONY_MERGER_WORKDIR", "/var/lib/symphony/merger")
    if workdir is None:
        workdir = _repo_workdir(base_workdir, repo, cfg.default_repo)
    if token is None:
        token = InstallationCredential.from_env(env, now_ms, target_repos=(repo,)).token
    if notify_post is None:
        from .. import notify

        notify_post = notify.post
    if tracker is None:
        try:
            tracker = make_tracker(cfg)
        except ConfigError as exc:  # the merger still merges; tickets just don't move
            log.warning("merger_tracker_unavailable", error=str(exc))
    pulse = pulse or Pulse()
    git = Mirror(workdir, f"{github_server_url()}/{repo}.git", main=cfg.base_branch(repo), token=token)
    git.ensure()
    git.fetch()
    # Build a skill stager that resolves and stages central skills per agent run.
    skill_stager = None
    if cfg.skill_sources:
        from ..skills import deduplicate, resolve_sources, skills_meta, stage_skills_local
        sessions_for_skills = SessionStore(client, ns)

        def _skill_stager(agent_dir: str, role: str, meta: dict) -> None:
            if not agent_dir or not cfg.skill_sources:
                return
            resolved = resolve_sources(
                cfg.skill_sources, role, repo,
                env, now_ms,
                cache_client=client, cache_ns=ns,
            )
            skills = deduplicate(resolved, repo)
            stage_skills_local(agent_dir, skills)
            # Record in session meta if a session id is available.
            sid = meta.get("sid")
            if sid and resolved:
                try:
                    sessions_for_skills.update(sid, skills=skills_meta(resolved))
                except Exception:  # noqa: BLE001
                    pass

        skill_stager = _skill_stager

    return Merger(
        store=MergerStore(client, ns, repo),
        gh=RestGitHub(repo, token),
        git=git,
        agents=AgentRunner(SessionStore(client, ns), session_root=os.path.join(workdir, "sessions"),
                           omp_bin=env.get("SYMPHONY_OMP_BIN", "omp"), providers=providers,
                           mcp=mcp, skill_stager=skill_stager),
        team=TeamConfig(client, ns),
        repo=repo,
        states=dict(cfg.tracker_states),
        tracker=tracker,
        notify_post=notify_post,
        pause=pulse.wait,
        wake=pulse.fire,
        ticket_labels=tuple(cfg.required_labels),
    )


def start_loops(m: Merger, client, ns: str, pulse: Pulse, stop: threading.Event, *, reviewers: int,
                train: bool = True, watch: bool = True, poll_s: float = POLL_S,
                review_semaphore: threading.Semaphore | None = None) -> tuple[list, dict]:
    """Starts the coordinator, `reviewers` review workers, the train loop and the main watch;
    returns the threads and the coordinator's beat (for /healthz).

    `review_semaphore`: process-wide concurrency gate for agent review steps.  A worker
    acquires it only around `worker.step()`.  When step() finds nothing to do (pick returns
    None) it returns immediately — the semaphore is held for at most one review, never during
    idle polling.  This bounds the total concurrent agent work across all repositories."""
    beat = {"at": now_ms()}
    m.store.register()
    target = wake_target(m.repo)

    def coordinator() -> None:
        while not stop.is_set():
            try:
                reasons = wakes.wait(client, ns, target, poll_s)
                if reasons:
                    log.info("merger_woken", reasons=reasons[:10])
                # alive for the API while the wake loop runs (TTL three polls)
                m.store.heartbeat(max(int(poll_s * 3000), HEARTBEAT_TTL_MS))
            except redis.RedisError as exc:
                log.warning("merger_wake_failed", error=str(exc)[:200])
                stop.wait(2)
            beat["at"] = now_ms()
            pulse.fire()

    def review_loop(worker: ReviewWorker) -> None:
        backoff = 0
        while not stop.is_set():
            try:
                # The semaphore bounds the number of concurrent agent review steps
                # across all repositories.  Acquire before step() so idle workers
                # (step returns None when pick finds nothing) release quickly: the
                # expensive agent work inside step blocks a slot only while it runs.
                if review_semaphore is not None:
                    review_semaphore.acquire()
                try:
                    did = worker.step()
                finally:
                    if review_semaphore is not None:
                        review_semaphore.release()
                backoff = 0
                if did:
                    log.info("merger_review_step", worker=worker.id, outcome=did[:300])
                    continue
            except ProbeFailed as exc:
                delay = PROBE_BACKOFF_S[min(backoff, len(PROBE_BACKOFF_S) - 1)]
                backoff += 1
                log.warning("merger_probe_failed", worker=worker.id, error=str(exc)[:300], backoff_s=delay)
                stop.wait(delay)
                continue
            except Exception as exc:  # noqa: BLE001 — a pass failed; the next one retries
                log.warning("merger_review_pass_failed", worker=worker.id, error=str(exc)[:300])
            pulse.wait(poll_s)

    def train_loop() -> None:
        t = Train(m, owner=f"train:{INSTANCE}")
        while not stop.is_set():
            try:
                did = t.step()
                if did:
                    log.info("merger_train_step", outcome=did[:300])
                    continue
            except TrainBusy as exc:
                log.info("merger_train_busy", detail=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001
                log.warning("merger_train_step_failed", error=str(exc)[:300])
            pulse.wait(poll_s)

    def watch_loop() -> None:
        while not stop.is_set():
            try:
                said = watch_once(m, m.policy())
                if said:
                    log.info("merger_main_watch", said=said)
            except Exception as exc:  # noqa: BLE001
                log.warning("merger_main_watch_failed", error=str(exc)[:300])
            pulse.wait(poll_s)

    threads = [threading.Thread(target=coordinator, name=f"wake:{m.repo}", daemon=True)]
    if train:
        threads.append(threading.Thread(target=train_loop, name=f"train:{m.repo}", daemon=True))
    if watch:
        threads.append(threading.Thread(target=watch_loop, name=f"main-watch:{m.repo}", daemon=True))
    threads += [threading.Thread(target=review_loop, args=(ReviewWorker(m, f"{i + 1}"),),
                                 name=f"review-{i + 1}:{m.repo}", daemon=True) for i in range(reviewers)]
    for t in threads:
        t.start()
    return threads, beat


def managed_providers(ns: str, environ=os.environ) -> Callable[[str, str], object | None] | None:
    """The agents' managed provider keys (internals §13): the `symphony-providers` Secret in a
    cluster. A merger on a host (next to the local mode) has no Secret to read and must not
    reach whatever cluster the user's kubeconfig points at: its agents use the host env, as
    the local mode's runners do."""
    if not environ.get("KUBERNETES_SERVICE_HOST"):
        return None
    core, _ = kube.get_clients()
    return ProviderRepository(core, ns).runtime


def managed_mcp(client, ns: str, cfg, environ=os.environ) -> McpLaunch | None:
    """The agents' MCP servers (Admin › MCP): the `symphony-mcp` Secret in a cluster, reached
    through the gateway. A merger on a host has none, like its provider keys."""
    if not environ.get("KUBERNETES_SERVICE_HOST"):
        return None
    core, _ = kube.get_clients()
    return McpLaunch(client, ns, McpRepository(core, ns), gateway_url(cfg))


def missing_config(cfg, env) -> list[str]:
    """What the merger still lacks, by name (never a value): a valid WORKFLOW and the GitHub App
    credential.  A repository is no longer required at startup — discovery may provide repos."""
    missing = [] if cfg is not None else ["workflow"]
    missing += [name for name in GITHUB_APP_ENV if not env.get(name)]
    return missing


def _env_repos(env) -> list[str]:
    """Repositories from SYMPHONY_MERGER_REPO (comma-separated)."""
    raw = env.get("SYMPHONY_MERGER_REPO", "")
    repos = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                repos.append(normalize_repo(part))
            except ValueError:
                log.warning("merger_invalid_env_repo", repo=part[:100])
    return repos


def _discovery_repos(client, ns: str) -> list[str]:
    """Repositories from the discovery document with `merger_policy: true` and not archived."""
    raw = client.get(DISCOVERY_KEY)
    if raw is None:
        return []
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return []
    repos = []
    for item in doc.get("items") or []:
        if item.get("merger_policy") and not item.get("archived"):
            name = item.get("name", "")
            try:
                repos.append(normalize_repo(name))
            except ValueError:
                pass
    return repos


def discover_repos(cfg, env, client, ns: str) -> set[str]:
    """The union of all repositories that should have a merger."""
    repos: set[str] = set()
    # 1. SYMPHONY_MERGER_REPO (comma-separated)
    repos.update(_env_repos(env))
    # 2. cfg.default_repo
    if cfg is not None and cfg.default_repo:
        try:
            repos.add(normalize_repo(cfg.default_repo))
        except ValueError:
            pass
    # 3. Discovery document
    repos.update(_discovery_repos(client, ns))
    return repos


def configure(load_cfg: Callable[[], object | None], env, stop: threading.Event, *,
              pause: Callable[[float], object],
              now: Callable[[], float] = time.monotonic, logger=log) -> object | None:
    """Blocks until the merger infrastructure can start (WORKFLOW + GitHub App credential),
    then returns the config (None once stopped).  'No repository' is no longer fatal — the
    reconcile loop will discover repos later."""
    last = None
    while not stop.is_set():
        cfg = load_cfg()
        missing = missing_config(cfg, env)
        if not missing:
            return cfg
        if last is None or now() - last >= NOT_CONFIGURED_LOG_S:
            logger.warning("merger_not_configured", missing=missing)
            last = now()
        pause(NOT_CONFIGURED_POLL_S)
    return None


class MergerFleet:
    """Manages a set of Mergers, one per repository, with a reconcile loop that starts and
    stops them as the set of discovered repositories changes."""

    def __init__(self, *, client, ns: str, cfg, env, stop: threading.Event,
                 build_fn: Callable[..., Merger], review_semaphore: threading.Semaphore,
                 reviewers: int, poll_s: float = POLL_S) -> None:
        self.client = client
        self.ns = ns
        self.cfg = cfg
        self.env = env
        self.stop = stop
        self.build_fn = build_fn
        self.review_semaphore = review_semaphore
        self.reviewers = reviewers
        self.poll_s = poll_s
        # repo → {"merger": Merger, "threads": list, "beat": dict, "stop": Event, "pulse": Pulse}
        self._repos: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Repos whose build failed: skip them until the next reconcile.
        self._failed: set[str] = set()

    def running_repos(self) -> set[str]:
        with self._lock:
            return set(self._repos)

    def healthy(self) -> bool:
        """True when every running repository's coordinator beats and its threads live.
        A repository whose build failed does not make the pod unhealthy."""
        with self._lock:
            if not self._repos:
                return True
            for entry in self._repos.values():
                if now_ms() - entry["beat"]["at"] >= STALE_MS:
                    return False
                if not all(t.is_alive() for t in entry["threads"]):
                    return False
        return True

    def start_repo(self, repo: str) -> bool:
        """Start loops for one repository.  Returns True on success, False on build failure."""
        pulse = Pulse()
        repo_stop = threading.Event()
        try:
            m = self.build_fn(repo=repo, pulse=pulse)
        except (NotConfigured, NotAuthorizedForRepo, CredentialError, GitError, GitHubError) as exc:
            log.warning("merger_fleet_build_failed", repo=repo, error=str(exc)[:300])
            return False
        threads, beat = start_loops(m, self.client, self.ns, pulse, repo_stop,
                                    reviewers=self.reviewers, review_semaphore=self.review_semaphore,
                                    poll_s=self.poll_s)
        with self._lock:
            self._repos[repo] = {"merger": m, "threads": threads, "beat": beat,
                                 "stop": repo_stop, "pulse": pulse}
        log.info("merger_fleet_started", repo=repo, reviewers=self.reviewers)
        return True

    def stop_repo(self, repo: str, join_timeout: float = 10.0) -> None:
        """Stop loops for one repository and join its threads."""
        with self._lock:
            entry = self._repos.pop(repo, None)
        if entry is None:
            return
        entry["stop"].set()
        entry["pulse"].fire("shutdown")
        for t in entry["threads"]:
            t.join(timeout=join_timeout)
        log.info("merger_fleet_stopped", repo=repo)

    def reconcile(self, cfg) -> None:
        """Discover repos, start new ones, stop removed ones."""
        self.cfg = cfg
        wanted = discover_repos(cfg, self.env, self.client, self.ns)
        with self._lock:
            current = set(self._repos)
        # Stop repos that disappeared
        for repo in current - wanted:
            self.stop_repo(repo)
            self._failed.discard(repo)
        # Start repos that are new
        for repo in wanted - current:
            ok = self.start_repo(repo)
            if not ok:
                self._failed.add(repo)
            else:
                self._failed.discard(repo)
        # Retry previously failed repos that are still wanted
        for repo in list(self._failed):
            if repo in wanted and repo not in self.running_repos():
                ok = self.start_repo(repo)
                if ok:
                    self._failed.discard(repo)

    def stop_all(self) -> None:
        with self._lock:
            repos = list(self._repos)
        for repo in repos:
            self.stop_repo(repo)


def main() -> None:
    stop = threading.Event()

    def terminate(*_):
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    health_port = int(os.environ.get("SYMPHONY_HEALTH_PORT", "8081"))
    holder = ConfigHolder()
    client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                  socket_timeout=POLL_S + 10, health_check_interval=30)
    reviewers = int(os.environ.get("SYMPHONY_MERGER_REVIEWERS", "2"))
    concurrency = int(os.environ.get("SYMPHONY_MERGER_CONCURRENCY", str(DEFAULT_CONCURRENCY)))
    review_semaphore = threading.Semaphore(concurrency)

    fleet: MergerFleet | None = None

    # Healthy while it waits for setup and, once running, while every repo's loops beat.
    def healthy():
        if fleet is None:
            return True
        return fleet.healthy()

    metrics.serve(health_port, healthy, metrics.Scrape("merger"))

    def load():
        load_config(holder)
        return holder.config

    cfg = configure(load, os.environ, stop, pause=stop.wait)
    if cfg is None:
        return
    ns = pod_namespace(cfg)
    providers = managed_providers(ns)
    mcp = managed_mcp(client, ns, cfg)

    def build_for_repo(*, repo: str, pulse: Pulse) -> Merger:
        return build(client, ns, cfg, pulse=pulse, repo=repo,
                     providers=providers, mcp=mcp)

    fleet = MergerFleet(client=client, ns=ns, cfg=cfg, env=os.environ, stop=stop,
                        build_fn=build_for_repo, review_semaphore=review_semaphore,
                        reviewers=reviewers)

    # Initial reconcile
    fleet.reconcile(cfg)
    log.info("merger_fleet_reconciled", repos=sorted(fleet.running_repos()),
             concurrency=concurrency, reviewers=reviewers)

    # Reconcile loop
    while not stop.wait(RECONCILE_INTERVAL_S):
        cfg_new = load()
        if cfg_new is not None:
            cfg = cfg_new
        fleet.reconcile(cfg)

    fleet.stop_all()


if __name__ == "__main__":
    main()
