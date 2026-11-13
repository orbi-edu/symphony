"""Runner: one execution per issue-attempt. Pure over the injected interfaces.

Discipline (root SPEC.md §3.7.2, constitution IV): fencing FIRST and re-validated on
every turn; the runner writes `runner_call_id`/`sandbox_id` itself; on finish it updates
its own claim (success/continuation/failure/blocked) — crash recovery belongs to the
daemon. The agent process is ALWAYS stopped before releasing the sandbox, and stopped
with SIGTERM (never by closing stdin alone) so its last telemetry batch is exported.

The attempt is also a live session (internals §2): every RPC event frame is published to
the session stream, the transcript is synced at each turn end, and human intents (steer,
follow-up, interrupt, answer) are applied the moment they arrive by a dedicated thread.
Tracker access and human questions are host tools served here (internals §3), so the
tracker credential never enters the workspace.
"""

from __future__ import annotations

import random
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from . import endpoints, metrics, prwatch, state
from .config import Config, bind_role_model
from .hosttools import HostToolBroker, tool_definitions
from .omp_rpc import CommandFailed, OmpClient, OmpError, ReadTimeout, RunWatcher, SilenceTimeout
from .orchestrator import PARKED, mark_clean_continuation, mark_retry_after_failure
from .prompt import PromptError, render_prompt
from .sessions import SessionStore, attempt_session_id
from .teamconfig import TeamConfig, model_arg
from .trackers.base import Issue, TrackerAdapter, TrackerUnavailable
from .usage import UsageLedger, limit_from_retry, limit_from_usage_state, record_rate_limit
from .workspace import HookFatal, SandboxLike, run_hook

HEARTBEAT_THROTTLE_MS = 10_000
CONTINUATION_GUIDANCE = (
    "Continue working on the issue from where you left off. Re-read the Workpad on the "
    "ticket, check the state of the branch and the pull request, and keep going until the "
    "completion bar is satisfied."
)
HUMAN_MESSAGE_HEADER = "Message from a human supervising this ticket:"
# SIGTERM → exit budget. OMP flushes its OTLP batch on SIGTERM; a few seconds suffice.
ENGINE_STOP_GRACE_S = 20.0
# Upper bound on one XREAD BLOCK: only the intent pump's shutdown latency depends on it,
# never the delivery of an intent (XREAD returns the moment one arrives).
INTENT_BLOCK_MS = 1_000
LAST_MESSAGE_CHARS = 280
# Frames that are protocol plumbing rather than session events.
_PROTOCOL_FRAMES = frozenset({"response", "ready", "host_tool_call", "host_tool_cancel"})
# `<GITHUB_SERVER_URL>/<owner>/<repo>/pull/<n>`; the host follows the configured server.
_PR_PATH = r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)"


@dataclass
class RunnerEnv:
    kv: state.KV
    tracker: TrackerAdapter
    cfg: Config
    now_ms: Callable[[], int]
    call_id: str
    # (issue, cfg bound to this attempt's model) -> sandbox
    ensure_sandbox: Callable[[Issue, Config], SandboxLike]
    # (sandbox, attempt token, issue, cfg bound to this attempt's model) -> client
    open_client: Callable[[SandboxLike, str, Issue, Config], OmpClient]
    kill_agent: Callable[[SandboxLike], None]
    release_sandbox: Callable[[SandboxLike], None]
    wall_deadline_ms: int
    sessions: SessionStore
    team: TeamConfig
    sleep_s: Callable[[float], None] = field(default=lambda s: None)
    # US2: rewrites the sandbox credential file when the installation token nears expiry.
    # Returns False when the credential could not be renewed, which fails the attempt
    # with a distinct reason rather than an opaque push failure hours later (FR-018).
    refresh_credentials: Callable[[SandboxLike], bool] = field(default=lambda sb: True)
    log: Callable[..., None] = field(default=lambda event, **kw: None)
    intent_block_ms: int = INTENT_BLOCK_MS
    # The attempt prompt's extra paragraph for the role's MCP servers (`McpLaunch.prompt_guidance`).
    mcp_guidance: Callable[[], str] = field(default=lambda: "")


class RunnerFenced(Exception):
    """The generation lost write authority; it must not touch a successor."""


