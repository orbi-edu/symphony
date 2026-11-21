"""symphony-assistant's entrypoint: the chat assistant on Slack and Discord (`assistant/`).

It connects out (Slack Socket Mode, the Discord Gateway): no public URL. Either transport alone
is enough.

Env: REDIS_URL, POD_NAMESPACE, SLACK_APP_TOKEN + SLACK_BOT_TOKEN (`symphony-slack`),
DISCORD_BOT_TOKEN (`symphony-discord`), GITHUB_APP_ID / _PRIVATE_KEY / _INSTALLATION_ID (read-only
mirrors and the GitHub tracker), the tracker's credential, the `model-provider` Secret's env,
SYMPHONY_ASSISTANT_WORKDIR (mirrors, worktrees, omp sessions), SYMPHONY_OMP_BIN (default omp),
SYMPHONY_HEALTH_PORT (default 8081, GET /healthz and GET /metrics).

Until setup gives it a valid WORKFLOW and a transport's tokens it idles:
`assistant_not_configured missing=[...]` once a minute. With `assistant.enabled` false it
idles with `assistant_disabled`. Either way it stays healthy and writes the heartbeat
`sym:<ns>:assistant:heartbeat` (TTL 90 s) with `state: "idle"`; running, `state: "running"`.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping

import redis

from .. import mcp_servers, metrics
from ..config import ConfigError, ConfigHolder
from ..edge import gateway_url, load_config, log, make_tracker, now_ms, pod_namespace
from ..endpoints import github_server_url

HEARTBEAT_TTL_S = 90
BEAT_S = 30
POLL_S = 15
LOG_EVERY_S = 60
STALE_MS = HEARTBEAT_TTL_S * 1000
GITHUB_APP_ENV = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")
_TOKENS = (
    ("slack_app_token", "SLACK_APP_TOKEN"),
    ("slack_bot_token", "SLACK_BOT_TOKEN"),
    ("discord_bot_token", "DISCORD_BOT_TOKEN"),
)


def heartbeat_key(namespace: str) -> str:
    return f"sym:{namespace}:assistant:heartbeat"


def missing_config(cfg, env: Mapping[str, str]) -> list[str]:
    """What the assistant still lacks, by name (never a value): a valid WORKFLOW and the
    tokens of at least one transport."""
    missing = [] if cfg is not None else ["workflow"]
    slack = bool(env.get("SLACK_APP_TOKEN") and env.get("SLACK_BOT_TOKEN"))
    if not (slack or env.get("DISCORD_BOT_TOKEN")):
        missing += [name for name, var in _TOKENS if not env.get(var)]
    return missing


def beat(client, namespace: str, *, state: str, transports: Mapping | None = None,
         stats: Mapping | None = None, missing: list[str] | None = None,
         soul: Mapping | None = None) -> None:
    """The heartbeat the setup API, `symphony assistant status` and Admin › Assistant read.
    `soul` is `{repo, path, sha}` of the active soul file (sha None: it is missing)."""
    transports = transports or {}
    stats = stats or {}
    doc = {
        "state": state,
        "at": now_ms(),
        "missing": list(missing or []),
        "threads": int(stats.get("threads", 0)),
        "active": int(stats.get("active", 0)),
        "queued": int(stats.get("queued", 0)),
        "soul": dict(soul) if soul else None,
    }
    for name in ("slack", "discord"):
        t = transports.get(name)
        doc[name] = {"configured": t is not None, "connected": bool(t is not None and t.connected)}
    client.set(heartbeat_key(namespace), json.dumps(doc), ex=HEARTBEAT_TTL_S)


def wait_until_ready(load_cfg: Callable[[], object | None], env: Mapping[str, str],
                     stop: threading.Event, *, client, pause: Callable[[float], object],
                     now: Callable[[], float] = time.monotonic, logger=log) -> object | None:
    """Blocks until a valid WORKFLOW enables the assistant and a transport has its tokens;
    returns that config (None once stopped). Meanwhile: an idle heartbeat, a log line at
    most once a minute, no crash loop."""
    last: tuple[str, float] | None = None
    while not stop.is_set():
        cfg = load_cfg()
        missing = missing_config(cfg, env)
        if not missing and cfg.assistant_enabled:
            return cfg
        event = "assistant_not_configured" if missing else "assistant_disabled"
        try:
            beat(client, pod_namespace(cfg), state="idle", missing=missing)
        except redis.RedisError as exc:
            logger.warning("assistant_heartbeat_failed", error=str(exc)[:200])
        if last is None or last[0] != event or now() - last[1] >= LOG_EVERY_S:
            if missing:
                logger.warning(event, missing=missing)
            else:
                logger.info(event)
            last = (event, now())
        pause(POLL_S)
    return None


class _Trackers:
    """WORKFLOW's tracker, rebuilt when the WORKFLOW changes."""

    def __init__(self, policy: Callable[[], object]) -> None:
        self.policy = policy
        self._for: object | None = None
        self._tracker = None
        self._lock = threading.Lock()

    def __call__(self):
        cfg = self.policy()
        with self._lock:
            if self._tracker is None or self._for is not cfg:
                self._tracker, self._for = make_tracker(cfg), cfg
            return self._tracker


