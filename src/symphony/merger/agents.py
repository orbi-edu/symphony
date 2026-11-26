"""Merger agents: one `omp --mode rpc` process per agent, typed output through host tools, and
the conversation published to the `SessionStore` so every merger agent is a live session too.

The host registers the role's tools (`submit_finding`, `submit_verdict`, `submit_review`,
`submit_resolution`, `submit_arbitration`), validates every call against its schema and answers
`host_tool_result`; an invalid call is answered with the error, so the agent can correct it. A
role's terminal tool must be called once: a turn that ends without it gets one reminder, then the
run fails. Steer, follow-up and interrupt intents from the API are applied as they arrive.

Each run's directory (`<session_root>/<sid>`: omp's `--session-dir`, its stderr and the private
agent dir with the staged models.yml, MCP config and skills) is removed when the run ends; the
transcript and the events are in the session store by then. `sweep_session_root` removes what a
crashed process left behind.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from .. import metrics
from ..mcp_servers import McpLaunch, write_private_file
from ..omp_rpc import OmpError, RunWatcher
from ..sessions import SessionStore
from ..teamconfig import model_arg

# omp 18.20.2 `--tools` names; an unknown one makes omp exit 2. It has no `ls`, and `find`
# needs fd, which a session may lack: bash covers both for the developer.
READ_TOOLS = ("read", "grep")
EDIT_TOOLS = (*READ_TOOLS, "bash", "edit", "write")
VERDICTS = ("confirmed", "partial", "refuted", "defer")
SEVERITIES = ("blocking", "nit")
# A provider retry caused by throttling (internals §10 `source: retry`).
THROTTLED = re.compile(r"\b429\b|throttl|rate.?limit|too many requests", re.IGNORECASE)

# A leftover run directory older than this, that no run of this process owns, is removed.
SWEEP_AGE_S = 3600
_OWNED: set[str] = set()
_OWNED_LOCK = threading.Lock()


@contextmanager
def owned_run_dir(path: str) -> Iterator[str]:
    """A run's directory, removed when the run ends however it ends. While it runs, the sweep
    leaves it alone."""
    with _OWNED_LOCK:
        _OWNED.add(path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        with _OWNED_LOCK:
            _OWNED.discard(path)


def sweep_session_root(root: str, max_age_s: float = SWEEP_AGE_S,
                       now: Callable[[], float] = time.time) -> list[str]:
    """Removes the `merger_*` run directories under `root` that a crash or an OOM kill left:
    older than `max_age_s` and owned by no run of this process (the merger runs every omp as
    its child, so a run of another process cannot outlive it). Returns the names removed."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    removed = []
    for name in names:
        path = os.path.join(root, name)
        if not name.startswith("merger_"):
            continue
        with _OWNED_LOCK:
            owned = path in _OWNED
        try:
            old = now() - os.stat(path).st_mtime > max_age_s
        except OSError:
            continue
        if owned or not old or not os.path.isdir(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return removed


def record_rate_limit(sessions: SessionStore, model: dict, doc: dict) -> None:
    """internals §10: `sym:<ns>:ratelimits[<provider>/<model>]`, the latest observation, and
    its count (`symphony_rate_limit_events_total`)."""
    sessions.client.hset(f"sym:{sessions.ns}:ratelimits", model_arg(model),
                         json.dumps({**doc, "observed_at": int(time.time() * 1000)}))
    metrics.count_rate_limit(sessions.client, sessions.ns, model.get("provider"), doc.get("stage"))


class AgentFailed(Exception):
    pass


class HostToolError(Exception):
    """A valid call the host could not carry out; the message tells the agent what to do."""


@dataclass
class HostTool:
    name: str
    description: str
    parameters: dict
    terminal: bool = False
    # Extra validation beyond the schema: an error message, or None when the call is fine.
    check: Callable[[dict], str | None] | None = None
    # The text answered for a valid call (given the call's index in its tool's list). Raising
    # HostToolError answers its message as a tool error, and the call is not recorded.
    answer: Callable[[dict, int], str] | None = None

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters, "loadMode": "essential"}