class _RunnerKV:
    def __init__(self, kv, issue_id, attempt, seq, token):
        self.base = kv
        self.key = state.issue_key(issue_id)
        self.token_key = state.dispatch_key(issue_id, seq)
        self.expected = {
            "attempt": attempt,
            "dispatch_seq": seq,
            "bridge_token": token,
            "status": "running",
        }

    def get(self, key, default=None):
        value = self.base.get(key, default)
        return dict(value) if isinstance(value, dict) else value

    def put(self, key, value, *, skip_if_exists=False):
        if key == self.key:
            if not state.fenced_put(self.base, key, value, self.expected, self.token_key):
                raise RunnerFenced()
            return True
        return self.base.put(key, value, skip_if_exists=skip_if_exists)

    def delete(self, key):
        if key == self.key:
            raise RunnerFenced()
        return self.base.delete(key)

    def keys(self):
        return self.base.keys()

    def owns(self):
        entry = self.base.get(self.key)
        return bool(
            entry
            and all(entry.get(k) == v for k, v in self.expected.items())
            and self.base.get(self.token_key)
        )


def fencing_ok(kv: state.KV, issue_id: str, attempt: int, seq: int) -> bool:
    """Dispatch ownership: the entry points to THIS attempt AND this dispatch seq, and
    the seq's token exists. Platform re-executions (stale attempt OR stale seq) and
    zombies fail here and exit with no effects."""
    entry = kv.get(state.issue_key(issue_id))
    token = kv.get(state.dispatch_key(issue_id, seq))
    return (
        entry is not None
        and entry.get("attempt") == attempt
        and entry.get("dispatch_seq", attempt) == seq
        and entry.get("status") in {"claimed", "running"}
        and token is not None
    )


def new_bridge_token() -> str:
    """Authorizes ONE attempt against the sandbox gateway (telemetry).
    Rotated per dispatch, so a leaked value dies with the attempt and cannot be replayed
    against a later one."""
    return secrets.token_urlsafe(32)


def workspace_root(sandbox: SandboxLike, cfg: Config) -> str:
    """Where the agent works: a host sandbox (the local mode) in its own directory, a pod in
    `workspace.root`. Host tools that read workspace files resolve paths against it."""
    return getattr(sandbox, "root", None) or cfg.workspace_root


def target_repos(issue: Issue) -> tuple[str, ...]:
    """Every repository the attempt works in, primary first."""
    if issue.target_repos:
        return tuple(issue.target_repos)
    return (issue.target_repo,) if issue.target_repo else ()


def _fetch_issue(env: RunnerEnv, issue_id: str) -> Issue | None:
    """Re-fetch with waiting under unavailability (never fail the attempt over an outage)."""
    delay = 1.0
    while True:
        try:
            found = env.tracker.fetch_issues_by_ids([issue_id])
            return found[0] if found else None
        except TrackerUnavailable:
            if env.now_ms() >= env.wall_deadline_ms:
                return None
            env.sleep_s(delay * random.uniform(0.5, 1.5))  # jitter: N runners must not sync up
            delay = min(delay * 2, 30.0)


def run_attempt(env: RunnerEnv, issue_id: str, attempt: int, seq: int | None = None) -> str:
    """Claim a runner generation atomically, then fence every lifecycle write."""
    seq = seq if seq is not None else attempt
    if not fencing_ok(env.kv, issue_id, attempt, seq):
        return "fenced"
    entry = dict(env.kv.get(state.issue_key(issue_id)) or {})
    token = new_bridge_token()
    entry.update(
        status="running", runner_call_id=env.call_id, bridge_token=token, updated_at=env.now_ms()
    )
    if not state.fenced_put(
        env.kv,
        state.issue_key(issue_id),
        entry,
        {"attempt": attempt, "dispatch_seq": seq, "status": "claimed"},
        state.dispatch_key(issue_id, seq),
    ):
        return "fenced"
    scoped = _RunnerKV(env.kv, issue_id, attempt, seq, token)
    try:
        return _run_owned_attempt(replace(env, kv=scoped), issue_id, attempt, seq, token)
    except RunnerFenced:
        return "fenced"