def build(cfg_holder: ConfigHolder, client, namespace: str, env: Mapping[str, str], *,
          transports: dict | None = None, core=None, workdir: str | None = None):
    """The engine and its transports. `cfg_holder.config` is read at each use, so a
    republished WORKFLOW applies without a restart."""
    from ..admin_commands import AdminStore
    from ..assistant import tools
    from ..assistant.confirm import AUDIT_ACTION, Confirmer, Policy, ProposalStore
    from ..assistant.engine import Engine
    from ..assistant.facts import AUDIT_ACTION as FACT_AUDIT
    from ..assistant.facts import PERMISSIONS as FACT_PERMISSIONS
    from ..assistant.facts import FactConfirmer, FactPRs
    from ..assistant.memory import MemoryStore
    from ..assistant.runtime import (
        AssistRunner,
        ReadOnlyRepos,
        default_repositories,
        reachable_repositories,
    )
    from ..assistant.soul import MAX_BYTES, SoulReader
    from ..assistant.soul import source as soul_source
    from ..endpoints import github_api_url
    from ..ghapp import READ_ONLY, InstallationCredential
    from ..mcp_servers import McpLaunch, McpRepository
    from ..sessions import SessionStore
    from ..teamconfig import TeamConfig
    from .merger import managed_providers

    def policy():
        return cfg_holder.config

    if transports is None:
        from ..assistant.discord_gateway import DiscordGatewayTransport
        from ..assistant.slack_socket import SlackSocketTransport

        made = (SlackSocketTransport.from_env(env), DiscordGatewayTransport.from_env(env))
        transports = {t.name: t for t in made if t is not None}
    workdir = workdir or env.get("SYMPHONY_ASSISTANT_WORKDIR", "/var/lib/symphony/assistant")
    tracker = _Trackers(policy)

    def repositories() -> list[str]:
        """What the agents may read: `assistant.repositories`, else every reachable one."""
        cfg = policy()
        return list(cfg.assistant_repositories or reachable_repositories(client, namespace, cfg))

    def mirrored() -> list[str]:
        """Always checked out: the repositories Symphony works on, and the soul's. The other
        reachable ones are checked out on demand (`open_repository`)."""
        cfg = policy()
        repos = list(cfg.assistant_repositories or default_repositories(client, namespace, cfg))
        src = soul_source(cfg)
        if src is not None and src[0] not in {r.lower() for r in repos}:
            repos.append(src[0])
        return repos

    token_for = None
    if all(env.get(name) for name in GITHUB_APP_ENV):
        def token_for(repo: str) -> Callable[[], str]:
            return InstallationCredential.from_env(env, now_ms, target_repos=(repo,),
                                                   permissions=READ_ONLY).token

    status = tools.status_reader(client, namespace, policy().tracker_states)

    def tools_for(role: str) -> list:
        out = [tools.status_tool(status),
               tools.search_tool(lambda query, limit: tracker().search(query, limit)),
               tools.open_repository_tool(repositories, lambda repo: repos.open(repo))]
        if role == "planner":
            out.append(tools.propose_tool(repositories()))
        return out

    in_cluster = bool(env.get("KUBERNETES_SERVICE_HOST"))
    if core is None and in_cluster:
        from .. import kube

        core, _ = kube.get_clients()
    mcp = McpLaunch(client, namespace, McpRepository(core, namespace, env.get("SYMPHONY_MCP_FILE")),
                    gateway_url(policy()))
    repos = ReadOnlyRepos(workdir, mirrored, server_url=github_server_url(), token_for=token_for,
                          base_branch_for=lambda repo: policy().base_branch(repo), logger=log)
    runner = AssistRunner(
        SessionStore(client, namespace),
        session_root=os.path.join(workdir, "sessions"),
        config=policy,
        team=TeamConfig(client, namespace),
        tools_for=tools_for,
        repos=repos,
        providers=managed_providers(namespace, env),
        mcp=mcp,
        omp_bin=env.get("SYMPHONY_OMP_BIN", "omp"),
    )

    def confirm_policy() -> Policy:
        cfg = policy()
        return Policy(notify_users=cfg.notify_users, ticket_state=cfg.assistant_ticket_state,
                      start_state=cfg.active_states[0], ticket_labels=cfg.assistant_ticket_labels,
                      required_labels=cfg.required_labels)

    audit_store = AdminStore(client, namespace)
    confirmer = Confirmer(
        ProposalStore(client, namespace),
        tracker=tracker,
        policy=confirm_policy,
        audit=lambda actor, target, reason: audit_store.audit_event(
            actor=actor, action=AUDIT_ACTION, target=target, reason=reason),
        logger=log,
    )
    facts = None
    if all(env.get(name) for name in GITHUB_APP_ENV):
        def write_token(repo: str) -> str:
            return InstallationCredential.from_env(env, now_ms, target_repos=(repo,),
                                                   permissions=FACT_PERMISSIONS).token()

        prs = FactPRs(write_token, api_url=github_api_url(),
                       base_branch_for=lambda repo: policy().base_branch(repo))
        facts = FactConfirmer(
            ProposalStore(client, namespace, kind="fact"),
            opener=prs.open,
            notify_users=lambda: policy().notify_users,
            audit=lambda actor, target, reason: audit_store.audit_event(
                actor=actor, action=FACT_AUDIT, target=target, reason=reason),
            logger=log,
        )
    souls = SoulReader(policy, lambda repo, path: repos.blob(repo, path, MAX_BYTES + 1), logger=log)

    # -- sandbox: one workspace pod per chat thread (assistant/sandbox.py) ---------------------
    sandbox_tool_fn = None
    on_session_end_fn = None
    if core is not None and all(env.get(name) for name in GITHUB_APP_ENV):
        from ..assistant.sandbox import AssistantSandbox, sandbox_run_tool, sweep_orphans
        from ..ghapp import InstallationCredential

        sandboxes: dict[str, AssistantSandbox] = {}
        sandboxes_lock = threading.Lock()

        def sandbox_token(repo: str) -> str:
            # One repository, read-only contents: the clone is all the sandbox may do.
            return InstallationCredential.from_env(
                env, now_ms, target_repos=(repo,),
                permissions={"contents": "read", "metadata": "read"}).token()

        def sandbox_for(thread_key: str) -> AssistantSandbox:
            with sandboxes_lock:
                sb = sandboxes.get(thread_key)
                if sb is None:
                    sb = sandboxes[thread_key] = AssistantSandbox(
                        core, namespace, policy, thread_key=thread_key,
                        token_for=sandbox_token,
                        base_branch_for=lambda r: policy().base_branch(r))
                return sb

        def sandbox_tool_fn(thread_key: str, notes):
            if not policy().assistant_sandbox_enabled:
                return None
            return sandbox_run_tool(lambda: sandbox_for(thread_key), repositories,
                                    lambda: notes.login)

        def on_session_end_fn(thread_key: str) -> None:
            with sandboxes_lock:
                sb = sandboxes.pop(thread_key, None)
            if sb is not None:
                sb.close()

        try:
            swept = sweep_orphans(core, namespace, policy().app_name)
            if swept:
                log.info("assistant_sandbox_orphans_deleted", pods=swept)
        except Exception as exc:  # noqa: BLE001 — a missing permission must not stop the bot
            log.warning("assistant_sandbox_sweep_failed", error=type(exc).__name__)

    # -- MCP guidance and share_device for the chat assistant ---------------------------
    # Both read the MCP definitions at each session, so a device lab added later works
    # without a restart: a role gets share_device when its servers bring chat guidance.
    def _viewer_for_click(server: str, lease_id: str, sid: str) -> dict | None:
        """A device: click: a control viewer while the sharing session holds the lease."""
        if not sid or not mcp_servers.held_by_session(client, namespace, sid, server, lease_id):
            return None
        import asyncio

        definition = mcp.repository.get(server)
        if definition is None:
            return None
        return asyncio.run(mcp_servers.request_viewer(
            definition, mcp.repository.headers(server), lease_id, True))

    # -- PR previews (previews.py): each click reads the MCP definition it needs ---------
    preview_mgr = None
    if all(env.get(name) for name in GITHUB_APP_ENV):
        from ..previews import PreviewManager

        def preview_token(repo: str) -> Callable[[], str]:
            return InstallationCredential.from_env(
                env, now_ms, target_repos=(repo,),
                permissions={"actions": "read", "pull_requests": "read", "metadata": "read"},
            ).token

        preview_mgr = PreviewManager(
            client=client,
            namespace=namespace,
            repository=mcp.repository,
            github_token=preview_token,
            notify_users=lambda: policy().notify_users,
        )

    engine = Engine(outbounds=transports, agents=runner, policy=policy, confirmer=confirmer,
                    memory=MemoryStore(client, namespace), souls=souls, facts=facts,
                    sandbox_tool=sandbox_tool_fn, on_session_end=on_session_end_fn,
                    server_url=github_server_url(), reachable=repositories,
                    mcp_guidance_fn=mcp.chat_prompt_guidance,
                    share_device_tool_fn=_viewer_for_click,
                    preview_handler=preview_mgr.handle if preview_mgr is not None else None,
                    logger=log)
    return engine, transports, preview_mgr