@dataclass
class AgentSpec:
    sid: str
    kind: str
    role: str
    model: dict
    prompt: str
    cwd: str
    tools: tuple[str, ...]
    host_tools: list[HostTool]
    meta: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    sid: str
    status: str
    calls: dict[str, list[dict]]
    error: str | None = None

    def last(self, tool: str) -> dict | None:
        items = self.calls.get(tool) or []
        return items[-1] if items else None


# -- schema validation --------------------------------------------------------------
def validate(schema: dict, value, path: str = "arguments") -> str | None:
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {', '.join(map(str, schema['enum']))}"
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object"
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                return f"{path} has unknown fields: {', '.join(sorted(extra))}"
        for key, sub in props.items():
            if key in value:
                err = validate(sub, value[key], f"{path}.{key}")
                if err:
                    return err
        return None
    if kind == "array":
        if not isinstance(value, list):
            return f"{path} must be an array"
        for i, item in enumerate(value):
            err = validate(schema.get("items", {}), item, f"{path}[{i}]")
            if err:
                return err
        return None
    if kind == "string" and not isinstance(value, str):
        return f"{path} must be a string"
    if kind == "boolean" and not isinstance(value, bool):
        return f"{path} must be a boolean"
    if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{path} must be an integer"
    return None


def _obj(required: list[str], props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props}


_S = {"type": "string"}


def reviewer_tools(classes: tuple[str, ...], rereview: bool) -> list[HostTool]:
    finding = {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "class": {"type": "string", "enum": [*classes, "nit"],
                  "description": "the SEVERITY class; nit for anything that fits no blocking class"},
        "audit": {"type": "string", "description": "the audit that found it"},
        "location": {"type": "string", "description": "path:line or path:start-end at the head; 'PR body' or a commit sha when about those"},
        "claim": {"type": "string", "description": "what is wrong, with the evidence you checked"},
        "fix": {"type": "string", "description": "the fix, as a checklist the next review can tick"},
        "replacement": {"type": "string", "description": "optional: exact new text for the lines at location, "
                        "only when the fix is a few lines there; leave it out otherwise"},
    }
    if rereview:
        finding["reopens"] = {"type": "string", "description": "the id of the EARLIER FINDINGS entry this repeats, left unfixed"}
    return [
        HostTool("submit_finding", "Report ONE review finding. Call once per finding, before submit_review.",
                 _obj(["severity", "class", "audit", "location", "claim", "fix"], finding),
                 answer=lambda args, i: f"recorded as finding {i + 1}"),
        HostTool("submit_review", "Finish the review. Call exactly once, last, after every submit_finding.",
                 _obj(["strengths", "verified"], {
                     "strengths": {"type": "array", "items": _S},
                     "verified": {"type": "string", "description": "what you checked"},
                 }), terminal=True),
    ]


def oracle_tools(classes: tuple[str, ...], ids: list[str], rereview: bool) -> list[HostTool]:
    props = {
        "id": {"type": "string", "enum": ids},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "class": {"type": "string", "enum": [*classes, "nit"]},
        "evidence": {"type": "string", "description": "what you opened, searched or ran, and what it showed"},
        "fix": {"type": "string", "description": "confirmed/partial: the fix to apply; defer: where to record it; refuted: empty"},
    }
    if rereview:
        props["reopens"] = {"type": "string", "description": "the EARLIER FINDINGS id this finding repeats, left unfixed at the head"}
    return [
        HostTool("submit_verdict", "Give your verdict on ONE finding. Call once per finding id.",
                 _obj(["id", "verdict", "severity", "class", "evidence", "fix"], props)),
        HostTool("submit_review", "Finish the verification. Call exactly once, last, after a verdict for every finding.",
                 _obj(["summary"], {"summary": _S}), terminal=True),
    ]