# --------------------------------------------------------------------------
# live session: event publishing, transcript sync, human intents
# --------------------------------------------------------------------------
def _forwardable(frame: dict) -> bool:
    kind = frame.get("type")
    if kind in _PROTOCOL_FRAMES:
        return False
    # A built-in extension repaints a widget continuously; it is UI chrome, not a session
    # event, and would drown the stream (an early spike finding).
    return not (kind == "extension_ui_request" and frame.get("method") == "setWidget")


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def _with_human_messages(base: str, messages: list[str]) -> str:
    if not messages:
        return base
    parts = [base.rstrip()] if base.strip() else []
    for text in messages:
        parts.append(f"{HUMAN_MESSAGE_HEADER}\n{text}" if parts else text)
    return "\n\n".join(parts)


class LiveSession:
    """The attempt's live session: what people following it see, and how they steer it.

    The turn loop (the only reader of the engine stream) feeds `observe()` and drives
    prompts through `send_prompt()`. A dedicated pump thread blocks on the intent stream
    and applies each intent at once: while a prompt runs, steer/follow-up/interrupt go
    straight to omp; between prompts, messages are held and delivered with the next one,
    so a message is never dropped and never collides with a runner prompt.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        sid: str,
        seq: int,
        client: OmpClient,
        owns: Callable[[], bool],
        repos: tuple[str, ...],
        log: Callable[..., None],
        block_ms: int = INTENT_BLOCK_MS,
    ) -> None:
        self.store = store
        self.sid = sid
        self.seq = seq
        self.client = client
        self._owns = owns
        self._repos = {repo.casefold() for repo in repos}
        self._log = log
        self._block_ms = block_ms
        self.broker: HostToolBroker | None = None
        self._cond = threading.Condition()
        self._phase = "starting"  # starting | running | stopping | between | idle
        self._held: list[tuple[str, str, str]] = []  # (intent id, kind, text)
        self._interrupted = False
        self._stop = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self.cursor = "0-0"  # every intent addressed to this attempt's session counts
        self._last_entry: str | None = None
        self._seen_entries: set[str] = set()
        self._pr_url: str | None = None

    # -- intents ---------------------------------------------------------------
    def start(self, broker: HostToolBroker) -> None:
        self.broker = broker
        # Intents written while the workspace was starting are applied now, before the
        # first prompt exists, so they deterministically ride with it.
        self._apply_rows(self.store.read_intents(self.sid, after=self.cursor))
        self._pump_thread = threading.Thread(
            target=self._pump, name=f"intents-{self.sid}", daemon=True
        )
        self._pump_thread.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self.store.read_intents(self.sid, after=self.cursor, block_ms=self._block_ms)
            except Exception as exc:  # noqa: BLE001 — a Redis blip must not end the pump
                self._log("intent_read_failed", sid=self.sid, error=str(exc))
                self._stop.wait(1.0)
                continue
            self._apply_rows(rows)

    def _apply_rows(self, rows: list[tuple[str, str, str, dict]]) -> None:
        for intent_id, kind, actor, payload in rows:
            self.cursor = intent_id
            if self._stop.is_set():
                return
            try:
                self._apply(intent_id, kind, actor, payload)
            except Exception as exc:  # noqa: BLE001 — one bad intent never stops the rest
                self._log("intent_apply_failed", sid=self.sid, intent=intent_id, error=str(exc))

    def _apply(self, intent_id: str, kind: str, actor: str, payload: dict) -> None:
        owner_seq = payload.get("dispatch_seq")
        if owner_seq is not None and owner_seq != self.seq:
            self.store.intent_applied(
                self.sid, intent_id, kind, ignored=True, reason="intent for another attempt"
            )
            return
        if not self._owns():
            self.store.intent_applied(
                self.sid, intent_id, kind, ignored=True, reason="attempt no longer owns the issue"
            )
            return
        text = str(payload.get("text") or "").strip()
        if kind == "answer":
            if self.broker is not None and self.broker.answer(payload.get("request_id"), text, actor):
                self.store.intent_applied(self.sid, intent_id, kind)
                return
            # The question timed out or was cancelled: the answer still reaches the agent,
            # as a message, rather than vanishing.
            kind_applied = "follow_up"
        else:
            kind_applied = kind
        with self._cond:
            if kind_applied == "interrupt":
                self._interrupted = True
                if self._phase == "running":
                    self.client.send("abort")
                    # The run is winding down: messages written from now on wait for the
                    # next prompt instead of racing the abort into omp's queues.
                    self._phase = "stopping"
                self.store.intent_applied(self.sid, intent_id, kind, reason=payload.get("reason"))
                self._cond.notify_all()
                return
            if not text:
                self.store.intent_applied(self.sid, intent_id, kind, ignored=True, reason="empty text")
                return
            if self._phase == "running":
                self.client.send(kind_applied, {"message": text})
                self.store.intent_applied(self.sid, intent_id, kind, delivered=kind_applied)
                return
            self._held.append((intent_id, kind, text))
            self._cond.notify_all()

    # -- prompts ---------------------------------------------------------------
    def send_prompt(self, text: str) -> str:
        """Send a runner prompt, carrying every message held since the last one. The
        followUp behavior absorbs the race where omp already started a run of its own."""
        with self._cond:
            held, self._held = self._held, []
            message = _with_human_messages(text, [t for _, _, t in held])
            prompt_id = self.client.send(
                "prompt", {"message": message, "streamingBehavior": "followUp"}
            )
            self._phase = "running"
            if self._interrupted:
                # Interrupted after the loop's last check: stop the run we just started.
                self.client.send("abort")
        for intent_id, kind, _ in held:
            self.store.intent_applied(self.sid, intent_id, kind, delivered="prompt")
        return prompt_id

    def run_finished(self) -> bool:
        """The prompt's run ended. True when a human interrupt ended it."""
        with self._cond:
            self._phase = "between"
            interrupted, self._interrupted = self._interrupted, False
        return interrupted

    def take_interrupt(self) -> bool:
        with self._cond:
            interrupted, self._interrupted = self._interrupted, False
        return interrupted

    def wait_for_human(self, timeout_s: float) -> bool:
        """Park after a human interrupt until someone writes (True) or the timeout."""
        with self._cond:
            self._phase = "idle"
            written = self._cond.wait_for(
                lambda: bool(self._held) or self._stop.is_set(), max(0.0, timeout_s)
            ) and bool(self._held)
            # Nothing runs while parked, so an interrupt here has nothing left to stop.
            self._interrupted = False
            return written

    # -- events --------------------------------------------------------------
    def observe(self, frame: dict, ledger_totals: dict | None) -> None:
        if self.broker is not None and frame.get("type") in ("host_tool_call", "host_tool_cancel"):
            self.broker.handle(frame)
        if not _forwardable(frame):
            return
        kind = str(frame.get("type") or "unknown")
        self.store.append_event(self.sid, kind, frame)
        if kind != "message_end":
            return
        message = frame.get("message") or {}
        fields: dict = {}
        if ledger_totals is not None:
            fields["tokens"] = {
                "input": ledger_totals.get("input", 0),
                "output": ledger_totals.get("output", 0),
                "total": ledger_totals.get("total", 0),
                "cost": ledger_totals.get("cost_estimate", 0.0),
            }
        text = _message_text(message)
        if message.get("role") == "assistant" and text.strip():
            fields["last_message"] = text.strip()[:LAST_MESSAGE_CHARS]
        for match in re.finditer(re.escape(endpoints.github_server_url()) + _PR_PATH, text):
            if not self._repos or match.group(1).casefold() in self._repos:
                if match.group(0) != self._pr_url:
                    self._pr_url = match.group(0)
                    fields["pr_url"] = self._pr_url
        if fields:
            self.store.update(self.sid, **fields)

    def sync_transcript(self, timeout_s: float) -> int:
        """Append the session entries omp recorded since the last sync."""
        try:
            try:
                data = self.client.command(
                    "get_entries",
                    {"since": self._last_entry} if self._last_entry else None,
                    timeout_s,
                )
            except CommandFailed:
                # Cursor unknown (the session was replaced): resync and dedupe by id.
                data = self.client.command("get_entries", None, timeout_s)
        except (ReadTimeout, CommandFailed) as exc:
            self._log("transcript_sync_failed", sid=self.sid, error=str(exc))
            return 0
        entries = [e for e in data.get("entries") or [] if isinstance(e, dict)]
        fresh = [e for e in entries if e.get("id") not in self._seen_entries]
        if entries and entries[-1].get("id"):
            self._last_entry = entries[-1]["id"]
        self._seen_entries.update(e["id"] for e in fresh if e.get("id"))
        return self.store.append_transcript(self.sid, fresh)

    # -- shutdown ----------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._pump_thread is not None:
            self._pump_thread.join(self._block_ms / 1000 + 5)
        if self.broker is not None:
            self.broker.close()
        with self._cond:
            held, self._held = self._held, []
        for intent_id, kind, _ in held:
            self.store.intent_applied(
                self.sid, intent_id, kind, ignored=True, reason="attempt ended"
            )


