"""The assistant's agents: one `omp --mode rpc` process per chat thread, with read-only tools.

It reuses the merger's `AgentRunner` wire code (`merger/agents.py`): the frames go to the
session store, so Tern can follow and steer an `assist` session like any other. Unlike a merger
agent, one process serves many turns: each chat message is one `prompt`, and the process stays
until the engine ends the session (30 min idle).

- **Agent dir:** private, per session: the role's `models.yml` (a key saved in Admin) and the
  MCP config of the role (`mcp_servers.stage_mcp`, through the gateway with a short-lived
  token). No user-level or project-level omp config applies.
- **Tools:** `read,grep,glob,web_search`, the host tools of the role, and the role's MCP
  tools. No `bash`, `edit` or `write`.
- **Working dir:** a worktree of each `assistant.repositories` repository at its default branch
  (`<root>/work/<owner>/<name>`), fetched at most every 5 minutes from a mirror with a
  read-only GitHub App token. Any other reachable repository is checked out on demand
  (`open_repository`) and kept while it is used. The `assistant.knowledge` repositories are
  always checked out, and indexed when their commit changes (`knowledge.py`).
- **Environment:** the pod's, without the chat, tracker and GitHub App credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable

from .. import mcp_servers
from ..config import role_model
from ..merger.agents import AgentFailed, AgentRunner, AgentSpec, HostTool, _Run
from ..merger.git import Mirror
from ..omp_rpc import OmpError
from ..teamconfig import model_arg
from .engine import TurnResult

# omp 18.20.2 `--tools` names. `find` is semantic search and needs a native judge model, so on
# a Bedrock-only session omp exits 2; `glob` finds files by name.
TOOLS = ("read", "grep", "glob", "web_search")
MIRROR_REFRESH_S = 300.0
# A repository opened on demand stays checked out this long after its last use.
OPENED_IDLE_S = 86_400.0
ABORT_GRACE_S = 30.0
# Host credentials no agent process needs. The model credential (AWS_*, provider keys) stays.
_HOST_ONLY_PREFIXES = ("SLACK_", "DISCORD_", "GITHUB_APP_", "LINEAR_", "SYMPHONY_ADMIN_",
                       "SYMPHONY_API_")
_HOST_ONLY_NAMES = frozenset({"REDIS_URL", "MEMORY_TRACKER_FAKE_KEY"})


def agent_environment(environ: dict) -> dict:
    return {
        k: v for k, v in environ.items()
        if k not in _HOST_ONLY_NAMES and not k.startswith(_HOST_ONLY_PREFIXES)
    }


def default_repositories(client, namespace: str, cfg) -> list[str]:
    """Every repository Symphony routes work to: the default repository, the repositories a
    merger serves, and those of the tracked tickets."""
    from .. import state
    from ..merger.store import registered_repos

    repos = {cfg.default_repo.lower()} if cfg.default_repo else set()
    repos.update(r.lower() for r in registered_repos(client, namespace))
    keys = list(client.scan_iter(match=f"{state.ISSUE_PREFIX}*", count=1000))
    for raw in client.mget(keys) if keys else []:
        try:
            entry = json.loads(raw) if raw else None
        except ValueError:
            continue
        repo = entry.get("target_repo") if isinstance(entry, dict) else None
        if isinstance(repo, str) and repo.count("/") == 1:
            repos.add(repo.lower())
    return sorted(repos)


def reachable_repositories(client, namespace: str, cfg) -> list[str]:
    """What the assistant may open: the repositories Symphony routes work to, plus every
    repository the GitHub App reaches that is not archived (the daemon's discovery)."""
    from ..discovery import DISCOVERY_KEY

    repos = set(default_repositories(client, namespace, cfg))
    try:
        doc = json.loads(client.get(DISCOVERY_KEY) or "{}")
    except ValueError:
        doc = {}
    for item in doc.get("items") or []:
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.count("/") == 1 and not item.get("archived"):
            repos.add(name.lower())
    return sorted(repos)


class ReadOnlyRepos:
    """`<root>/work/<owner>/<name>`: one worktree per repository at its default branch.

    Mirrors live in `<root>/mirrors/<owner>/<name>`. A refresh happens at most every
    `refresh_s`: it fetches and checks the new commit out in place. A repository that fails
    to sync is left out (and logged); the others still serve. The `knowledge` bases'
    repositories are always synced, and each refresh hands the synced commits to
    `Knowledge.update`, which indexes a base whose commit changed."""

    def __init__(
        self,
        root: str,
        repos: Callable[[], Iterable[str]],
        *,
        server_url: str,
        token_for: Callable[[str], Callable[[], str]] | None = None,
        base_branch_for: Callable[[str], str | None] | None = None,
        refresh_s: float = MIRROR_REFRESH_S,
        clock: Callable[[], float] = time.monotonic,
        logger=None,
        knowledge=None,  # knowledge.Knowledge
    ) -> None:
        self.root = root
        self.work = os.path.join(root, "work")
        self.repos = repos
        self.server_url = server_url.rstrip("/")
        self.token_for = token_for
        self.base_branch_for = base_branch_for
        self.refresh_s = refresh_s
        self.clock = clock
        self.log = logger
        self.knowledge = knowledge
        self._mirrors: dict[str, Mirror] = {}
        self._shas: dict[str, str] = {}
        self._at: float | None = None
        self._lock = threading.Lock()
        self._opened: dict[str, float] = {}  # repository opened on demand → last use

    def open(self, repo: str) -> str:
        """Check `repo` out now and keep it through later refreshes; returns its commit."""
        repo = repo.lower()
        with self._lock:
            if repo not in self._shas:
                self._sync(repo)  # raises when it cannot be fetched: then it is not kept
            self._opened[repo] = self.clock()
            return self._shas[repo]

    def commits(self) -> dict[str, str]:
        """repository → the commit its worktree is at."""
        with self._lock:
            return dict(self._shas)

    def blob(self, repo: str, path: str, limit: int) -> tuple[str, bytes] | None:
        """(blob sha, at most `limit` bytes) of `path` at the commit of the last refresh of
        the default branch, or None when the repository or the file is not there."""
        repo = repo.lower()
        with self._lock:
            mirror, commit = self._mirrors.get(repo), self._shas.get(repo)
        if mirror is None or commit is None:
            return None
        found = mirror.run("rev-parse", "--verify", "-q", f"{commit}:{path}", check=False)
        sha = found.stdout.strip()
        if found.returncode != 0 or mirror.run("cat-file", "-t", sha, check=False).stdout.strip() != "blob":
            return None
        with subprocess.Popen(["git", "cat-file", "blob", sha], cwd=mirror.path,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            data = proc.stdout.read(limit)
            proc.kill()
        return sha, data

    def refresh(self, *, force: bool = False, warm: bool = False) -> str:
        """Syncs every repository at most every `refresh_s`. A `warm` sync (at start) syncs
        now without starting that window, so the first session still fetches."""
        with self._lock:
            now = self.clock()
            if force or warm or self._at is None or now - self._at >= self.refresh_s:
                if not warm:
                    self._at = now
                for repo, used in list(self._opened.items()):
                    if now - used >= OPENED_IDLE_S:
                        del self._opened[repo]
                knowledge = self.knowledge.repos() if self.knowledge is not None else []
                wanted = list(dict.fromkeys([*(r.lower() for r in self.repos()), *knowledge,
                                             *self._opened]))
                for repo in wanted:
                    try:
                        self._sync(repo)
                    except Exception as exc:  # noqa: BLE001 — one repository's failure
                        self._shas.pop(repo, None)
                        if self.log is not None:
                            self.log.warning("assistant_mirror_failed", repo=repo,
                                             error=type(exc).__name__, detail=str(exc)[:200])
                for repo in set(self._shas) - set(wanted):
                    self._drop(repo)
                if self.knowledge is not None:
                    self.knowledge.update({r: (self._mirrors[r].path, self._shas[r])
                                           for r in knowledge if r in self._shas})
            os.makedirs(self.work, exist_ok=True)
            return self.work

    def _sync(self, repo: str) -> None:
        mirror = self._mirrors.get(repo)
        if mirror is None:
            token = self.token_for(repo) if self.token_for is not None else None
            main = self.base_branch_for(repo) if self.base_branch_for is not None else None
            mirror = Mirror(os.path.join(self.root, "mirrors", repo),
                            f"{self.server_url}/{repo}.git", main=main, token=token)
            self._mirrors[repo] = mirror
        mirror.ensure()
        mirror.fetch()
        sha = mirror.main_sha()
        path = os.path.join(self.work, repo)
        if not os.path.isdir(os.path.join(path, ".git")) and not os.path.isfile(os.path.join(path, ".git")):
            shutil.rmtree(path, ignore_errors=True)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mirror.run("worktree", "prune", check=False)
            mirror.run("worktree", "add", "-q", "--force", "--detach", path, sha)
        elif self._shas.get(repo) != sha:
            mirror.run("checkout", "-q", "--force", "--detach", sha, cwd=path)
        self._shas[repo] = sha

    def _drop(self, repo: str) -> None:
        self._shas.pop(repo, None)
        path = os.path.join(self.work, repo)
        mirror = self._mirrors.get(repo)
        if mirror is not None:
            mirror.run("worktree", "remove", "--force", path, check=False)
        shutil.rmtree(path, ignore_errors=True)


class OmpAgent(_Run):
    """One chat thread's omp process. `ask` runs one turn; `close` ends the session."""

    def __init__(self, runner: AssistRunner, spec: AgentSpec,
                 on_intent: Callable[[str, str], None], token: str | None, session_dir: str) -> None:
        super().__init__(runner, spec)
        self.on_intent = on_intent
        self.token = token
        self.session_dir = session_dir
        self.on_text: Callable[[str], None] | None = None
        self.busy = False
        self._blocks: dict[int, str] = {}
        self._message: str | None = None
        self._last_text = ""
        self._closed = False

    # -- frames -----------------------------------------------------------------------
    def handle(self, frame: dict):
        kind = frame.get("type")
        if kind == "message_update" and (frame.get("message") or {}).get("role", "assistant") == "assistant":
            event = frame.get("assistantMessageEvent") or {}
            if frame.get("messageId") != self._message:
                self._message, self._blocks = frame.get("messageId"), {}
            index = int(event.get("contentIndex") or 0)
            if event.get("type") == "text_start":
                self._blocks[index] = ""
            elif event.get("type") == "text_delta":
                self._blocks[index] = self._blocks.get(index, "") + str(event.get("delta") or "")
                text = "".join(self._blocks[i] for i in sorted(self._blocks))
                if text.strip() and self.on_text is not None:
                    self.on_text(text)
        elif kind == "message_end":
            message = frame.get("message") or {}
            if message.get("role") == "assistant":
                parts = message.get("content") or []
                text = "".join(p.get("text", "") for p in parts
                               if isinstance(p, dict) and p.get("type") == "text").strip()
                if text:
                    self._last_text = text
        return super().handle(frame)

    def intents(self) -> None:
        """Tern's intents: a steer reaches a running turn; a follow-up (or a steer while idle)
        becomes the thread's next question; an interrupt aborts the turn."""
        after = self.r.sessions.last_intent_id(self.sid)
        while not self.stop.is_set():
            try:
                rows = self.r.sessions.read_intents(self.sid, after, block_ms=1000)
            except Exception:  # noqa: BLE001 — Redis blip: try again
                self.stop.wait(1)
                continue
            for iid, kind, actor, payload in rows:
                after = iid
                text = str(payload.get("text") or "")
                try:
                    if kind == "interrupt" and self.busy:
                        self.send("abort")
                    elif kind == "steer" and self.busy and text.strip():
                        self.send("steer", {"message": text})
                    elif kind in ("steer", "follow_up") and text.strip():
                        self.on_intent(actor, text)
                    else:
                        self.r.sessions.intent_applied(self.sid, iid, kind, ignored=True)
                        continue
                except OSError:  # omp exited: the session is over
                    return
                self.r.sessions.intent_applied(self.sid, iid, kind)

    # -- turns ------------------------------------------------------------------------
    def ask(self, prompt: str, on_text: Callable[[str], None], timeout_s: float,
            images: tuple[dict, ...] = ()) -> TurnResult:
        self.calls = {t.name: [] for t in self.spec.host_tools}
        self.finished = False
        self._last_text, self._blocks, self._message = "", {}, None
        self.on_text = on_text
        self.busy = True
        self.r.sessions.update(self.sid, state="running")
        try:
            self.prompt(prompt, images=images)
            outcome, timed_out = self._turn(timeout_s)
        except (OmpError, AgentFailed, OSError) as exc:
            return TurnResult("failed", self._last_text, None, str(exc)[:300])
        finally:
            self.busy = False
            self.on_text = None
            self.r.sessions.update(self.sid, state="idle")
        self.sync_transcript()
        self.sync_usage_limit()
        proposals = self.calls.get("propose_tickets") or []
        proposal = proposals[-1] if proposals else None
        if timed_out:
            return TurnResult("timeout", self._last_text, proposal, "the turn took too long")
        if outcome.status == "aborted":
            status = "completed" if self.finished else "aborted"
            return TurnResult(status, self._last_text, proposal, None)
        if outcome.status != "completed":
            return TurnResult("failed", self._last_text, proposal, outcome.error_message)
        return TurnResult("completed", self._last_text, proposal, None)

    def _turn(self, timeout_s: float):
        start = last_seen = time.monotonic()
        aborted_at: float | None = None
        while True:
            saw, outcome = self.pump_one(1.0)
            if outcome is not None:
                self.watcher = None
                return outcome, aborted_at is not None
            now = time.monotonic()
            if saw:
                last_seen = now
            if aborted_at is None and now - start >= timeout_s:
                self.send("abort")
                aborted_at = now
            elif aborted_at is not None and now - aborted_at > ABORT_GRACE_S:
                raise AgentFailed("the turn did not stop after abort")
            elif now - last_seen > self.r.silence_s:
                raise AgentFailed(f"omp silent for {self.r.silence_s:.0f}s")

    def close(self, outcome: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop.set()
        try:
            if self.proc is not None:
                self.proc.close()
        finally:
            self.r.sessions.close(self.sid, outcome)
            self.r.revoke(self.token)
            # Nothing reads the session dir after the process: the transcript is in the store.
            shutil.rmtree(self.session_dir, ignore_errors=True)

    @property
    def alive(self) -> bool:
        proc = getattr(self.proc, "proc", None)
        return not self._closed and (proc is None or proc.poll() is None)


class AssistRunner(AgentRunner):
    """The engine's `AgentFactory` on omp."""

    def __init__(
        self,
        sessions,
        *,
        session_root: str,
        config: Callable[[], object],
        team,
        tools_for: Callable[[str], list[HostTool]],
        repos: ReadOnlyRepos | None = None,
        providers: Callable[[str, str], object | None] | None = None,
        mcp=None,  # mcp_servers.McpLaunch
        omp_bin: str = "omp",
        spawn=None,
        environ: dict | None = None,
        silence_s: float = 1200.0,
        command_timeout_s: float = 30.0,
        skill_stager: Callable[[str, str, dict], None] | None = None,
    ) -> None:
        super().__init__(sessions, session_root=session_root, omp_bin=omp_bin, spawn=spawn,
                         silence_s=silence_s, command_timeout_s=command_timeout_s,
                         providers=providers)
        self.config = config
        self.team = team
        self.tools_for = tools_for
        self.repos = repos
        self.mcp = mcp
        self.environ = environ if environ is not None else os.environ
        self.skill_stager = skill_stager

    def commits(self) -> dict[str, str]:
        return self.repos.commits() if self.repos is not None else {}

    def revoke(self, token: str | None) -> None:
        if token and self.mcp is not None:
            try:
                self.mcp.end(token)
            except Exception:  # noqa: BLE001 — it expires on its own
                pass

    def stage(self, role: str, model: dict, sid: str, session_dir: str) -> tuple[dict, str | None]:
        """The child's env and its MCP token: a private agent dir with the role's
        `models.yml` and MCP config. Keys travel only in the env."""
        env = agent_environment(dict(self.environ))
        agent_dir = os.path.join(session_dir, "agent")
        os.makedirs(agent_dir, mode=0o700, exist_ok=True)
        runtime = self.providers(model["provider"], model["model"]) if self.providers else None
        if runtime is not None:
            fd = os.open(os.path.join(agent_dir, "models.yml"),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(runtime.models_config, fh)  # JSON is YAML
            env.update(runtime.environment)

        token = None
        if self.mcp is not None:
            token = self.mcp.stage(mcp_servers.write_private_file, agent_dir, role, sid)
        else:
            # No MCP servers, but still no project or user omp config.
            mcp_servers.write_private_file(os.path.join(agent_dir, "config.yml"),
                                           mcp_servers.AGENT_CONFIG)
        # Stage central skills into the agent dir when configured.
        if self.skill_stager is not None:
            try:
                self.skill_stager(agent_dir, role, {})
            except Exception:  # noqa: BLE001 — skill staging never fails a session
                pass
        return {**env, "PI_CODING_AGENT_DIR": agent_dir}, token

    def open(self, sid: str, role: str, meta: dict, on_intent: Callable[[str, str], None],
             tools: Iterable[HostTool] = ()) -> OmpAgent:
        """`tools` are the engine's own host tools for this session (memory, team facts)."""
        cfg = self.config()
        model = role_model(cfg, self.team.get(), role)
        cwd = self.repos.refresh() if self.repos is not None else self.session_root
        os.makedirs(cwd, exist_ok=True)
        spec = AgentSpec(sid=sid, kind="assist", role=role, model=model, prompt="", cwd=cwd,
                         tools=TOOLS, host_tools=[*self.tools_for(role), *tools], meta=meta)
        session_dir = os.path.join(self.session_root, sid.replace(":", "_"))
        self.sessions.open(sid, kind="assist", model=model_arg(model), pod=self.pod, role=role, **meta)
        token = None
        try:
            env, token = self.stage(role, model, sid, session_dir)
            proc = self._spawn(self.argv(spec, session_dir), cwd, env,
                               os.path.join(session_dir, "omp.stderr"))
        except Exception as exc:
            self.sessions.close(sid, "failed")
            self.revoke(token)
            shutil.rmtree(session_dir, ignore_errors=True)
            raise AgentFailed(f"{sid}: the agent did not start: "
                              f"{getattr(exc, 'code', None) or type(exc).__name__}") from None
        agent = OmpAgent(self, spec, on_intent, token, session_dir)
        agent.proc = proc
        try:
            agent.command("set_event_filter", {"events": None, "messageUpdates": "delta"})
            agent.command("set_host_tools", {"tools": [t.definition() for t in spec.host_tools]})
        except (AgentFailed, OmpError, OSError):
            agent.close("failed")
            raise
        threading.Thread(target=agent.intents, name=f"intents-{sid}", daemon=True).start()
        return agent

