"""Local mode — `python -m symphony.entry.local --root DIR [--workflow WORKFLOW.md] [--port 8080]`.

One process runs what the Kubernetes deploy spreads over Deployments and Jobs:

- the control API (`/v1`, `/mcp`, webhooks) on `--host`/`--port`, in a uvicorn thread;
- the sandbox gateway (OTLP traces and the MCP proxy) on `127.0.0.1:--gateway-port`, for
  the local engines only;
- the wake-driven tick loop (`daemon.run_loop` + `orchestrator.tick`, with the durable admin
  commands: pause, resume, refresh, reevaluate, interrupt);
- the attempts, as threads driving local `omp --mode rpc` processes (`LocalEffects`).

`REDIS_URL` is required. When no admin token exists yet (and `SYMPHONY_BOOTSTRAP_TOKEN` is
unset) one is created and printed once. With `tracker.kind: memory` the API also mounts
`/v1/dev/issues`, the only way to create and move tickets in that mode.

First-run setup works here as in the cluster (`/v1/setup`, `symphony setup`, Tern):

- integration credentials go to `<root>/secrets.env` (`setup_secrets_local`), loaded at
  start and applied at once on change; a variable set in the host environment wins;
- MCP servers (Admin › MCP) go to `<root>/mcp.json`, mode 0600, secret headers included;
- workflow publications (setup, `PUT /v1/workflow`, durable workflow commands) rewrite the
  `--workflow` file through `workflow_file`, which the tick reloads;
- until the tracker can be built the tick idles: heartbeat, durable commands and
  `daemon:setup` ("incomplete", with the reason) only, no dispatch.

Agents run as the host user, with no pod isolation (docs/install.md, local mode).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import orchestrator, questions, state, wake
from ..admin_auth import Caller, authorize_command
from ..admin_commands import AdminStore, process_commands
from ..api.auth import Authenticator
from ..api.errors import ApiError
from ..apitokens import TokenStore
from ..config import Config, ConfigHolder, bind_role_model
from ..edge import (
    GITHUB_APP_ENV,
    ObservedTracker,
    SetupGate,
    load_config,
    log,
    make_tracker,
    now_ms,
    pod_namespace,
    usable_tracker,
)
from ..effects_local import EngineFactory, LocalEffects, launch_engine
from ..kv import LeaseKV, RedisKV
from ..mcp_servers import McpLaunch, McpRepository
from ..runner import INTENT_BLOCK_MS
from ..sessions import SessionStore
from ..setup_secrets_local import LocalSecretStore
from ..teamconfig import TeamConfig
from ..trackers.base import Issue
from ..trackers.memory import MemoryTracker
from ..workflow_file import file_workflow
from .admin_token import bootstrap_token
from .daemon import _lease_guard, questions_step, run_loop

DEFAULT_PORT = 8080
DEFAULT_GATEWAY_PORT = 8090


# --------------------------------------------------------------------------
# the in-memory board, shared by the API thread, the tick and the attempts
# --------------------------------------------------------------------------
class LockedMemoryTracker:
    """`MemoryTracker` made safe for the threads of one process (API, tick, attempts)."""

    kind = "memory"

    def __init__(self, inner: MemoryTracker) -> None:
        self._inner = inner
        self._lock = threading.RLock()

    def configure(self, cfg: Config) -> None:
        with self._lock:
            self._inner.active_states = list(cfg.active_states)
            self._inner.terminal_states = list(cfg.terminal_states)

    def add_issue(self, issue_id: str, identifier: str, **kw) -> Issue:
        with self._lock:
            if any(i.identifier == identifier for i in self._inner._issues.values()):
                raise KeyError(identifier)
            return self._inner.add_issue(issue_id, identifier, **kw)

    def set_state(self, issue_id: str, state_name: str) -> Issue:
        with self._lock:
            self._inner.set_state(issue_id, state_name, updated_at=_iso_now())
            return self._inner._issues[issue_id]

    def issues(self) -> list[Issue]:
        with self._lock:
            return list(self._inner._issues.values())

    def comments(self, issue_id: str) -> list[str]:
        with self._lock:
            return [c["body"] for c in self._inner.comments.get(issue_id, [])]

    def fetch_issues_by_states(self, states):
        with self._lock:
            found = self._inner.fetch_issues_by_states(states)
            self._inner.fetch_calls.clear()  # a test helper; unbounded in a long-lived process
            return found

    def fetch_issues_by_ids(self, ids):
        with self._lock:
            found = self._inner.fetch_issues_by_ids(ids)
            self._inner.fetch_calls.clear()
            return found

    def agent_tool_specs(self):
        return self._inner.agent_tool_specs()

    def execute_agent_tool(self, name: str, args: dict) -> dict:
        with self._lock:
            return self._inner.execute_agent_tool(name, args)

    def secret_environment_names(self) -> list[str]:
        return self._inner.secret_environment_names()

    def move(self, issue: str, state_name: str) -> bool:
        with self._lock:
            return self._inner.move(issue, state_name)

    def comment(self, issue: str, body: str, *, parent: str | None = None) -> str:
        with self._lock:
            return self._inner.comment(issue, body, parent=parent)

    def person(self, issue: str, hint: str | None = None) -> dict | None:
        with self._lock:
            return self._inner.person(issue, hint)

    def assignee(self, issue: str) -> dict | None:
        with self._lock:
            return self._inner.assignee(issue)

    def upsert_workpad(self, issue: str, body: str) -> str:
        with self._lock:
            return self._inner.upsert_workpad(issue, body)

    def fetch_issue_workpad(self, identifier: str) -> str:
        with self._lock:
            return self._inner.fetch_issue_workpad(identifier)

    def create_sub_issues(self, parent: str, items, *, labels, records) -> list[dict]:
        with self._lock:
            return self._inner.create_sub_issues(parent, items, labels=labels, records=records)

    def attach_evidence(self, issue: str, **kw) -> dict:
        with self._lock:
            return self._inner.attach_evidence(issue, **kw)

    def create_issue(self, title: str, body: str, **kw) -> dict:
        with self._lock:
            return self._inner.create_issue(title, body, **kw)

    def state_of(self, identifier: str) -> dict | None:
        with self._lock:
            return self._inner.state_of(identifier)

    def attachments(self, identifier: str) -> list[dict]:
        with self._lock:
            return self._inner.attachments(identifier)

    def identifier_in(self, text: str, repo: str | None = None) -> str | None:
        with self._lock:
            return self._inner.identifier_in(text, repo)


def _iso_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevIssueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identifier: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
    title: Annotated[str, Field(min_length=1, max_length=500)]
    description: Annotated[str, Field(max_length=20_000)] = ""
    state: Annotated[str, Field(min_length=1, max_length=100)] = "Todo"
    labels: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=50)] = []
    priority: Annotated[int | None, Field(ge=0, le=4)] = None


class DevStateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Annotated[str, Field(min_length=1, max_length=100)]


def _public_issue(issue: Issue, tracker: LockedMemoryTracker) -> dict:
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "description": issue.description,
        "state": issue.state,
        "state_type": issue.state_type,
        "labels": list(issue.labels),
        "priority": issue.priority,
        "target_repo": issue.target_repo,
        "dispatchable": issue.dispatchable,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "comments": tracker.comments(issue.id),
    }


def dev_router(tracker: LockedMemoryTracker, auth: Authenticator, client, namespace: str) -> APIRouter:
    """`/v1/dev/issues`: create, list and move tickets of the in-memory tracker. Mounted
    only by the local mode with `tracker.kind: memory`; every write wakes the tick."""
    router = APIRouter(prefix="/v1/dev")
    viewer = Depends(auth.dependency("viewer"))
    operator = Depends(auth.dependency("operator"))

    @router.get("/issues")
    def list_issues(_: Caller = viewer):
        return {"items": [_public_issue(i, tracker) for i in tracker.issues()]}

    @router.post("/issues", status_code=201)
    def create_issue(body: DevIssueBody, _: Caller = operator):
        now = _iso_now()
        try:
            issue = tracker.add_issue(
                f"dev-{uuid.uuid4().hex[:12]}",
                body.identifier,
                state=body.state,
                title=body.title,
                description=body.description,
                labels=tuple(body.labels),
                priority=body.priority,
                created_at=now,
                updated_at=now,
            )
        except KeyError:
            raise ApiError(409, "conflict", f"ticket {body.identifier} already exists") from None
        wake.wake(client, namespace, wake.DAEMON, f"dev:create:{issue.identifier}")
        return _public_issue(issue, tracker)

    @router.post("/issues/{issue_id}/state")
    def move_issue(issue_id: str, body: DevStateBody, _: Caller = operator):
        try:
            issue = tracker.set_state(issue_id, body.state)
        except KeyError:
            raise ApiError(404, "not_found", "unknown ticket") from None
        wake.wake(client, namespace, wake.DAEMON, f"dev:state:{issue.identifier}")
        return _public_issue(issue, tracker)

    return router


# --------------------------------------------------------------------------
# the stack
# --------------------------------------------------------------------------
def ensure_admin_token(tokens: TokenStore, configured: str | None) -> str | None:
    """Install `SYMPHONY_BOOTSTRAP_TOKEN` when given; otherwise, when no usable admin token
    exists, create one and return it (the caller prints it, once)."""
    if configured:
        tokens.ensure_bootstrap(configured.strip())
        return None
    now = now_ms()
    for record in tokens.list():
        expired = record.get("expires_at") and record["expires_at"] <= now
        if record.get("role") == "admin" and not record.get("revoked_at") and not expired:
            return None
    token = bootstrap_token()
    tokens.ensure_bootstrap(token)
    return token


class _Server:
    """A uvicorn server in its own thread; port 0 picks a free one."""

    def __init__(self, app, host: str, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(
            app, host=host, port=port, log_level="warning", lifespan="off", proxy_headers=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()), name=f"http-{port}", daemon=True
        )
        self.port = port

    def start(self) -> int:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the HTTP server did not start (port in use?)")
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self.port

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


class LocalStack:
    def __init__(
        self,
        *,
        client,
        aclient,
        namespace: str,
        root: str | os.PathLike,
        workflow: str | os.PathLike,
        open_engine: EngineFactory = launch_engine,
        intent_block_ms: int = INTENT_BLOCK_MS,
    ) -> None:
        self.client = client
        self.aclient = aclient
        self.namespace = namespace
        self.workflow = str(Path(workflow).resolve())
        self.root = Path(root).resolve()
        self.kv = RedisKV(client)
        # Before the WORKFLOW loads: the tracker credential may come from the file.
        self.secrets = LocalSecretStore(self.root / "secrets.env", on_change=self._secrets_changed)
        self.mcp = McpRepository(None, namespace, local_path=self.root / "mcp.json")
        self.workflow_repo = file_workflow(
            self.workflow, self.root / "run" / "workflow-annotations.json", namespace,
            on_replace=lambda: wake.wake(client, namespace, wake.DAEMON, "setup:workflow"),
        )
        self.setup = SetupGate()
        self.holder = ConfigHolder()
        load_config(self.holder, self.workflow)
        if self.holder.config is None:
            raise ValueError(f"invalid WORKFLOW.md: {self.holder.last_error}")
        cfg = self.holder.config
        self.memory: LockedMemoryTracker | None = (
            LockedMemoryTracker(MemoryTracker(cfg.active_states, cfg.terminal_states))
            if cfg.tracker_kind == "memory"
            else None
        )
        self.sessions = SessionStore(client, namespace)
        self.team = TeamConfig(client, namespace)
        self.store = AdminStore(client, namespace)
        self.tokens = TokenStore(client, namespace)
        self.fx = LocalEffects(
            cfg,
            kv=self.kv,
            tracker_for=self.tracker,
            sessions=self.sessions,
            team=self.team,
            root=root,
            open_engine=open_engine,
            intent_block_ms=intent_block_ms,
        )
        self.owner = f"local-{uuid.uuid4().hex[:8]}"
        self.stop = threading.Event()
        self._swept = False
        self._servers: list[_Server] = []
        self.api_port: int | None = None
        self.gateway_port: int | None = None

    def _secrets_changed(self, names: set[str]) -> None:
        """A setup write landed in `secrets.env` and the environment: drop the cached App
        installation credentials and tick now, so the change applies without a restart.
        (The GitHub tracker's own credential is keyed by the App environment.)"""
        if names & set(GITHUB_APP_ENV):
            self.fx.reset_credentials()
        wake.wake(self.client, self.namespace, wake.DAEMON, "setup:secrets")

    def tracker(self, cfg: Config):
        if cfg.tracker_kind != "memory":
            return make_tracker(cfg)
        if self.memory is None:
            raise RuntimeError("tracker.kind changed to memory while the process runs: restart it")
        self.memory.configure(cfg)
        return ObservedTracker(self.memory, cfg.required_labels)

    # -- HTTP --------------------------------------------------------------
    def api_app(self):
        from ..api import build_app, build_services

        services = build_services(
            self.client, self.aclient, self.namespace, kv=self.kv, core=None,
            secrets=self.secrets, workflow=self.workflow_repo, mcp=self.mcp,
        )
        app = build_app(services)
        if self.memory is not None:
            auth = Authenticator(
                self.client,
                self.namespace,
                services.tokens,
                per_minute=int(os.environ.get("SYMPHONY_API_RATE_LIMIT", "600")),
            )
            app.include_router(dev_router(self.memory, auth, self.client, self.namespace))
        return app

    def start_http(self, host: str, port: int, gateway_port: int | None) -> None:
        api = _Server(self.api_app(), host, port)
        self.api_port = api.start()
        self._servers.append(api)
        if gateway_port is not None:
            from ..gateway_api import build_app as build_gateway

            gateway = _Server(
                build_gateway(lambda: self.kv, namespace=self.namespace, mcp=self.mcp),
                "127.0.0.1", gateway_port,
            )
            self.gateway_port = gateway.start()
            self._servers.append(gateway)
            # The engines reach MCP servers only through this gateway (internals section 18).
            self.fx.mcp = McpLaunch(
                self.client, self.namespace, self.mcp, f"http://127.0.0.1:{self.gateway_port}"
            )

    # -- the tick ------------------------------------------------------------
    def step(self, reasons: list[str]) -> bool:
        now = now_ms()
        load_config(self.holder, self.workflow)  # hot reload, last-known-good kept
        if not state.renew_lease(self.kv, self.owner, now):
            log.info("lease_held_by_other_waiting")
            return False
        try:
            team_doc = self.team.get()
            cfg = bind_role_model(self.holder.config, team_doc, "implementer")
            self.fx.cfg = cfg
            self.team.observe("daemon", int(team_doc["generation"]))
            with _lease_guard(self.kv, self.owner) as authoritative:
                self.fx.authoritative = authoritative
                state.heartbeat(self.kv, now_ms())
                if self.holder.accepted_document_hash:
                    self.store.observe_workflow("daemon", self.holder.accepted_document_hash)
                    if self.holder.rejected_document_hash:
                        self.store.observe_workflow(
                            "daemon", self.holder.accepted_document_hash, status="rejected",
                            message="A newer workflow file was rejected; last accepted configuration remains active.",
                        )
                if cfg.tracker_kind == "memory":
                    # The board this process's dev routes write; always usable.
                    tracker, ready = self.tracker(cfg), self.setup.update(self.kv, now_ms(), None)
                else:
                    tracker, ready = usable_tracker(cfg, self.kv, self.setup, now_ms())
                # Durable commands still apply while setup is incomplete; tracker-bound
                # ones fail with 409 instead of stalling the queue.
                process_commands(
                    self.store, self.kv, tracker, cfg, self.fx, self.owner, now,
                    publish_workflow=self.workflow_repo.publish,
                    authorize=lambda command: authorize_command(command, self.namespace, self.tokens),
                )
                if ready and authoritative():
                    tick_kv = LeaseKV(self.kv, self.owner)
                    if not self._swept:
                        orchestrator.startup_sweep(tick_kv, tracker, cfg, now_ms(), self.fx)
                        self._swept = True
                    result = orchestrator.tick(
                        tick_kv, tracker, cfg, now_ms(), self.fx, self.owner,
                        admission_open=lambda: bool(authoritative() and not self.store.paused()),
                        wake_reasons=reasons,
                        own_comment=lambda comment_id: questions.is_own(
                            self.kv.client, self.namespace, comment_id),
                    )
                    questions_step(self.kv, self.namespace, tracker, cfg)
                    log.info("tick", wakes=len(reasons), **result)
        except Exception as exc:  # noqa: BLE001 — a tick never brings the stack down
            log.error("tick_failed", error=str(exc))
        return True

    def run_loop(self) -> None:
        run_loop(
            self.step,
            client=self.client,
            namespace=self.namespace,
            interval_s=lambda: self.holder.config.polling_interval_ms / 1000,
            stop=self.stop,
        )

    def request_stop(self) -> None:
        self.stop.set()
        try:
            wake.wake(self.client, self.namespace, wake.DAEMON, "shutdown")  # unblock the wait
        except Exception:  # noqa: BLE001 — the loop's own timeout still ends it
            pass

    def shutdown(self) -> None:
        self.request_stop()
        self.fx.close()
        for server in reversed(self._servers):
            server.stop()
        self._servers.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m symphony.entry.local",
        description="Symphony in local mode: API, tick and agents on this machine, without Kubernetes.",
    )
    parser.add_argument("--root", required=True, help="directory for workspaces, sessions and logs")
    parser.add_argument("--workflow", default="WORKFLOW.md", help="WORKFLOW.md file (default: ./WORKFLOW.md)")
    parser.add_argument("--host", default="127.0.0.1", help="API address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT,
                        help="agent gateway port, always on 127.0.0.1 (default: 8090)")
    args = parser.parse_args(argv)

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("error: set REDIS_URL (for example redis://127.0.0.1:6379/0)", file=sys.stderr)
        return 2
    workflow = Path(args.workflow).resolve()
    if not workflow.is_file():
        print(f"error: {workflow} does not exist", file=sys.stderr)
        return 2
    if workflow.name == "WORKFLOW.md":
        # `notify` reads `notify.users` from the mount directory.
        os.environ.setdefault("SYMPHONY_WORKFLOW_MOUNT", str(workflow.parent))

    import redis.asyncio

    namespace = pod_namespace()
    kv = RedisKV(url=redis_url)
    # SSE holds a blocking XREAD per viewer; its socket timeout must outlast the 15 s block.
    aclient: Any = redis.asyncio.Redis.from_url(
        redis_url, decode_responses=True, socket_timeout=60, socket_connect_timeout=5
    )
    try:
        stack = LocalStack(
            client=kv.client, aclient=aclient, namespace=namespace, root=args.root, workflow=workflow
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    token = ensure_admin_token(stack.tokens, os.environ.get("SYMPHONY_BOOTSTRAP_TOKEN"))
    stack.start_http(args.host, args.port, args.gateway_port)
    # The engines' telemetry route: this process's own gateway.
    os.environ.setdefault("SYMPHONY_GATEWAY_URL", f"http://127.0.0.1:{stack.gateway_port}")
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{stack.api_port}"
    print(f"Symphony local at {url} (root {Path(args.root).resolve()}, tracker {stack.holder.config.tracker_kind})",
          flush=True)
    if token:
        print(
            "Admin token created. It is shown only once; save it now:\n"
            f"  {token}\n"
            f"Login: symphony login --url {url} --token -",
            flush=True,
        )

    def on_signal(signum, frame):
        log.info("local_stack_stopping", signal=signal.Signals(signum).name)
        stack.request_stop()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    log.info("local_stack_started", port=stack.api_port, gateway_port=stack.gateway_port,
             namespace=namespace)
    try:
        stack.run_loop()
    finally:
        stack.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