# --------------------------------------------------------------------------
# the attempt
# --------------------------------------------------------------------------
def _run_owned_attempt(env, issue_id, attempt, seq, bridge_token):
    kv = env.kv

    entry = kv.get(state.issue_key(issue_id))

    answering = entry.get("mode") == prwatch.ANSWER_MODE
    issue = _fetch_issue(env, issue_id)
    # An answer run exists precisely because the ticket is NOT in an active state — it is
    # parked under a reviewer, and it stays there. Applying the active-state check to it
    # would make it exit before saying anything (feature 005, FR-008).
    if issue is None or (not answering and issue.state_type != "active"):
        return _finish(env, issue_id, "done", issue)

    # Team configuration is read once per attempt (internals §4): a publication reaches
    # the next attempt without restarts and never changes a model mid-attempt.
    team_doc = env.team.get()
    cfg = bind_role_model(env.cfg, team_doc, "implementer")
    env = replace(env, cfg=cfg)
    env.team.observe(f"runner:{issue.identifier}", int(team_doc.get("generation") or 0))
    repos = target_repos(issue)

    # The repository is fixed for the attempt's duration and persisted with the run: the
    # retrospective addresses it after the workspace is gone and the ticket may have
    # changed. A designation edited mid-attempt takes effect on the next dispatch —
    # switching now would orphan a clone, a branch and possibly a pull request.
    entry = kv.get(state.issue_key(issue_id)) or entry
    entry.update(
        target_repo=issue.target_repo,
        target_repos=list(repos),
        title=issue.title,
        runtime=cfg.agent_runtime,
        provider=cfg.engine_provider,
        model=cfg.engine_model,
        thinking=cfg.engine_thinking,
        tracker_state=issue.state,
        tracker_state_type=issue.state_type,
        last_issue_updated_at=issue.updated_at,
    )
    kv.put(state.issue_key(issue_id), entry)

    sid = attempt_session_id(issue_id, seq)
    env.sessions.open(
        sid,
        kind="answer" if answering else "implement",
        identifier=issue.identifier,
        title=issue.title,
        issue_id=issue_id,
        seq=seq,
        repo=issue.target_repo,
        branch=issue.branch_name,
        url=issue.url,
        model=model_arg({"provider": cfg.engine_provider, "model": cfg.engine_model}),
        pod=None,
    )
    outcome = "error"
    try:
        outcome = _run_session(env, issue_id, attempt, seq, bridge_token, issue, sid, repos)
        return outcome
    except RunnerFenced:
        outcome = "fenced"
        raise
    finally:
        try:
            env.sessions.close(sid, outcome)
        except Exception as exc:  # noqa: BLE001 — the session must never mask the outcome
            env.log("session_close_failed", sid=sid, error=str(exc))


