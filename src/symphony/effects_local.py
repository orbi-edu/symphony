"""LocalEffects — orchestrator.Effects with every attempt running on this host.

The local mode's counterpart of `K8sEffects`, primitive by primitive:

    spawn_runner        Job create            →  a thread running `runner.run_attempt`
    cancel_runner       Job delete            →  SIGTERM the engine's process group, join
    is_call_running     Job conditions        →  the attempt thread is alive
    kill_agent_process  pod exec pkill        →  SIGTERM/SIGKILL the engine's process group
    cleanup_workspace   hook + pod/PVC delete →  hook + remove the workspace and session dirs
    release_workspace   idle annotation       →  engine stopped, directories kept
    spawn_reflector     reflector Job         →  skipped, with an explicit log line

Layout under the root: `workspaces/<workspace_key>/` (the attempt's working directory, where
the hooks run), `sessions/<sandbox_name>/` (omp `--session-dir`), `logs/` (engine stderr),
`run/<sandbox_name>.pgid` (the live engine's process group, so a restart can stop a leftover
before reusing the workspace) and `credentials/<sandbox_name>/` (the attempt's GitHub App
token and the git credential helper that reads it, removed when the attempt ends).

With `GITHUB_APP_ID` set, each attempt gets the same credential as a workspace pod: an
installation token scoped to the ticket's repositories (read-only for an investigation),
minted here and handed to git through a helper that reads it from a file, never through a URL
or the environment. Without it, git uses the host user's own credentials.

MCP servers (Admin › MCP, role `implementer`) reach the engine without touching the host's
omp agent dir: `credentials/<sandbox_name>/mcp/` holds a plugin (`--plugin-dir`) whose servers
point at this process's gateway with the attempt's own MCP token, and a `--config` overlay
that turns off the repository's `.omp/mcp.json` (`mcp_servers.stage_overlay`). The gateway
adds the stored headers; the token is revoked when the attempt ends.

There is no isolation: the engine and its tools run as the host user and inherit its
environment, git credentials and omp configuration. Only the Symphony service secrets below
are removed from what the engine and the hooks see — hygiene, not a boundary (any process of
the same user can read them from this one).
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import codehost, ghapp, state
from . import runner as runner_mod
from .config import Config, base_branches_env
from .edge import log, now_ms
from .entry.run_issue import engine_env, engine_launch, installation_credential
from .kube import runner_job_name
from .mcp_servers import McpLaunch, overlay_args
from .omp_rpc import OmpClient, OmpError
from .sessions import SessionStore
from .teamconfig import TeamConfig
from .trackers.base import Issue
from .workspace import (
    HookFatal,
    LocalEngine,
    LocalSandbox,
    run_hook,
    sandbox_name,
    workspace_key,
    workspace_matches_repo,
)

# Credentials of the Symphony services themselves. The tracker's own names are added per
# tracker (`secret_environment_names`).
SERVICE_SECRETS = (
    "REDIS_URL",
    "LINEAR_API_KEY",
    "LINEAR_WEBHOOK_SECRET",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "SYMPHONY_BOOTSTRAP_TOKEN",
    "SYMPHONY_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_WEBHOOK_URL",
    "DISCORD_BOT_TOKEN",
    "DISCORD_WEBHOOK_URL",
)
# Puts the engine in its own session (and process group) before exec, so stopping a
# generation reaches the tools it spawned and a Ctrl-C on the stack's terminal does not.
_SETSID = "import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])"
GROUP_TERM_GRACE_S = 5.0
CANCEL_JOIN_S = 30.0

EngineFactory = Callable[[list[str], str, dict[str, str], str], Any]


def launch_engine(argv: list[str], cwd: str, env: dict[str, str], stderr_path: str) -> LocalEngine:
    """The engine as a session leader: its pid is its process group id."""
    return LocalEngine([sys.executable, "-c", _SETSID, *argv], cwd, env, stderr_path)


def stop_process_group(pgid: int, grace_s: float = GROUP_TERM_GRACE_S) -> bool:
    """SIGTERM the group, SIGKILL what survives the grace; True once the group is gone.

    On macOS a group whose last members exited can still answer `EPERM`: a zombie that
    launchd adopted keeps the group id, and we may not signal it. None of our processes is
    left, so that counts as gone, the same as `ESRCH`."""
    for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return True
            time.sleep(0.05)
    return False


class HostSandbox(LocalSandbox):
    """`LocalSandbox` whose scripts see the host environment minus the service secrets."""

    def __init__(self, root: str, name: str, env: dict[str, str], base_env: dict[str, str]) -> None:
        super().__init__(root, name, env)
        self._base_env = base_env

    def exec_script(self, script: str, timeout_s: float | None = None) -> tuple[int, str]:
        try:
            done = subprocess.run(
                ["bash", "-lc", script],
                cwd=self.root,
                env={**self._base_env, **self.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output or ""
            return 124, out if isinstance(out, str) else out.decode("utf-8", "replace")
        return done.returncode, done.stdout


class _CancellableKV:
    """The attempt's view of the store: once cancelled it holds no dispatch token and every
    fenced write fails, exactly as a deleted Job's runner can write nothing. Cancellation is
    set before the engine is stopped, so the runner's reaction to the dead engine is fenced."""

    def __init__(self, base: state.KV, cancelled: threading.Event) -> None:
        self._base = base
        self._cancelled = cancelled

    def __getattr__(self, name):
        return getattr(self._base, name)

    def get(self, key, default=None):
        if self._cancelled.is_set() and key.startswith(state.DISPATCH_PREFIX):
            return default
        return self._base.get(key, default)

    def fenced_put(self, key, value, expected, token_key) -> bool:
        if self._cancelled.is_set():
            return False
        return state.fenced_put(self._base, key, value, expected, token_key)