def warm(engine) -> None:
    """Syncs the mirrors and reads the soul once, so the heartbeat names it before the first
    session. Runs beside the transports."""
    repos = getattr(engine.agents, "repos", None)
    if engine.souls is None or repos is None:
        return
    try:
        repos.refresh(warm=True)  # the first session still fetches what main has then
        engine.souls.read()
    except Exception as exc:  # noqa: BLE001 — the first session tries again
        log.warning("assistant_warm_failed", error=type(exc).__name__, detail=str(exc)[:200])


def run(engine, transports: dict, client, namespace: str, stop: threading.Event, *,
        reload: Callable[[], object], beat_s: float = BEAT_S, health: dict | None = None,
        preview_mgr=None) -> None:
    """Starts the transports and beats until `stop`; then stops them and ends every session."""
    for t in transports.values():
        t.start(engine.on_message, engine.on_action)
    if preview_mgr is not None:
        preview_mgr.start()
    log.info("assistant_started", namespace=namespace, transports=sorted(transports))
    threading.Thread(target=warm, args=(engine,), name="assistant-warm", daemon=True).start()
    last_disabled = None
    try:
        while True:
            cfg = reload()
            engine.sweep()
            enabled = cfg is not None and cfg.assistant_enabled
            if not enabled and (last_disabled is None or time.monotonic() - last_disabled >= LOG_EVERY_S):
                log.info("assistant_disabled")
                last_disabled = time.monotonic()
            try:
                beat(client, namespace, state="running" if enabled else "idle",
                     transports=transports, stats=engine.stats(),
                     soul=engine.souls.active() if engine.souls is not None else None)
                if health is not None:
                    health["at"] = now_ms()
            except redis.RedisError as exc:
                log.warning("assistant_heartbeat_failed", error=str(exc)[:200])
            for t in transports.values():
                code = getattr(t, "fatal_code", None)
                if code:
                    log.warning("assistant_transport_stopped", transport=t.name, code=code)
            if stop.wait(beat_s):
                break
    finally:
        if preview_mgr is not None:
            preview_mgr.stop()
        for t in transports.values():
            try:
                t.stop()
            except Exception as exc:  # noqa: BLE001 — stopping anyway
                log.warning("assistant_transport_stop_failed", transport=t.name,
                            error=type(exc).__name__)
        engine.stop()
        try:
            client.delete(heartbeat_key(namespace))
        except redis.RedisError:
            pass
        log.info("assistant_stopped")