def arbiter_tools(ids: list[str]) -> list[HostTool]:
    ruling = _obj(["id", "ruling", "reason"], {
        "id": {"type": "string", "enum": ids},
        "ruling": {"type": "string", "enum": ["uphold", "dismiss"],
                   "description": "uphold: it must block the merge; dismiss: it may merge as is"},
        "reason": _S,
    })

    def complete(args: dict) -> str | None:
        missing = set(ids) - {r["id"] for r in args["rulings"]}
        return f"rulings missing for: {', '.join(sorted(missing))}" if missing else None

    return [HostTool(
        "submit_arbitration", "Rule on every blocking finding. Call exactly once.",
        _obj(["rulings", "summary"], {"rulings": {"type": "array", "items": ruling}, "summary": _S}),
        terminal=True, check=complete,
    )]


def developer_tools() -> list[HostTool]:
    return [HostTool(
        "submit_resolution", "Report the conflict resolution. Call exactly once, when done.",
        _obj(["resolved", "files", "note", "checks"], {
            "resolved": {"type": "boolean", "description": "the rebase completed and the checks passed"},
            "files": {"type": "array", "items": _S},
            "note": {"type": "string", "description": "per file: what each side changed and how both are kept; not resolved: why"},
            "checks": {"type": "string", "description": "the checks you ran and their results"},
        }), terminal=True,
    )]


def resolve_review_tools() -> list[HostTool]:
    return [HostTool(
        "submit_review", "Approve or reject the resolution. Call exactly once.",
        _obj(["approved", "issues"], {
            "approved": {"type": "boolean"},
            "issues": {"type": "string", "description": "empty when approved; else path:line, why, fix for each issue"},
        }), terminal=True,
    )]


def probe_tools() -> list[HostTool]:
    return [HostTool("submit_review", "Report the tool check. Call exactly once.",
                     _obj(["summary"], {"summary": _S}), terminal=True)]