def _run_session(env, issue_id, attempt, seq, bridge_token, issue, sid, repos):
    kv, cfg = env.kv, env.cfg
    entry = kv.get(state.issue_key(issue_id))
    sandbox = None
    agent_started = False
    client: OmpClient | None = None
    live: LiveSession | None = None
    outcome = "failed"
    failure_summary: str | None = None
    try:
        sandbox = env.ensure_sandbox(issue, cfg)
        entry = kv.get(state.issue_key(issue_id)) or entry
        entry.update(sandbox_name=sandbox.name, sandbox_id=getattr(sandbox, "id", None))
        kv.put(state.issue_key(issue_id), entry)
        env.sessions.update(sid, pod=sandbox.name)

        agent_started = True  # opening the client may launch before raising
        client = env.open_client(sandbox, bridge_token, issue, cfg)

        # A command round-trip, not the session file's presence, proves readiness.
        # Native startup includes provider/credential discovery before RPC is ready, so
        # startup commands get a grace period; warm RPC keeps the configured timeout.
        read_timeout_s = cfg.read_timeout_ms / 1000
        startup_timeout_s = max(30.0, read_timeout_s)
        client.negotiate(startup_timeout_s)
        session_path = entry.get("session_path")
        if session_path:
            try:
                switched = client.command(
                    "switch_session", {"sessionPath": session_path}, startup_timeout_s
                )
                if switched.get("cancelled"):
                    raise OmpError("session switch cancelled")
            except (CommandFailed, ReadTimeout):
                # stale session (files lost or rotated): fall back to a fresh session IN
                # THE SAME attempt — never burn the attempt on this
                session_path = None
                entry = kv.get(state.issue_key(issue_id)) or entry
                entry.update(session_path=None)
                kv.put(state.issue_key(issue_id), entry)
                fresh = client.command("new_session", None, startup_timeout_s)
                if fresh.get("cancelled"):
                    raise OmpError("new session cancelled") from None
        client.command(
            "set_event_filter", {"events": None, "messageUpdates": "delta"}, startup_timeout_s
        )
        client.command("set_host_tools", {"tools": tool_definitions(env.tracker)}, startup_timeout_s)
        # Human questions go through symphony_ask (Redis, Tern, Slack), never omp's dialog.
        client.command("set_ask_dialog", {"enabled": False}, startup_timeout_s)
        # WORKFLOW `engine.auto_retry`: omp retries transient provider errors inside the turn.
        client.command("set_auto_retry", {"enabled": cfg.auto_retry}, startup_timeout_s)

        st = client.command("get_state", None, startup_timeout_s)
        entry = kv.get(state.issue_key(issue_id)) or entry
        entry.update(
            session_path=st.get("sessionFile") or session_path, session_id=st.get("sessionId")
        )
        kv.put(state.issue_key(issue_id), entry)

        live = LiveSession(
            store=env.sessions,
            sid=sid,
            seq=seq,
            client=client,
            owns=kv.owns,
            repos=repos,
            log=env.log,
            block_ms=env.intent_block_ms,
        )
        live.start(
            HostToolBroker(
                store=env.sessions,
                sid=sid,
                kv=kv,
                tracker=env.tracker,
                issue_id=issue_id,
                seq=seq,
                identifier=issue.identifier,
                respond=client.host_tool_result,
                owns=kv.owns,
                input_timeout_s=cfg.input_timeout_ms / 1000,
                notify_link=issue.url,
                log=env.log,
                sandbox=sandbox,
                workspace_root=workspace_root(sandbox, cfg),
                required_labels=tuple(cfg.required_labels),
            )
        )
        outcome, failure_summary = _turn_loop(
            env, client, live, issue_id, attempt, seq, sid, sandbox
        )
    except HookFatal as exc:
        outcome, failure_summary = "failed", str(exc)
    except PromptError as exc:
        outcome, failure_summary = "failed", f"prompt render failed: {exc}"
    except TrackerUnavailable as exc:
        outcome, failure_summary = "failed", f"tracker unavailable during attempt: {exc}"
    except (ReadTimeout, OmpError) as exc:
        outcome, failure_summary = "failed", str(exc)
    finally:
        if live is not None:
            try:
                live.stop()
            except Exception as exc:  # noqa: BLE001 — never skip the engine stop
                env.log("live_session_stop_failed", sid=sid, error=str(exc))
        if sandbox is not None:
            try:
                if kv.owns():
                    # Shared hooks require ownership; exact-generation stopping does not.
                    run_hook(sandbox, "after_run", cfg)
            except Exception:  # noqa: BLE001 — Redis/hook failure must not skip the kill
                pass
            if agent_started:
                if client is not None:
                    try:
                        if client.terminate(ENGINE_STOP_GRACE_S) is False:
                            env.log("engine_sigterm_grace_exceeded", sid=sid)
                    except Exception as exc:  # noqa: BLE001 — the generation kill follows
                        env.log("engine_sigterm_failed", sid=sid, error=str(exc))
                env.kill_agent(sandbox)
            if kv.owns():
                env.release_sandbox(sandbox)

    return _finish(env, issue_id, outcome, issue, failure_summary)