@dataclass
class _Attempt:
    call_id: str
    issue_id: str
    seq: int
    sandbox_name: str
    thread: threading.Thread | None = None
    engine: Any = None
    pgid: int | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    installation: ghapp.InstallationCredential | None = None
    git_env: dict[str, str] = field(default_factory=dict)
    mcp_tokens: list[str] = field(default_factory=list)

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class LocalEffects:
    def __init__(
        self,
        cfg: Config,
        *,
        kv: state.KV,
        tracker_for: Callable[[Config], Any],
        sessions: SessionStore,
        team: TeamConfig,
        root: str | os.PathLike,
        open_engine: EngineFactory = launch_engine,
        clock: Callable[[], int] = now_ms,
        intent_block_ms: int = runner_mod.INTENT_BLOCK_MS,
        mcp: McpLaunch | None = None,
    ) -> None:
        # Set by the stack once its gateway listens; None stages no MCP servers.
        self.mcp = mcp
        self.cfg = cfg
        self.kv = kv
        self._tracker_for = tracker_for
        self.sessions = sessions
        self.team = team
        self.root = Path(root).resolve()
        self._open_engine = open_engine
        self._now = clock
        self._intent_block_ms = intent_block_ms
        self.authoritative = lambda: True
        self._attempts: dict[str, _Attempt] = {}
        self._lock = threading.Lock()
        self._credentials: dict[str, ghapp.InstallationCredential] = {}
        self._reader = codehost.GitHubReader(self._installation_token)
        for sub in ("workspaces", "sessions", "logs", "run", "credentials"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # -- layout --------------------------------------------------------------
    def workspace_dir(self, identifier: str) -> Path:
        return self.root / "workspaces" / workspace_key(identifier)

    def session_dir(self, name: str) -> Path:
        return self.root / "sessions" / name

    def _pgid_file(self, name: str) -> Path:
        return self.root / "run" / f"{name}.pgid"

    def credential_dir(self, name: str) -> Path:
        return self.root / "credentials" / name

    def _base_env(self, tracker) -> dict[str, str]:
        hidden = set(SERVICE_SECRETS) | set(tracker.secret_environment_names())
        return {k: v for k, v in os.environ.items() if k not in hidden}

    def _require_lease(self) -> None:
        if not self.authoritative():
            raise RuntimeError("Daemon lease lost before external effect")

    # -- the attempt ---------------------------------------------------------
    def spawn_runner(self, issue: Issue, attempt: int, seq: int) -> None:
        self._require_lease()
        call_id = runner_job_name(issue.identifier, seq)
        with self._lock:
            current = self._attempts.get(call_id)
            if current is not None and current.alive():
                return  # the same dispatch, already running: a duplicate create is a no-op
            record = _Attempt(call_id, issue.id, seq, sandbox_name(issue.identifier))
            record.thread = threading.Thread(
                target=self._run, args=(record, attempt), name=f"attempt-{call_id}", daemon=True
            )
            self._attempts[call_id] = record
        record.thread.start()

    def _run(self, record: _Attempt, attempt: int) -> None:
        cfg = self.cfg
        tracker = self._tracker_for(cfg)
        started = self._now()
        env = runner_mod.RunnerEnv(
            kv=_CancellableKV(self.kv, record.cancelled),
            tracker=tracker,
            cfg=cfg,
            now_ms=self._now,
            call_id=record.call_id,
            ensure_sandbox=lambda issue, bound: self._ensure_sandbox(record, tracker, issue, bound),
            open_client=lambda sandbox, token, issue, bound: self._open_client(
                record, tracker, sandbox, token, issue, bound
            ),
            kill_agent=lambda sandbox: self._stop_generation(record),
            release_sandbox=lambda sandbox: log.info("workspace_released", sandbox=sandbox.name),
            wall_deadline_ms=runner_mod.compute_env_wall_deadline(started, cfg),
            sessions=self.sessions,
            team=self.team,
            sleep_s=time.sleep,
            refresh_credentials=lambda sandbox: self._refresh_credential(record),
            log=lambda event, **kw: log.warning(event, **kw),
            intent_block_ms=self._intent_block_ms,
            mcp_guidance=lambda: self.mcp.prompt_guidance("implementer") if self.mcp is not None else "",
        )
        try:
            outcome = runner_mod.run_attempt(env, record.issue_id, attempt, record.seq)
            entry = self.kv.get(state.issue_key(record.issue_id)) or {}
            log.info(
                "runner_finished",
                issue_id=record.issue_id,
                attempt=attempt,
                seq=record.seq,
                outcome=outcome,
                reason=entry.get("failure_summary"),
            )
        except Exception as exc:  # noqa: BLE001 — recovery is the tick's (stall/orphan paths)
            log.error("runner_crashed", issue_id=record.issue_id, seq=record.seq, error=str(exc))
        finally:
            try:
                self._stop_generation(record)
            except Exception as exc:  # noqa: BLE001
                log.error("agent_stop_failed", issue_id=record.issue_id, error=str(exc))
            # The token dies with the attempt, as it does with a workspace pod's Job.
            if self.mcp is not None:
                for mcp_token in record.mcp_tokens:
                    self.mcp.end(mcp_token)
            shutil.rmtree(self.credential_dir(record.sandbox_name), ignore_errors=True)

    # -- the code-hosting credential ------------------------------------------
    def _install_credential(self, record: _Attempt, issue: Issue, bound: Config) -> dict[str, str]:
        """Mint the attempt's App token (scoped like a pod's, `run_issue.installation_credential`)
        and return the git environment that points git at it; {} when no App is configured."""
        record.installation, record.git_env = None, {}
        if not os.environ.get("GITHUB_APP_ID"):
            return {}
        installation = installation_credential(issue, bound, os.environ, self._now)
        if installation is None:
            return {}
        directory = self.credential_dir(record.sandbox_name)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self._write_token(directory, installation.token())
        helper = directory / "git-credential-symphony"
        helper.write_text(ghapp.credential_helper_script(shlex.quote(str(directory / "gh-token"))))
        os.chmod(helper, 0o700)
        record.installation = installation
        # Command-scope config: the empty value drops every helper the host's system and
        # global config declare, so git asks this one only. `!` runs it through the shell,
        # which keeps a root with spaces working.
        record.git_env = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "!" + shlex.quote(str(helper)),
        }
        return record.git_env

    @staticmethod
    def _write_token(directory: Path, token: str) -> None:
        staged = directory / "gh-token.new"
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        os.replace(staged, directory / "gh-token")  # git never reads a half-written token

    def _refresh_credential(self, record: _Attempt) -> bool:
        """Installation tokens live an hour; attempts run longer (FR-017)."""
        installation = record.installation
        if installation is None or not installation.needs_refresh():
            return True
        try:
            self._write_token(self.credential_dir(record.sandbox_name), installation.token(force_refresh=True))
            return True
        except Exception as exc:  # noqa: BLE001 — reported as its own failure reason
            log.error("credential_refresh_failed", error=str(exc))
            return False

    def _ensure_sandbox(self, record: _Attempt, tracker, issue: Issue, bound: Config) -> HostSandbox:
        if record.cancelled.is_set():
            raise OmpError("attempt cancelled before the workspace was ready")
        repos = runner_mod.target_repos(issue)
        path = self.workspace_dir(issue.identifier)
        # Before any hook: `after_create` clones with it.
        git_env = self._install_credential(record, issue, bound)
        bb_env = base_branches_env(bound, repos)
        sandbox = HostSandbox(
            str(path),
            record.sandbox_name,
            {
                "SYMPHONY_TARGET_REPO": repos[0] if repos else "",
                "SYMPHONY_TARGET_REPOS": " ".join(repos),
                "SYMPHONY_WORKSPACE": str(path),
                **({"SYMPHONY_BASE_BRANCHES": bb_env} if bb_env else {}),
                **git_env,
            },
            self._base_env(tracker),
        )
        # Two engines in one workspace corrupt the session: a leftover from a previous
        # process of this stack goes before anything touches the directory.
        self._stop_leftover(record.sandbox_name)
        created = not path.exists()
        if not created and repos and not workspace_matches_repo(sandbox, str(path), repos[0]):
            # Same rule as the pod path: `after_create` owns the clone and only runs on
            # creation, so a workspace of another repository is rebuilt, never reused.
            log.warning("workspace_repo_mismatch_recreating", workspace=str(path), target_repo=repos[0])
            shutil.rmtree(path)
            created = True
        path.mkdir(parents=True, exist_ok=True)
        hook_log = lambda event, **kw: log.warning(event, **kw)  # noqa: E731
        if created:
            try:
                run_hook(sandbox, "after_create", bound, log=hook_log)
            except HookFatal:
                # A half-built workspace must not be mistaken for a ready one next time.
                shutil.rmtree(path, ignore_errors=True)
                raise
        run_hook(sandbox, "before_run", bound, log=hook_log)
        return sandbox

    def _open_client(
        self, record: _Attempt, tracker, sandbox: HostSandbox, token: str, issue: Issue, bound: Config
    ) -> OmpClient:
        env = {
            **self._base_env(tracker),
            **engine_env(bound, issue.id, record.seq, token, runner_mod.target_repos(issue)),
            "SYMPHONY_WORKSPACE": sandbox.root,
            **record.git_env,
        }
        launch = engine_launch(bound, str(self.session_dir(sandbox.name)))
        if self.mcp is not None:
            directory = self.credential_dir(sandbox.name) / "mcp"
            ttl_s = int(bound.runner.get("wallclock_guard_s", 12_600)) + 600
            mcp_token = self.mcp.stage_overlay(directory, "implementer", f"{issue.id}:{record.seq}", ttl_s)
            record.mcp_tokens.append(mcp_token)
            # The servers' `agent_env`: the session token and gateway URLs, for this engine only.
            env.update(self.mcp.agent_env("implementer", mcp_token))
            launch += " " + " ".join(shlex.quote(arg) for arg in overlay_args(directory))
        argv = ["bash", "-c", launch]
        stderr_path = str(self.root / "logs" / f"{sandbox.name}-s{record.seq}.log")
        with record.lock:
            if record.cancelled.is_set():
                raise OmpError("attempt cancelled before the engine started")
            engine = self._open_engine(argv, sandbox.root, env, stderr_path)
            record.engine = engine
            proc = getattr(engine, "proc", None)
            record.pgid = proc.pid if proc is not None else None
            if record.pgid is not None:
                self._pgid_file(record.sandbox_name).write_text(f"{record.pgid}\n")
        return OmpClient(engine, engine, engine)

    def _stop_generation(self, record: _Attempt) -> None:
        """Stop the attempt's engine (SIGTERM first, so omp flushes its last span batch)
        and everything left in its process group; raises when that cannot be confirmed."""
        with record.lock:
            engine, pgid = record.engine, record.pgid
        if engine is not None:
            try:
                engine.terminate(runner_mod.ENGINE_STOP_GRACE_S)
            except Exception as exc:  # noqa: BLE001 — the group kill below still runs
                log.warning("engine_terminate_failed", call_id=record.call_id, error=str(exc))
        if pgid is not None:
            if not stop_process_group(pgid):
                raise RuntimeError("Agent generation termination could not be confirmed")
            pidfile = self._pgid_file(record.sandbox_name)
            try:
                if pidfile.read_text().strip() == str(pgid):
                    pidfile.unlink()
            except FileNotFoundError:
                pass

    def _stop_leftover(self, name: str) -> None:
        """Stop the engine group recorded for a workspace by a previous run of the stack."""
        pidfile = self._pgid_file(name)
        try:
            pgid = int(pidfile.read_text().strip())
        except (FileNotFoundError, ValueError):
            return
        with self._lock:
            owned = any(r.pgid == pgid and r.alive() for r in self._attempts.values())
        if owned:
            return  # a live attempt of this process; its own lifecycle stops it
        if not stop_process_group(pgid):
            raise RuntimeError("Agent process termination could not be confirmed")
        pidfile.unlink(missing_ok=True)

    def cancel_runner(self, call_id: str, *, issue_id: str, seq: int) -> None:
        with self._lock:
            record = self._attempts.get(call_id)
        if record is None or not record.alive():
            return
        if record.issue_id != issue_id or record.seq != seq:
            raise RuntimeError("Refusing to cancel an unowned runner")
        self._require_lease()
        record.cancelled.set()
        self._stop_generation(record)
        record.thread.join(CANCEL_JOIN_S)
        if record.alive():
            raise RuntimeError("Runner termination is not yet confirmed")

    def is_call_running(self, call_id: str) -> bool:
        with self._lock:
            record = self._attempts.get(call_id)
        return record is not None and record.alive()

    def kill_agent_process(
        self, sandbox_name_: str | None, *, issue_id: str | None = None, seq: int | None = None
    ) -> None:
        self._require_lease()
        with self._lock:
            if issue_id is not None and seq is not None:
                records = [r for r in self._attempts.values() if (r.issue_id, r.seq) == (issue_id, seq)]
            else:
                records = [r for r in self._attempts.values() if r.sandbox_name == sandbox_name_]
        for record in records:
            self._stop_generation(record)
        if sandbox_name_:
            self._stop_leftover(sandbox_name_)

    def cleanup_workspace(self, issue_id: str, entry: dict) -> None:
        identifier = entry.get("identifier")
        if not identifier:
            return
        name = entry.get("sandbox_name") or sandbox_name(identifier)
        self._require_lease()
        self.kill_agent_process(name)
        path = self.workspace_dir(identifier)
        if path.exists():
            tracker = self._tracker_for(self.cfg)
            sandbox = HostSandbox(str(path), name, {"SYMPHONY_WORKSPACE": str(path)}, self._base_env(tracker))
            try:
                # A hook failure must never block cleanup (FR-015).
                run_hook(sandbox, "before_remove", self.cfg, log=lambda e, **kw: log.warning(e, **kw))
            except Exception as exc:  # noqa: BLE001
                log.warning("before_remove_exec_failed_ignored", error=str(exc))
            shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(self.session_dir(name), ignore_errors=True)
        shutil.rmtree(self.credential_dir(name), ignore_errors=True)
        with self._lock:
            for call_id in [c for c, r in self._attempts.items() if r.issue_id == issue_id and not r.alive()]:
                del self._attempts[call_id]

    def release_workspace(self, issue_id: str, entry: dict) -> None:
        """Claim released while KEEPING the workspace (FR-014): no engine survives it, and
        the directories stay for the next dispatch of the ticket."""
        name = entry.get("sandbox_name")
        if name:
            self._require_lease()
            self.kill_agent_process(name)

    def spawn_reflector(self, issue_id: str, entry: dict) -> None:
        log.warning(
            "reflector_skipped_local_mode",
            issue_id=issue_id,
            identifier=entry.get("identifier"),
            reason="the local mode runs no reflector; retrospectives need the Kubernetes deploy",
        )

    def close(self) -> None:
        """Stop every running attempt (the stack is shutting down)."""
        with self._lock:
            records = [r for r in self._attempts.values() if r.alive()]
        for record in records:
            record.cancelled.set()
            try:
                self._stop_generation(record)
            except Exception as exc:  # noqa: BLE001 — keep stopping the others
                log.error("agent_stop_failed", issue_id=record.issue_id, error=str(exc))
        for record in records:
            record.thread.join(CANCEL_JOIN_S)

    # -- code host and tracker ------------------------------------------------
    def _installation_token(self, repo: str) -> str:
        cred = self._credentials.get(repo)
        if cred is None:
            cred = ghapp.InstallationCredential.from_env(os.environ, now_ms, target_repos=(repo,))
            self._credentials[repo] = cred
        return cred.token()

    def reset_credentials(self) -> None:
        """The GitHub App credentials changed (local setup): mint from the new ones."""
        self._credentials.clear()

    def open_pulls(self, repo: str) -> list[dict]:
        return self._reader.open_pulls(repo)

    def pr_conversation(self, repo: str, number: int) -> list[dict]:
        return self._reader.conversation(repo, number)

    def emit(self, event: str, **fields) -> None:
        log.info(event, **fields)

    def report_to_ticket(self, issue_id: str, message: str) -> None:
        """Best effort, through the tracker adapter (same as `K8sEffects`)."""
        try:
            self._tracker_for(self.cfg).comment(issue_id, message)
        except Exception as exc:  # noqa: BLE001 — visibility is not worth the daemon
            log.warning("ticket_report_failed", issue_id=issue_id, error=str(exc))