# -- the omp process ----------------------------------------------------------------
class OmpProcess:
    """`omp --mode rpc` as a line transport: a reader thread queues stdout lines."""

    _EOF = object()

    def __init__(self, argv: list[str], cwd: str, env: dict, stderr_path: str) -> None:
        os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
        self._err_path = stderr_path
        self._err = open(stderr_path, "a")  # noqa: SIM115 — lives as long as the process
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._err, text=True, bufsize=1,
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            if line.strip():
                self._lines.put(line.rstrip("\n"))
        self._lines.put(self._EOF)

    def write_line(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_line(self, timeout_s: float) -> str | None:
        try:
            item = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if item is self._EOF:
            self._lines.put(self._EOF)
            code = self.proc.wait()
            raise OmpError(f"omp exited with {code}: {self.stderr_tail()}")
        return item

    def stderr_tail(self, lines: int = 3) -> str:
        """Why omp stopped, from its own words (an unknown tool, a provider error)."""
        self._err.flush()
        try:
            with open(self._err_path) as fh:
                return " | ".join(x.strip() for x in fh.read().strip().split("\n")[-lines:] if x.strip())[:500]
        except OSError:
            return ""

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._err.close()


class AgentRunner:
    def __init__(
        self,
        sessions: SessionStore,
        *,
        session_root: str,
        omp_bin: str = "omp",
        spawn: Callable[[list[str], str, dict, str], object] | None = None,
        silence_s: float = 1200.0,
        command_timeout_s: float = 30.0,
        providers: Callable[[str, str], object | None] | None = None,
        mcp: McpLaunch | None = None,
        skill_stager: Callable[[str, str, dict], None] | None = None,
    ) -> None:
        self.sessions = sessions
        self.session_root = session_root
        self.omp_bin = omp_bin
        self._spawn = spawn or (lambda argv, cwd, env, err: OmpProcess(argv, cwd, env, err))
        self.silence_s = silence_s
        self.command_timeout_s = command_timeout_s
        # internals §13: `ProviderRepository.runtime(provider, model)`, read at every run so a
        # key saved in Admin applies to the next agent; None (or no item): the pod's env.
        self.providers = providers
        # A6: each agent gets its role's MCP servers through the gateway, with its own token.
        # The `--tools` allowlist stays; omp adds the MCP tools to it.
        self.mcp = mcp
        self.skill_stager = skill_stager
        self.pod = socket.gethostname()

    def environment(self, spec: AgentSpec, session_dir: str) -> dict:
        """The child's env: the pod's, plus the role's managed provider credential when there is
        one, staged as `models.yml` in a private agent dir (omp reads `PI_CODING_AGENT_DIR`).
        The key travels only in the env, never in a file, an argv or a log. With MCP, the agent
        dir is always private, so no user-level omp config applies."""
        env = dict(os.environ)
        runtime = None
        if self.providers is not None:
            runtime = self.providers(spec.model["provider"], spec.model["model"])
        if runtime is None and self.mcp is None:
            return env
        agent_dir = os.path.join(session_dir, "agent")
        os.makedirs(agent_dir, mode=0o700, exist_ok=True)
        if runtime is None:
            return {**env, "PI_CODING_AGENT_DIR": agent_dir}
        fd = os.open(os.path.join(agent_dir, "models.yml"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(runtime.models_config, fh)  # JSON is YAML
        return {**env, **runtime.environment, "PI_CODING_AGENT_DIR": agent_dir}

    def argv(self, spec: AgentSpec, session_dir: str) -> list[str]:
        return [
            self.omp_bin, "--mode", "rpc", "--no-title", "--auto-approve",
            "--session-dir", session_dir,
            "--model", model_arg(spec.model),
            "--thinking", spec.model.get("thinking", "medium"),
            "--tools", ",".join(spec.tools),
        ]

    def run(self, spec: AgentSpec) -> AgentResult:
        with owned_run_dir(os.path.join(self.session_root, spec.sid.replace(":", "_"))) as session_dir:
            return self._run(spec, session_dir)

    def _run(self, spec: AgentSpec, session_dir: str) -> AgentResult:
        self.sessions.open(
            spec.sid, kind=spec.kind, model=model_arg(spec.model), pod=self.pod,
            role=spec.role, **spec.meta,
        )
        run = _Run(self, spec)
        outcome = "failed"
        try:
            env = self.environment(spec, session_dir)
        except Exception as exc:  # noqa: BLE001 — a provider store outage fails this run only
            self.sessions.close(spec.sid, "failed")
            raise AgentFailed(f"{spec.sid}: no credential for {model_arg(spec.model)}: "
                              f"{getattr(exc, 'code', type(exc).__name__)}") from None
        token = None
        if self.mcp is not None:
            try:
                token = self.mcp.stage(write_private_file, env["PI_CODING_AGENT_DIR"], spec.role,
                                       spec.sid)
            except Exception as exc:  # noqa: BLE001 — an MCP store outage fails this run only
                self.sessions.close(spec.sid, "failed")
                raise AgentFailed(f"{spec.sid}: MCP servers could not be staged: "
                                  f"{getattr(exc, 'code', type(exc).__name__)}") from None
        # Stage central skills into the agent dir when configured.
        if self.skill_stager is not None:
            try:
                self.skill_stager(env.get("PI_CODING_AGENT_DIR", ""), spec.role, spec.meta)
            except Exception:  # noqa: BLE001 — skill staging never fails a run
                pass
        try:
            proc = self._spawn(self.argv(spec, session_dir), spec.cwd, env,
                               os.path.join(session_dir, "omp.stderr"))
        except OSError as exc:
            self.sessions.close(spec.sid, "failed")
            if self.mcp is not None:
                self.mcp.end(token)
            raise AgentFailed(f"{spec.sid}: omp did not start: {exc}") from exc
        run.proc = proc
        try:
            result = run.drive()
            outcome = result.status
            return result
        finally:
            run.stop.set()
            try:
                proc.close()
            finally:
                if self.mcp is not None:
                    self.mcp.end(token)
                self.sessions.close(spec.sid, outcome)


class _Run:
    def __init__(self, runner: AgentRunner, spec: AgentSpec) -> None:
        self.r = runner
        self.spec = spec
        self.sid = spec.sid
        self.proc = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.tools = {t.name: t for t in spec.host_tools}
        self.calls: dict[str, list[dict]] = {t.name: [] for t in spec.host_tools}
        self.responses: dict[str, dict] = {}
        self.watcher: RunWatcher | None = None
        self.n = 0
        self.last_entry: str | None = None
        self.terminal = next((t.name for t in spec.host_tools if t.terminal), None)
        self.finished = False

    # -- wire ----------------------------------------------------------------------
    def send(self, type_: str, params: dict | None = None) -> str:
        with self.lock:
            self.n += 1
            cid = f"m{self.n}"
            self.proc.write_line(json.dumps({"id": cid, "type": type_, **(params or {})}))
        return cid

    def reply(self, payload: dict) -> None:
        with self.lock:
            self.proc.write_line(json.dumps(payload))

    def command(self, type_: str, params: dict | None = None) -> dict:
        cid = self.send(type_, params)
        deadline = time.monotonic() + self.r.command_timeout_s
        while cid not in self.responses:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AgentFailed(f"{self.sid}: no response to {type_}")
            self.pump_one(min(left, 1.0))
        resp = self.responses.pop(cid)
        if not resp.get("success"):
            raise AgentFailed(f"{self.sid}: {type_} failed: {resp.get('error')}")
        return resp.get("data") or {}

    def pump_one(self, timeout: float):
        """Handles the next frame: (whether one came, the run's outcome once it is over)."""
        line = self.proc.read_line(timeout)
        if line is None:
            return False, None
        try:
            frame = json.loads(line)
        except ValueError:
            return True, None
        return True, self.handle(frame)

    def handle(self, frame: dict):
        kind = frame.get("type")
        if kind == "response":
            if frame.get("id"):
                self.responses[frame["id"]] = frame
        elif kind == "host_tool_call":
            self.host_call(frame)
        elif kind == "host_tool_cancel":
            pass
        elif not (kind == "extension_ui_request" and frame.get("method") == "setWidget"):
            self.r.sessions.append_event(self.sid, kind or "unknown", frame)
            if kind == "message_end":
                self.note_message(frame.get("message") or {})
            elif kind == "auto_retry_start" and THROTTLED.search(frame.get("errorMessage") or ""):
                record_rate_limit(self.r.sessions, self.spec.model, {
                    "stage": "throttled", "retry_after_ms": int(frame.get("delayMs") or 0), "source": "retry"})
        if self.watcher is not None:
            return self.watcher.observe(frame)
        return None

    def note_message(self, message: dict) -> None:
        if message.get("role") != "assistant":
            return
        parts = message.get("content") or []
        text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text").strip()
        if text:
            self.r.sessions.update(self.sid, last_message=text[:280])

    def host_call(self, frame: dict) -> None:
        name, args = frame.get("toolName"), frame.get("arguments") or {}
        tool = self.tools.get(name)
        if tool is None:
            err = f"unknown tool {name!r}"
        else:
            err = validate(tool.parameters, args) or (tool.check(args) if tool.check else None)
            if not err and tool.terminal and self.calls[name]:
                # Already recorded: the agent has delivered. Some models keep calling it in a
                # loop when told it is an error (sandbox: kimi-k3.1 produced 5000 events), so the
                # answer is a plain "done" and the run is stopped below.
                self.reply({"type": "host_tool_result", "id": frame["id"],
                            "result": {"content": [{"type": "text", "text": "already recorded: you are done"}]},
                            "isError": False})
                self.finish_early()
                return
        if err:
            text, is_error = f"rejected: {err}. Call it again with valid arguments.", True
        else:
            index = len(self.calls[name])
            self.calls[name].append(args)
            try:
                text = tool.answer(args, index) if tool.answer else "recorded"
                is_error = False
            except HostToolError as exc:
                # Not carried out, so not recorded: the agent may call it again.
                self.calls[name].pop()
                text, is_error = str(exc), True
        self.reply({"type": "host_tool_result", "id": frame["id"],
                    "result": {"content": [{"type": "text", "text": text}]}, "isError": is_error})
        if not is_error and tool.terminal:
            self.finish_early()

    def finish_early(self) -> None:
        """The terminal tool is recorded: nothing the agent does after it can count, so the
        turn is aborted rather than left to run (or loop) until the silence timeout."""
        if not self.finished:
            self.finished = True
            self.send("abort")

    # -- intents ------------------------------------------------------------------
    def intents(self) -> None:
        after = self.r.sessions.last_intent_id(self.sid)
        while not self.stop.is_set():
            try:
                rows = self.r.sessions.read_intents(self.sid, after, block_ms=1000)
            except Exception:  # noqa: BLE001 — Redis blip: try again, the run goes on
                self.stop.wait(1)
                continue
            for iid, kind, _actor, payload in rows:
                after = iid
                if kind not in ("steer", "follow_up", "interrupt"):
                    self.r.sessions.intent_applied(self.sid, iid, kind, ignored=True)
                    continue
                try:
                    if kind == "interrupt":
                        self.send("abort")
                    else:
                        self.send(kind, {"message": payload.get("text", "")})
                except OSError:  # omp already exited: the run is over
                    return
                self.r.sessions.intent_applied(self.sid, iid, kind)

    def sync_transcript(self) -> None:
        params = {"since": self.last_entry} if self.last_entry else {}
        try:
            data = self.command("get_entries", params)
        except (AgentFailed, OmpError):
            return
        entries = data.get("entries") or []
        if entries:
            self.r.sessions.append_transcript(self.sid, entries)
            self.last_entry = entries[-1].get("id", self.last_entry)

    def sync_usage_limit(self) -> None:
        """internals §10: the account's usage-limit stage after each turn (`get_state`)."""
        try:
            limit = self.command("get_state").get("usageLimit")
        except (AgentFailed, OmpError):
            return
        if limit:
            doc = {"stage": limit.get("stage", "unknown"), "source": "usage_limit"}
            if limit.get("resetsAtSec"):
                doc["reset_at"] = int(limit["resetsAtSec"]) * 1000
            if limit.get("allowanceLeftPercent") is not None:
                doc["remaining"] = limit["allowanceLeftPercent"]
                doc["limit"] = 100
            record_rate_limit(self.r.sessions, self.spec.model, doc)

    def prompt(self, message: str, *, images: tuple[dict, ...] = ()) -> None:
        params: dict = {"message": message}
        if images:
            params["images"] = list(images)
        cid = self.send("prompt", params)
        self.watcher = RunWatcher(cid)

    def turn(self):
        """Frames until the prompt's run is over. Silence longer than `silence_s` fails it."""
        last_seen = time.monotonic()
        while True:
            saw, outcome = self.pump_one(1.0)
            if outcome is not None:
                self.watcher = None
                return outcome
            if saw:
                last_seen = time.monotonic()
            elif time.monotonic() - last_seen > self.r.silence_s:
                raise AgentFailed(f"omp silent for {self.r.silence_s:.0f}s")

    def drive(self) -> AgentResult:
        threading.Thread(target=self.intents, daemon=True).start()
        try:
            self.command("set_event_filter", {"events": None, "messageUpdates": "delta"})
            self.command("set_host_tools", {"tools": [t.definition() for t in self.spec.host_tools]})
            self.prompt(self.spec.prompt)
            self.r.sessions.update(self.sid, state="running")
            reminded = False
            while True:
                outcome = self.turn()
                self.r.sessions.update(self.sid, state="idle")
                self.sync_transcript()
                self.sync_usage_limit()
                if outcome.status == "aborted":
                    if self.finished and self.calls[self.terminal]:
                        return AgentResult(self.sid, "completed", self.calls)
                    return AgentResult(self.sid, "aborted", self.calls, "interrupted")
                if outcome.status != "completed":
                    return AgentResult(self.sid, "failed", self.calls, outcome.error_message)
                if self.terminal is None or self.calls[self.terminal]:
                    return AgentResult(self.sid, "completed", self.calls)
                if reminded:
                    return AgentResult(self.sid, "failed", self.calls,
                                       f"the agent ended without calling {self.terminal}")
                reminded = True
                self.r.sessions.update(self.sid, state="running")
                self.prompt(f"You ended without calling {self.terminal}. Call the tools as asked: "
                            f"finish by calling {self.terminal} exactly once. Do not write prose.")
        except (OmpError, AgentFailed) as exc:
            return AgentResult(self.sid, "failed", self.calls, str(exc))