def _turn_loop(
    env: RunnerEnv,
    client: OmpClient,
    live: LiveSession,
    issue_id: str,
    attempt: int,
    seq: int,
    sid: str,
    sandbox: SandboxLike,
) -> tuple[str, str | None]:
    kv, cfg = env.kv, env.cfg
    entry = kv.get(state.issue_key(issue_id))
    answering = entry.get("mode") == prwatch.ANSWER_MODE
    ledger = UsageLedger()
    last_flush = env.now_ms()
    read_timeout_s = cfg.read_timeout_ms / 1000
    turn = 0
    # Answering is a short errand, not a work session: a handful of turns to read the
    # question, check what it refers to, and reply.
    turn_cap = cfg.answer_max_turns if answering else cfg.max_turns
    # Set after a human interrupt: the next prompt is what the human wrote, not ours.
    human_turn = False

    while True:
        if not fencing_ok(kv, issue_id, attempt, seq):
            return "fenced", None
        if env.now_ms() >= env.wall_deadline_ms:
            return "continuation", None  # wall-clock guard: exit cleanly, never killed at the wall
        current = _fetch_issue(env, issue_id)
        if current is None:
            return "done", None
        if answering:
            # The reviewer moving the ticket outranks the question: rework and terminal both
            # supersede an answer run, and the full path takes over from here.
            if current.state_type != "unknown" or current.state in cfg.active_states:
                return "done", None
        elif current.state_type != "active" or current.state not in cfg.active_states:
            return "done", None
        if turn >= turn_cap:
            return "continuation", None  # FR-013: turn cap ends cleanly
        if not human_turn and live.take_interrupt():
            # Interrupted between prompts: hold the next one until a human writes.
            if not _park_for_human(env, live, sid):
                return "continuation", None
            human_turn = True
        turn += 1

        if human_turn:
            prompt_text = ""
        elif answering:
            prompt_text = render_prompt(
                cfg.answer_prompt,
                current,
                entry.get("runs", attempt),
                "",
                investigation_label=cfg.investigation_label,
            )
        elif turn == 1:
            guidance = []
            if entry.get("failure_summary"):
                guidance.append(
                    f"Failure summary from the previous attempt (learn from it): {entry['failure_summary']}"
                )
            mcp_guidance = env.mcp_guidance()
            if mcp_guidance:
                guidance.append(mcp_guidance)
            # upstream §12.3: the template `attempt` counts RUNS (null on the very first
            # run), not just failures — continuations advance it too
            prompt_text = render_prompt(
                cfg.prompt_body,
                current,
                entry.get("runs", attempt),
                "\n\n".join(guidance),
                investigation_label=cfg.investigation_label,
            )
        else:
            prompt_text = CONTINUATION_GUIDANCE
        human_turn = False

        prompt_id = live.send_prompt(prompt_text)
        env.sessions.update(sid, state="running", turn=turn)
        watcher = RunWatcher(prompt_id)

        try:
            for event in client.events(cfg.turn_timeout_ms / 1000):
                now = env.now_ms()
                if not kv.owns():
                    raise RunnerFenced()
                delta = ledger.observe(event)
                live.observe(event, ledger.totals if delta is not None else None)
                throttled = limit_from_retry(event, now)
                if throttled is not None:
                    _record_limit(env, throttled)
                if event.get("type") == "turn_end":
                    live.sync_transcript(read_timeout_s)
                    _observe_usage_limit(env, client, read_timeout_s)

                outcome = watcher.observe(event)
                if outcome is not None:
                    by_human = live.run_finished()
                    live.sync_transcript(read_timeout_s)
                    _flush(env, issue_id, ledger, force=True)
                    if outcome.status in ("completed", "truncated"):
                        break  # next turn, or the loop's own exit conditions decide
                    if outcome.status == "aborted":
                        if by_human and _park_for_human(env, live, sid):
                            human_turn = True
                            break
                        return "continuation", None
                    return "failed", outcome.error_message

                # runner-side heartbeat, throttled to ≤1 write/10s (FR-007)
                if now - last_flush >= HEARTBEAT_THROTTLE_MS:
                    last_flush = _flush(env, issue_id, ledger)
        except SilenceTimeout as exc:
            return "failed", str(exc)

        # Renew the code-hosting credential between turns: installation tokens live an
        # hour while attempts run for up to four (FR-017). Failing here is reported as
        # its own reason rather than surfacing later as an opaque push rejection.
        if not env.refresh_credentials(sandbox):
            return "failed", "could not renew the code-hosting credential"


