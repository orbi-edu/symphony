"""Orchestrator daemon — the Deployment's entrypoint.

- The loop is wake-driven: between ticks it blocks on `wake.wait(daemon)`, so a Linear or
  GitHub webhook (pushed by the API) starts the next tick at once; the polling interval is
  only the safety net for a missed webhook (internals §6).
- A liveness probe against /healthz on the health port (`metrics.serve`, which also serves
  /metrics): a wedged loop stops answering and the kubelet restarts the pod.
- The idle reaper runs from the loop.
- An install that is not set up never crash-loops. With no WORKFLOW (or an empty one) or no
  usable tracker (credential or board scope missing) the daemon keeps its heartbeat, reports
  `daemon:setup` incomplete, logs `daemon_setup_incomplete` at most once a minute and
  re-reads both every step: it starts dispatching on the first step after they are valid.

The lease stays: a rolling update briefly runs two daemons (the Deployment uses
Recreate to narrow that, but narrows is not never), and the lease plus the dispatch
tokens are what make the overlap harmless. A standby daemon never consumes wakes — they
belong to the leader.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager

from .. import discovery, ghapp, kube, metrics, orchestrator, state, wake
from ..admin_auth import authorize_command
from ..admin_commands import AdminStore, process_commands
from ..admin_workflow import WorkflowRepository
from ..config import Config, ConfigHolder, bind_role_model
from ..edge import SetupGate, load_config, log, now_ms, pod_namespace, usable_tracker
from ..effects import K8sEffects
from ..kv import LeaseKV, RedisKV
from ..teamconfig import TeamConfig

HEALTH_PORT = int(os.environ.get("SYMPHONY_HEALTH_PORT", "8081"))
# Same threshold the Modal watchdog used: a loop silent for longer than this is dead.
STALE_MS = 90_000

_last_beat_ms = 0


def _beat() -> None:
    global _last_beat_ms
    _last_beat_ms = now_ms()


@contextmanager
def _lease_guard(kv, owner):
    """Maintain the lease during bounded external effects; never reacquire after loss."""
    stopped = threading.Event()
    lost = threading.Event()

    def authoritative():
        lease = kv.get(state.LOCK_KEY)
        if not lease or lease.get("owner") != owner or lease.get("lease_expires", 0) <= now_ms():
            lost.set()
        return not lost.is_set()

    def maintain():
        while not stopped.wait(state.LEASE_TTL_MS / 3000):
            try:
                if lost.is_set() or not state.renew_lease(kv, owner, now_ms(), require_live=True):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=maintain, daemon=True)
    thread.start()
    try:
        yield authoritative
    finally:
        stopped.set()
        thread.join(timeout=15)


REDIS_BACKOFF_INITIAL_S = 0.2
REDIS_BACKOFF_MAX_S = 5.0


def wait_for_redis(
    client,
    stop: threading.Event,
    *,
    initial_s: float = REDIS_BACKOFF_INITIAL_S,
    max_s: float = REDIS_BACKOFF_MAX_S,
    on_attempt: Callable[[], None] | None = None,
) -> bool:
    """Block until Redis answers a PING, backing off exponentially (capped) between tries.

    A daemon that starts before Redis is ready, or outlives a Redis restart, waits for it
    instead of crash-restarting: the restart costs a pod cycle and a CrashLoopBackOff delay
    that outlasts the outage. `on_attempt` runs before every try (the daemon keeps its
    liveness beat with it: waiting on Redis is not a wedged loop). Returns False only when
    `stop` is set first."""
    delay = initial_s
    attempt = 0
    while not stop.is_set():
        if on_attempt is not None:
            on_attempt()
        try:
            client.ping()
            if attempt:
                log.info("redis_ready", attempts=attempt + 1)
            return True
        except Exception as exc:  # noqa: BLE001 — any failure to answer is "not ready yet"
            attempt += 1
            log.warning("redis_unavailable", attempt=attempt, retry_in_s=delay, error=str(exc))
            stop.wait(delay)
            delay = min(delay * 2, max_s)
    return False


def _block_cap_s(client) -> float | None:
    """The longest one blocking pop may take on this client without tripping its own socket
    timeout (which would read as a Redis failure and fall back to sleeping)."""
    kwargs = getattr(getattr(client, "connection_pool", None), "connection_kwargs", None) or {}
    socket_timeout = kwargs.get("socket_timeout")
    return None if socket_timeout is None else max(float(socket_timeout) - 1.0, 0.1)


def run_loop(
    step: Callable[[list[str]], bool],
    *,
    client,
    namespace: str,
    interval_s: Callable[[], float],
    stop: threading.Event,
    on_redis_wait: Callable[[], None] | None = None,
) -> None:
    """Run `step(wake_reasons)` until `stop`, waiting between steps on the daemon's wake
    list.

    `step` returns True when this daemon is the authoritative one. Only then does the loop
    block on `wake.wait`, which returns as soon as a wake arrives (with every pending reason,
    so a burst of webhooks costs one tick) or after the safety interval. A wake that lands
    while a tick runs stays queued and starts the next tick immediately — it carries news
    the running tick may have missed. A standby daemon sleeps the lease TTL instead, leaving
    the wakes to the leader. A step that fails on Redis waits for Redis, then carries on.
    """
    cap = _block_cap_s(client)
    reasons: list[str] = []
    while not stop.is_set():
        try:
            leader = step(reasons)
        except Exception as exc:  # noqa: BLE001 — e.g. Redis restarted under the lease call
            log.error("step_failed", error=str(exc))
            if not wait_for_redis(client, stop, on_attempt=on_redis_wait):
                return
            reasons = []
            continue
        reasons = []
        if stop.is_set():
            return
        if not leader:
            stop.wait(state.LEASE_TTL_MS / 1000)
            continue
        # Never longer than half the liveness window, so a long safety interval cannot
        # read as a wedged loop.
        deadline = time.monotonic() + min(interval_s(), STALE_MS / 2000)
        try:
            while not reasons and not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                block = remaining if cap is None else min(remaining, cap)
                reasons = wake.wait(client, namespace, wake.DAEMON, block)
        except Exception as exc:  # noqa: BLE001 — no wake channel still means a poll
            log.warning("wake_wait_failed", error=str(exc))
            wait_for_redis(client, stop, on_attempt=on_redis_wait)


NO_WORKFLOW_REASON = "WORKFLOW.md is missing, empty or invalid."


def await_workflow(holder: ConfigHolder, kv, owner: str, gate: SetupGate, *, path=None):
    """A step for `run_loop` while no WORKFLOW has ever loaded (a fresh install mounts an
    empty ConfigMap until setup seeds it): keep the liveness beat, report
    `setup_incomplete`, and re-read the file every step. Returns a step function and the
    event it sets once a WORKFLOW is in force."""
    configured = threading.Event()

    def step(reasons: list[str]) -> bool:
        _beat()
        load_config(holder, path)
        if holder.config is not None:
            configured.set()
            return True
        now = now_ms()
        if not state.renew_lease(kv, owner, now):
            return False
        state.heartbeat(kv, now)
        gate.update(kv, now, NO_WORKFLOW_REASON)
        return True

    return step, configured


def main() -> None:
    kv = RedisKV()
    stop = threading.Event()
    owner = f"daemon-{uuid.uuid4().hex[:8]}"
    store_ns = pod_namespace()  # the Redis stores' namespace (internals §1)
    events = [stop]

    def _terminate(signum, _frame) -> None:
        """SIGTERM (a rollout, or a setup step's restart): stop at the next step boundary
        and hand the lease back, so the next daemon takes over at once instead of waiting
        out the lease TTL. A wake ends a blocked `wake.wait` without waiting its timeout."""
        for event in events:
            event.set()
        threading.Thread(target=_wake_self, daemon=True).start()

    def _wake_self() -> None:
        try:
            wake.wake(kv.client, store_ns, wake.DAEMON, "shutdown")
        except Exception:  # noqa: BLE001 — the loop still ends at its next timeout
            pass

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        _run(kv, stop, owner, store_ns, events)
    finally:
        try:
            if state.release_lease(kv, owner):
                log.info("lease_released", owner=owner)
        except Exception as exc:  # noqa: BLE001 — the TTL still frees it
            log.warning("lease_release_failed", error=str(exc))


def _run(kv, stop: threading.Event, owner: str, store_ns: str, events: list) -> None:
    # The probe answers from the first moment, and the Redis wait keeps it fresh: a daemon
    # waiting for Redis to come up is alive, and restarting it would not bring Redis sooner.
    _beat()
    scrape = metrics.Scrape("daemon", lambda: metrics.daemon_families(kv.client, store_ns, owner))
    metrics.serve(HEALTH_PORT, lambda: now_ms() - _last_beat_ms < STALE_MS, scrape)
    wait_for_redis(kv.client, stop, on_attempt=_beat)
    holder = ConfigHolder()
    gate = SetupGate()
    load_config(holder)
    if holder.config is None:
        # Not a crash: a fresh install starts without a WORKFLOW, and restarting would not
        # make one appear. Durable commands wait too — they need the policy's namespace and
        # effects — so the setup API seeds the ConfigMap itself.
        waiting, configured = await_workflow(holder, kv, owner, gate)
        events.append(configured)
        if stop.is_set():
            configured.set()
        run_loop(
            waiting,
            client=kv.client,
            namespace=store_ns,
            interval_s=lambda: Config.polling_interval_ms / 1000,
            stop=configured,
            on_redis_wait=_beat,
        )
        if stop.is_set():
            return
    cfg = holder.config
    core, batch = kube.get_clients()
    ns = pod_namespace(cfg)
    team = TeamConfig(kv.client, store_ns)
    fx = K8sEffects(cfg, core, batch, ns)
    store = AdminStore(kv.client, ns)
    workflow = WorkflowRepository(
        core, ns, name=os.environ.get("SYMPHONY_WORKFLOW_CONFIGMAP", f"{cfg.app_name}-workflow")
    )
    credential = ghapp.InstallationCredential.from_env(os.environ, now_ms, permissions=ghapp.READ_ONLY)
    refresher = discovery.Refresher(
        kv, lambda: credential.token() if os.environ.get("GITHUB_APP_ID") else None, now_ms)
    swept = False

    def step(reasons: list[str]) -> bool:
        global _last_beat_ms
        nonlocal cfg, swept
        now = now_ms()
        _last_beat_ms = now
        load_config(holder)  # hot reload from the ConfigMap mount, last-known-good kept
        if not state.renew_lease(kv, owner, now):
            # Only the authoritative daemon advertises control-plane freshness.
            log.info("lease_held_by_other_waiting")
            return False
        try:
            # internals §4: each tick binds the implementer model of the CURRENT team
            # configuration and reports the generation it applied.
            team_doc = team.get()
            cfg = bind_role_model(holder.config, team_doc, "implementer")
            fx.cfg = cfg
            team.observe("daemon", int(team_doc["generation"]))
            with _lease_guard(kv, owner) as authoritative:
                fx.authoritative = authoritative
                state.heartbeat(kv, now_ms())
                if holder.accepted_document_hash:
                    store.observe_workflow("daemon", holder.accepted_document_hash)
                    if holder.rejected_document_hash:
                        store.observe_workflow(
                            "daemon", holder.accepted_document_hash, status="rejected",
                            message="A newer mounted workflow was rejected; last accepted configuration remains active.",
                        )
                tracker, ready = usable_tracker(cfg, kv, gate, now_ms())
                # Commands apply even while setup is incomplete: setup itself publishes the
                # WORKFLOW through them. Tracker-bound ones fail with the setup reason.
                process_commands(
                    store, kv, tracker, cfg, fx, owner, now,
                    publish_workflow=workflow.publish,
                    authorize=lambda command: authorize_command(command, ns),
                )
                if ready and authoritative():
                    tick_kv = LeaseKV(kv, owner)
                    if not swept:
                        orchestrator.startup_sweep(tick_kv, tracker, cfg, now_ms(), fx)
                        swept = True
                    result = orchestrator.tick(
                        tick_kv, tracker, cfg, now_ms(), fx, owner,
                        admission_open=lambda: bool(authoritative() and not store.paused()),
                        wake_reasons=reasons,
                    )
                    refresher.start(cfg)
                    log.info("tick", wakes=len(reasons), **result)
        except Exception as exc:  # noqa: BLE001 — a tick never brings the daemon down
            log.error("tick_failed", error=str(exc))
        try:
            lease = kv.get(state.LOCK_KEY)
            if lease and lease.get("owner") == owner and lease.get("lease_expires", 0) > now_ms():
                kube.reap_idle_workspaces(core, ns, cfg, now_ms())
        except Exception as exc:  # noqa: BLE001 — hygiene, never worth the daemon
            log.warning("idle_reap_failed", error=str(exc))
        return True

    _beat()
    run_loop(
        step,
        client=kv.client,
        namespace=store_ns,
        interval_s=lambda: cfg.polling_interval_ms / 1000,
        stop=stop,
        on_redis_wait=_beat,
    )


if __name__ == "__main__":
    main()
