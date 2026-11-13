"""symphony-merger's entrypoint: review workers, one train loop and the main CI watch.

Every loop idles on one shared pulse: a coordinator thread blocks on `wake.wait(client, ns,
store.wake_target(repo), 30)` (`merger:<owner/name>`) and fires the pulse on each wake (a
GitHub webhook, an API action for this repository) or timeout, so
a webhook reaches every loop at once and a missed one costs at most the 30 s safety poll. A
hand-off or a conflict fires the pulse locally too, so the train takes it at once.

Env: REDIS_URL, POD_NAMESPACE, the tracker's credential (`LINEAR_API_KEY` for Linear),
GITHUB_APP_ID / _PRIVATE_KEY / _INSTALLATION_ID,
the `model-provider` Secret's env (agents whose role has no managed provider use it; the others
get their key from `symphony-providers`, read at every agent run),
SYMPHONY_MERGER_WORKDIR (mirror, worktrees, omp sessions), SYMPHONY_MERGER_REPO (owner/name;
empty: WORKFLOW.md's default repo), SYMPHONY_MERGER_REVIEWERS (default 2), SYMPHONY_OMP_BIN
(default omp), SYMPHONY_HEALTH_PORT (default 8081, GET /healthz and GET /metrics).

Until setup gives it a valid WORKFLOW, the GitHub App credential and a repository it can reach,
the process idles (`configure`): healthy, no heartbeat, `merger_not_configured` once a minute.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable

import redis

from .. import kube, metrics
from .. import wake as wakes
from ..config import ConfigError, ConfigHolder
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


def build(client, ns: str, cfg, env=os.environ, pulse: Pulse | None = None, *,
          token: Callable[[], str] | None = None, notify_post=None, tracker=None,
          providers: Callable[[str, str], object | None] | None = None,
          mcp: McpLaunch | None = None) -> Merger:
    """The merger for one repository. `token` defaults to the GitHub App installation token
    (scoped to that repository); `notify_post` to `symphony.notify.post`; `tracker` to
    WORKFLOW.md's tracker (none while its credential is missing); `providers` resolves each
    agent role's credential (internals §13), None leaving the pod's env to every agent; `mcp`
    stages each agent role's MCP servers (None: no MCP servers)."""
    repo = env.get("SYMPHONY_MERGER_REPO") or cfg.default_repo
    if not repo:
        raise NotConfigured(["repository"])
    workdir = env.get("SYMPHONY_MERGER_WORKDIR", "/var/lib/symphony/merger")
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
    git = Mirror(workdir, f"{github_server_url()}/{repo}.git", token=token)
    git.ensure()
    git.fetch()
    return Merger(
        store=MergerStore(client, ns, repo),
        gh=RestGitHub(repo, token),
        git=git,
        agents=AgentRunner(SessionStore(client, ns), session_root=os.path.join(workdir, "sessions"),
                           omp_bin=env.get("SYMPHONY_OMP_BIN", "omp"), providers=providers,
                           mcp=mcp),
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
                train: bool = True, watch: bool = True, poll_s: float = POLL_S) -> tuple[list, dict]:
    """Starts the coordinator, `reviewers` review workers, the train loop and the main watch;
    returns the threads and the coordinator's beat (for /healthz)."""
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
                did = worker.step()
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

    threads = [threading.Thread(target=coordinator, name="wake", daemon=True)]
    if train:
        threads.append(threading.Thread(target=train_loop, name="train", daemon=True))
    if watch:
        threads.append(threading.Thread(target=watch_loop, name="main-watch", daemon=True))
    threads += [threading.Thread(target=review_loop, args=(ReviewWorker(m, f"{i + 1}"),), name=f"review-{i + 1}",
                                 daemon=True) for i in range(reviewers)]
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
    """What the merger still lacks, by name (never a value): a valid WORKFLOW, the GitHub App
    credential and its repository."""
    missing = [] if cfg is not None else ["workflow"]
    missing += [name for name in GITHUB_APP_ENV if not env.get(name)]
    repo = env.get("SYMPHONY_MERGER_REPO") or (cfg.default_repo if cfg is not None else None)
    try:
        normalize_repo(repo or "")
    except ValueError:
        missing.append("repository")
    return missing


def configure(load_cfg: Callable[[], object | None], env, stop: threading.Event, *,
              build_merger: Callable[[object], Merger], pause: Callable[[float], object],
              now: Callable[[], float] = time.monotonic, logger=log) -> Merger | None:
    """Blocks until the merger can be built, then returns it (None once stopped). While setup
    is incomplete (no WORKFLOW, no GitHub App credential, no repository, or a credential that
    doesn't mint a token or reach the repository) it idles: no heartbeat, so `GET /v1/setup`
    shows no merger for the repository; `merger_not_configured` at most once a minute; no
    crash loop. The WORKFLOW is re-read every pass; the Secrets arrive with the restart setup
    triggers."""
    last = None
    while not stop.is_set():
        cfg = load_cfg()
        missing = missing_config(cfg, env)
        if not missing:
            try:
                return build_merger(cfg)
            except NotConfigured as exc:
                missing = exc.missing
            except NotAuthorizedForRepo:
                missing = ["repository_access"]  # the App is not installed on the repository
            except CredentialError:
                missing = ["github_app"]  # it doesn't mint a token: the error never names a value
            except (GitError, GitHubError):
                missing = ["repository_access"]
        if last is None or now() - last >= NOT_CONFIGURED_LOG_S:
            logger.warning("merger_not_configured", missing=missing)
            last = now()
        pause(NOT_CONFIGURED_POLL_S)
    return None


def main() -> None:
    stop = threading.Event()
    pulse = Pulse()

    def terminate(*_):
        stop.set()
        pulse.fire()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    running: dict = {}
    # Healthy while it waits for setup (a liveness restart would not configure it) and, once
    # running, while its loops beat.
    metrics.serve(int(os.environ.get("SYMPHONY_HEALTH_PORT", "8081")),
                  lambda: not running or (now_ms() - running["beat"]["at"] < STALE_MS
                                          and all(t.is_alive() for t in running["threads"])),
                  metrics.Scrape("merger"))
    holder = ConfigHolder()
    client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                  socket_timeout=POLL_S + 10, health_check_interval=30)

    def load():
        load_config(holder)
        return holder.config

    def build_merger(cfg):
        ns = pod_namespace(cfg)
        return build(client, ns, cfg, pulse=pulse, providers=managed_providers(ns),
                     mcp=managed_mcp(client, ns, cfg))

    m = configure(load, os.environ, stop, build_merger=build_merger, pause=stop.wait)
    if m is None:
        return
    ns = m.store.ns
    count = int(os.environ.get("SYMPHONY_MERGER_REVIEWERS", "2"))
    threads, beat = start_loops(m, client, ns, pulse, stop, reviewers=count)
    running.update(threads=threads, beat=beat)
    log.info("merger_started", repo=m.repo, namespace=ns, reviewers=count)
    while not stop.wait(5):
        pass


if __name__ == "__main__":
    main()