def _record_limit(env: RunnerEnv, doc: dict) -> None:
    try:
        record_rate_limit(
            env.sessions.client, env.sessions.ns, env.cfg.engine_provider, env.cfg.engine_model, doc
        )
    except Exception as exc:  # noqa: BLE001 — an observation never costs the attempt
        env.log("rate_limit_record_failed", error=str(exc))


def _observe_usage_limit(env: RunnerEnv, client: OmpClient, timeout_s: float) -> None:
    """Contracts §10: the provider account's usage-limit stage, read at each turn end."""
    try:
        st = client.command("get_state", None, timeout_s)
    except (ReadTimeout, CommandFailed) as exc:
        env.log("usage_limit_read_failed", error=str(exc))
        return
    doc = limit_from_usage_state(st.get("usageLimit"), env.now_ms())
    if doc is not None:
        _record_limit(env, doc)


def _park_for_human(env: RunnerEnv, live: LiveSession, sid: str) -> bool:
    """After a human interrupt the agent waits, idle, for the human to write — up to the
    input timeout and never past the wall-clock guard. False: nobody wrote."""
    env.sessions.update(sid, state="idle")
    remaining_s = (env.wall_deadline_ms - env.now_ms()) / 1000
    return live.wait_for_human(min(env.cfg.input_timeout_ms / 1000, remaining_s))