def main() -> None:
    stop = threading.Event()

    def terminate(*_):
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    health: dict = {}
    live: dict = {}  # the engine and transports, once built: `/metrics` reads them

    def collect() -> list:
        if "engine" not in live:
            return []
        return metrics.assistant_families(live["engine"].stats(), live["transports"])

    # Healthy while it waits for setup (a restart would not configure it) and, once running,
    # while its beat loop runs.
    metrics.serve(int(os.environ.get("SYMPHONY_HEALTH_PORT", "8081")),
                  lambda: "at" not in health or now_ms() - health["at"] < STALE_MS,
                  metrics.Scrape("assistant", collect))
    holder = ConfigHolder()
    client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                  socket_timeout=30, health_check_interval=30)

    def load():
        load_config(holder)
        return holder.config

    cfg = wait_until_ready(load, os.environ, stop, client=client, pause=stop.wait)
    if cfg is None:
        return
    namespace = pod_namespace(cfg)
    try:
        engine, transports, preview_mgr = build(holder, client, namespace, os.environ)
    except ConfigError as exc:
        log.warning("assistant_not_configured", missing=["workflow"], error=str(exc)[:200])
        return
    live.update(engine=engine, transports=transports)
    health["at"] = now_ms()
    run(engine, transports, client, namespace, stop, reload=load, health=health,
        preview_mgr=preview_mgr)


if __name__ == "__main__":
    main()