def _flush(env: RunnerEnv, issue_id: str, ledger: UsageLedger, force: bool = False) -> int:
    now = env.now_ms()
    entry = env.kv.get(state.issue_key(issue_id))
    if entry is None:
        return now
    pending = ledger.drain()
    entry["tokens"] = state.add_tokens(entry.get("tokens") or {}, pending)
    entry["last_event_at"] = now
    env.kv.put(state.issue_key(issue_id), entry)
    if any(pending.values()):
        totals = env.kv.get(state.TOTALS_KEY) or {}
        env.kv.put(state.TOTALS_KEY, state.add_tokens(totals, pending))
        model = model_arg({"provider": env.cfg.engine_provider, "model": env.cfg.engine_model})
        metrics.count_tokens(env.sessions.client, env.sessions.ns, model, pending)
    return now


def _finish(
    env: RunnerEnv, issue_id: str, outcome: str, issue: Issue | None, summary: str | None = None
) -> str:
    """The runner updates its OWN claim on finish — without this the first failure would
    wedge the issue forever (minimal claim lifecycle, root SPEC.md §9)."""
    kv, cfg, now = env.kv, env.cfg, env.now_ms()
    entry = kv.get(state.issue_key(issue_id))
    if entry is None or outcome == "fenced":
        return outcome
    updated_at = issue.updated_at if issue is not None else None
    # the attempt token dies with the attempt
    entry["bridge_token"] = None
    if entry.get("mode") == prwatch.ANSWER_MODE:
        # An answer run ends by giving the issue back to the reviewer, whatever its outcome.
        # Routing it through the normal paths would be wrong in both directions: a clean exit
        # would queue it for retry and redispatch it into a state it must not work in, and a
        # failure would burn implementation backoff on a question that went unanswered
        # (FR-010/FR-011).
        entry.update(
            status=PARKED,
            mode=None,
            runner_call_id=None,
            last_event_at=now,
            updated_at=now,
        )
        if outcome == "failed":
            entry["failure_summary"] = summary
        kv.put(state.issue_key(issue_id), entry)
        return outcome
    if outcome == "done":
        entry.update(runner_call_id=None, last_event_at=now, updated_at=now)
        kv.put(state.issue_key(issue_id), entry)
    elif outcome == "continuation":
        mark_clean_continuation(kv, issue_id, entry, now, updated_at)
    elif outcome == "failed":
        mark_retry_after_failure(kv, issue_id, entry, cfg, now, summary or "attempt failed")
    elif outcome == "blocked":
        entry.update(
            status="blocked",
            runner_call_id=None,
            last_issue_updated_at=updated_at,
            failure_summary=summary,
            updated_at=now,
        )
        kv.put(state.issue_key(issue_id), entry)
    return outcome


def compute_env_wall_deadline(start_ms: int, cfg: Config) -> int:
    return start_ms + int(cfg.runner.get("wallclock_guard_s", 12_600)) * 1_000
