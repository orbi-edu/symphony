"""Host tools the runner serves to omp over RPC (`set_host_tools`, internals §3).

The agent calls these like any other tool; omp forwards each call as a `host_tool_call`
frame and waits for our `host_tool_result`. Two consequences shape this module:

- **Credentials stay here.** The tracker's raw tool (`linear_graphql`),
  `symphony_create_issues` and `symphony_attach_evidence` run through the tracker adapter in
  the runner process, so the tracker credential never enters the workspace pod's
  environment, argv or files.
- **Nothing blocks the reader.** Each call is served on its own worker thread: the turn
  loop keeps forwarding events, and a pending `symphony_ask` waits for a human `answer`
  intent (or `cfg.input_timeout_ms`) without stalling the stream or the intent pump.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import questions, state
from .sessions import SessionStore
from .trackers.base import SubIssue, TrackerAdapter, TrackerInputError
from .workspace import SandboxLike


def _now_ms() -> int:
    return int(time.time() * 1000)


ASK_TOOL = "symphony_ask"
ASK_FALLBACK = (
    "Nobody answered in time. The question stays open on the ticket and in Slack, and the "
    "person who decides is reminded later. Proceed with your best judgment and record the "
    "assumption in the Workpad. An answer that comes while you still work reaches you as a "
    "message; one that comes after this run sends the ticket back to Rework with it."
)
ASK_QUESTION_MAX = 1200
ASK_OPTIONS_MAX = 5
ASK_OPTION_MAX = 60
ASK_DEFINITION = {
    "name": ASK_TOOL,
    "label": "Ask a human",
    "description": (
        "Ask a person a question and wait for their answer. Use it only when you cannot "
        "proceed responsibly without a human decision (ambiguous requirements, risky or "
        "irreversible choices). Symphony posts the question on the ticket and in Slack, "
        "mentioning `to`, and reminds them later: do not post it as a comment yourself. "
        "Write for a busy person reading Slack: the first sentence is the decision you need; "
        "then at most three short lines of context. Several sub-questions go one per line, "
        "numbered `1.` `2.`. Options are short (they become buttons: at most 5, 40 characters "
        "each); do not repeat them inside the question. Ask one decision at a time; if a "
        "question on this ticket is already open, wait for its answer unless the new one "
        "blocks the work on its own. If nobody answers in time you get an instruction to "
        "proceed with your best judgment; a later answer still reaches the work. Never ask for "
        "credentials, keys, tokens or passwords: ask a person to run the command in an "
        "authorized environment and post only the output."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The decision needed, in the first sentence; at most three short lines "
                    "of context; sub-questions one per line. At most 1200 characters."
                ),
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Suggested answers, when the choice is between known options: at most 5, "
                    "each a short label (40 characters) that becomes a button."
                ),
            },
            "to": {
                "type": "string",
                "description": (
                    "Who decides: the name or e-mail of a person on the ticket (its author, "
                    "assignee or someone who commented). Default: the ticket's author."
                ),
            },
        },
    },
    "loadMode": "essential",
}
BLOCKED_TOOL = "symphony_blocked"
BLOCKED_NEEDS = ("answer", "access", "decision", "other")
BLOCKED_REASON_MAX = 500
BLOCKED_RESULT = (
    "Recorded. This run ends when your turn ends, and Symphony leaves one comment on the "
    "ticket saying what it waits for. The ticket starts again when a person answers your "
    "question, comments, or changes its labels or state. Update the Workpad if needed, then "
    "end your turn: do not post a 'blocked' comment yourself."
)
BLOCKED_DEFINITION = {
    "name": BLOCKED_TOOL,
    "label": "Wait for a person",
    "description": (
        "Stop working until a person acts, when you cannot make progress without one: an "
        "unanswered question, missing access (a repository or service you cannot reach), a "
        "decision only a person can make. The run ends at the end of this turn and the "
        "ticket waits, without new runs, until someone answers, comments or changes the "
        "ticket. Call it instead of ending turns with 'blocked' messages. Ask with "
        "symphony_ask first when one question would unblock you."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reason", "needs"],
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "What you are waiting for, in one or two sentences a person can act on. "
                    "At most 500 characters."
                ),
            },
            "needs": {"enum": list(BLOCKED_NEEDS)},
        },
    },
    "loadMode": "essential",
}
# "(1) … (2) … (3) …" or "1) … 2) … 3) …" on one line: a list squeezed into a paragraph.
_INLINE_ITEM = re.compile(r"(?:^|(?<=\s))\(?\d{1,2}\)(?=\s)")


def _ask_problem(question: str, options: list[str]) -> str | None:
    """Why a question would be hard to read on Slack, for the agent to rewrite it."""
    if len(question) > ASK_QUESTION_MAX:
        return (f"symphony_ask: the question is too long for a person to read on Slack "
                f"({len(question)} characters, at most {ASK_QUESTION_MAX}): keep the decision "
                "in the first sentence, at most three lines of context, sub-questions one per "
                "line.")
    if any(len(_INLINE_ITEM.findall(line)) >= 3 for line in question.splitlines()):
        return ("symphony_ask: the question lists sub-questions inline, as \"(1) … (2) …\": "
                "put each on its own line, numbered `1.` `2.`.")
    if len(options) > ASK_OPTIONS_MAX:
        return (f"symphony_ask: {len(options)} options, at most {ASK_OPTIONS_MAX}: they become "
                "buttons. Keep the choices that matter, or ask one decision at a time.")
    long = [o for o in options if len(o.strip()) > ASK_OPTION_MAX]
    if long:
        return (f"symphony_ask: an option is over {ASK_OPTION_MAX} characters ({long[0][:40]!r}…): "
                "options become buttons; use a short label and put the detail in the question.")
    return None


# -- secret-request guard ---------------------------------------------------------

_SECRET_ENVVAR_RE = re.compile(
    r"[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*",
)

# Known dangerous env-var names that alone trigger refusal.
_KNOWN_SECRET_VARS = frozenset({
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
})

# Request verbs (English / Portuguese), whole words only — "post the output" or "the column"
# must not count.
_VERB_PATTERN = (
    r"\b(?:share|send|provide|paste|give|grant|expose|reveal|disclose"
    r"|disponibiliz\w*|forne[cç]\w*|envi(?:ar|e|em|a|ou)|mand(?:ar|e|em|a|ou)"
    r"|pass(?:ar|e|em|a|ou)|col(?:ar|e|em|a|ou)|compartilh\w*)\b"
)

# Secret nouns (English / Portuguese), whole words only.
_NOUN_PATTERN = (
    r"\b(?:credentials?|access\s+keys?|secret(?:\s+keys?)?|tokens?|passwords?|api\s+keys?"
    r"|private\s+keys?|credencia(?:l|is)|chaves?|senhas?)\b"
)

# A request verb near a secret noun, in either order.
_VERB_NOUN_RE = re.compile(
    rf"{_VERB_PATTERN}.{{0,60}}{_NOUN_PATTERN}"
    rf"|{_NOUN_PATTERN}.{{0,60}}{_VERB_PATTERN}",
    re.IGNORECASE | re.DOTALL,
)

# A direct question for a secret's value ("What is the database password?", "Qual é a senha
# do banco?"). A question about the secret's handling ("the password hash") is not one.
_VALUE_QUESTION_RE = re.compile(
    rf"\b(?:what(?:'s|\s+is|\s+are)|qual\s+(?:é|e)|quais\s+são)\s+(?:\w+\s+){{0,4}}?{_NOUN_PATTERN}"
    r"(?!\s+(?:hash\w*|polic\w*|format\w*|length|rules?|rotation|expir\w*|reset|fields?|columns?))",
    re.IGNORECASE,
)

_SECRET_REFUSAL = (
    "Refused: never ask for credentials, keys or tokens in chat. Instead, ask a person to "
    "run the exact command in an environment authorized for it and post only the output to "
    "the ticket or PR. Record in the Workpad that this check was not run by the agent."
)


def _is_secret_request(text: str) -> bool:
    """True when the question asks people to share secrets."""
    # (a) Explicit secret env-var names (uppercase identifiers).
    for m in _SECRET_ENVVAR_RE.finditer(text):
        token = m.group(0)
        if token in _KNOWN_SECRET_VARS:
            return True
        # Generic pattern: only flag when surrounded by word-boundary-like context
        # (not part of a longer lowercase word) and at least 6 chars.
        start, end = m.start(), m.end()
        before_ok = start == 0 or not text[start - 1].isalpha()
        after_ok = end == len(text) or not text[end].isalpha()
        if before_ok and after_ok and len(token) >= 6:
            return True
    # (b) Request verb near a secret noun, or a direct question for the value.
    return bool(_VERB_NOUN_RE.search(text) or _VALUE_QUESTION_RE.search(text))

CREATE_ISSUES_TOOL = "symphony_create_issues"
CREATE_ISSUES_DEFINITION = {
    "name": CREATE_ISSUES_TOOL,
    "label": "Create sub-issues",
    "description": (
        "Split the current ticket into sub-issues on the tracker. Each one becomes a child of this "
        "ticket, inherits its repo: labels and the required label, and is blocked by the issues "
        "named in blocked_by (keys from this call or an earlier one). Symphony only dispatches "
        "the unblocked ones. Calling again with the same keys never creates duplicates. Returns "
        "[{key, identifier, url}]."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["issues"],
        "properties": {
            "issues": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "title", "description"],
                    "properties": {
                        "key": {"type": "string", "description": "Your stable name for it."},
                        "title": {"type": "string"},
                        "description": {"type": "string", "description": "Markdown."},
                        "labels": {"type": "array", "items": {"type": "string"}},
                        "blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keys of the sub-issues that must finish first.",
                        },
                    },
                },
            }
        },
    },
    "loadMode": "essential",
}

EVIDENCE_TOOL = "symphony_attach_evidence"
EVIDENCE_KINDS = ("screenshot", "video", "log", "report")
EVIDENCE_MAX_BYTES = 25 * 1024 * 1024
EVIDENCE_DEFINITION = {
    "name": EVIDENCE_TOOL,
    "label": "Attach evidence",
    "description": (
        "Attach a file from the workspace to the ticket as proof of work (a screenshot of the "
        "change, a test log, a recording, a report). At most 25 MB; the path must be inside "
        "the workspace. Returns {url}."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "title", "kind"],
        "properties": {
            "path": {"type": "string", "description": "Relative to the workspace root."},
            "title": {"type": "string"},
            "kind": {"enum": list(EVIDENCE_KINDS)},
        },
    },
    "loadMode": "essential",
}

_DEFAULT_TYPES = {
    "screenshot": "image/png",
    "video": "video/mp4",
    "log": "text/plain",
    "report": "text/markdown",
}


class ToolInputError(Exception):
    """The agent's arguments cannot be served; the message tells it what to fix."""


def tool_definitions(tracker: TrackerAdapter) -> list[dict]:
    """The `set_host_tools` payload: every tracker tool, the tracker-backed Symphony tools
    (`symphony_create_issues`, `symphony_attach_evidence`), `symphony_ask` and
    `symphony_blocked`. Essential load mode keeps them top-level, so workflow prompts can name
    them directly."""
    tools = [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema or {"type": "object"},
            "loadMode": "essential",
        }
        for spec in tracker.agent_tool_specs()
    ]
    return [*tools, CREATE_ISSUES_DEFINITION, EVIDENCE_DEFINITION, ASK_DEFINITION,
            BLOCKED_DEFINITION]


def subissue_key(issue_id: str, key: str) -> str:
    """Idempotency record for one sub-issue of a ticket — across retries and attempts."""
    return f"subissue:{issue_id}:{key}"


def relation_key(issue_id: str, blocker: str, blocked: str) -> str:
    return f"subissue:{issue_id}:{blocker}>{blocked}"


def _dependency_order(items: list[dict], known: set[str]) -> list[dict]:
    """Blockers before the issues they block; a cycle or an unknown key is refused."""
    by_key = {item["key"]: item for item in items}
    for item in items:
        for blocker in item.get("blocked_by", ()):
            if blocker not in by_key and blocker not in known:
                raise ToolInputError(f"{item['key']}: blocked_by names an unknown key {blocker!r}")
            if blocker == item["key"]:
                raise ToolInputError(f"{item['key']} cannot block itself")
    ordered: list[dict] = []
    state_of: dict[str, str] = {}

    def visit(key: str, path: tuple[str, ...]) -> None:
        if state_of.get(key) == "done" or key not in by_key:
            return
        if state_of.get(key) == "visiting":
            raise ToolInputError("blocked_by forms a cycle: " + " -> ".join((*path, key)))
        state_of[key] = "visiting"
        for blocker in by_key[key].get("blocked_by", ()):
            visit(blocker, (*path, key))
        state_of[key] = "done"
        ordered.append(by_key[key])

    for item in items:
        visit(item["key"], ())
    return ordered


def _validate_issues(args: dict) -> list[dict]:
    raw = args.get("issues")
    if not isinstance(raw, list) or not raw:
        raise ToolInputError("issues must be a non-empty array")
    items, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ToolInputError("each issue must be an object")
        key = str(entry.get("key") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not key or not title:
            raise ToolInputError("each issue needs a key and a title")
        if key in seen:
            raise ToolInputError(f"duplicate key {key!r}")
        seen.add(key)
        labels = entry.get("labels") or []
        blocked_by = entry.get("blocked_by") or []
        if not all(isinstance(x, str) for x in labels) or not all(
            isinstance(x, str) for x in blocked_by
        ):
            raise ToolInputError(f"{key}: labels and blocked_by must be arrays of strings")
        items.append(
            {
                "key": key,
                "title": title,
                "description": str(entry.get("description") or ""),
                "labels": list(dict.fromkeys(labels)),
                "blocked_by": list(dict.fromkeys(blocked_by)),
            }
        )
    return items


class _KVRecords:
    """`SubIssueRecords` for one ticket, in the runner's KV (Redis)."""

    def __init__(self, kv: state.KV, issue_id: str) -> None:
        self.kv, self.issue_id = kv, issue_id

    def issue(self, key: str) -> dict | None:
        return self.kv.get(subissue_key(self.issue_id, key))

    def save_issue(self, key: str, record: dict) -> None:
        self.kv.put(subissue_key(self.issue_id, key), record)

    def relation(self, blocker: str, blocked: str) -> bool:
        return self.kv.get(relation_key(self.issue_id, blocker, blocked)) is not None

    def save_relation(self, blocker: str, blocked: str) -> None:
        self.kv.put(relation_key(self.issue_id, blocker, blocked), True)


@dataclass
class _Ask:
    done: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    by: str | None = None
    cancelled: bool = False


class HostToolBroker:
    """Serves `host_tool_call` frames for one attempt.

    `respond(call_id, text, is_error)` writes the result back to omp (thread-safe);
    `owns()` re-checks dispatch ownership so a superseded attempt cannot mutate the
    ticket; `notify_link` is where a Slack `input_requested` points people.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        sid: str,
        kv: state.KV,
        tracker: TrackerAdapter,
        issue_id: str,
        seq: int,
        identifier: str,
        respond: Callable[..., None],
        owns: Callable[[], bool],
        input_timeout_s: float,
        notify_link: str | None = None,
        log: Callable[..., None] = lambda event, **kw: None,
        sandbox: SandboxLike | None = None,
        workspace_root: str = "/workspace",
        required_labels: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.sid = sid
        self.kv = kv
        self.tracker = tracker
        self.issue_id = issue_id
        self.seq = seq
        self.identifier = identifier
        self._respond = respond
        self._owns = owns
        self.input_timeout_s = input_timeout_s
        self.notify_link = notify_link
        self._log = log
        self.sandbox = sandbox
        self.workspace_root = workspace_root
        self._inherit = tuple(required_labels)
        self._ticket_lock = threading.Lock()  # one ticket mutation at a time
        self._lock = threading.Lock()
        self._asks: dict[str, _Ask] = {}
        self._threads: list[threading.Thread] = []
        self._known = {spec.name for spec in tracker.agent_tool_specs()}
        self._closed = False
        # `symphony_blocked`'s request, `{reason, needs}`: the turn loop ends the run at the
        # end of the turn in which it was set.
        self.blocked: dict | None = None

    # -- dispatch ----------------------------------------------------------------
    def handle(self, frame: dict) -> None:
        """Route one engine frame; non-host-tool frames are ignored."""
        kind = frame.get("type")
        if kind == "host_tool_call":
            worker = threading.Thread(
                target=self._serve, args=(frame,), name=f"hosttool-{frame.get('id')}", daemon=True
            )
            with self._lock:
                self._threads = [t for t in self._threads if t.is_alive()]
                self._threads.append(worker)
            worker.start()
        elif kind == "host_tool_cancel":
            self._cancel(str(frame.get("targetId") or ""))

    def _serve(self, frame: dict) -> None:
        call_id = str(frame.get("id") or "")
        name = frame.get("toolName")
        args = frame.get("arguments") if isinstance(frame.get("arguments"), dict) else {}
        try:
            if name == ASK_TOOL:
                self._ask(call_id, args)
            elif name == BLOCKED_TOOL:
                self._blocked(call_id, args)
            elif name in (CREATE_ISSUES_TOOL, EVIDENCE_TOOL):
                if not self._owns():
                    self._respond(
                        call_id, "this attempt no longer owns the issue; stop working on it",
                        is_error=True,
                    )
                    return
                try:
                    if name == CREATE_ISSUES_TOOL:
                        result = self.create_issues(args)
                    else:
                        result = self.attach_evidence(args)
                except (ToolInputError, TrackerInputError) as exc:
                    self._respond(call_id, str(exc), is_error=True)
                    return
                self._respond(call_id, json.dumps(result, ensure_ascii=False), is_error=False)
            elif name in self._known:
                text, is_error = self._tracker_tool(str(name), str(frame.get("toolCallId") or call_id), args)
                self._respond(call_id, text, is_error=is_error)
            else:
                self._respond(call_id, f"unknown host tool {name!r}", is_error=True)
        except Exception as exc:  # noqa: BLE001 — a tool failure is a result, never a crash
            self._log("host_tool_failed", tool=name, error=str(exc))
            try:
                self._respond(call_id, f"host tool failed: {exc}", is_error=True)
            except Exception:  # noqa: BLE001 — the engine is gone; nothing to answer
                pass

    # -- tracker tools -------------------------------------------------------------
    def _tracker_tool(self, name: str, tool_call_id: str, args: dict) -> tuple[str, bool]:
        if not self._owns():
            return "this attempt no longer owns the issue; stop working on it", True
        # Keyed on the engine's tool-call id, stable across replay: a replayed mutation
        # answers from cache instead of executing twice (idempotency, FR-013).
        cache_key = state.toolcall_key(self.issue_id, self.seq, tool_call_id)
        cached = self.kv.get(cache_key)
        if cached is not None:
            return json.dumps(cached["result"], ensure_ascii=False), False
        try:
            result = self.tracker.execute_agent_tool(name, args)
        except Exception as exc:  # noqa: BLE001 — outages are retryable tool errors
            return f"{name} failed: {exc}", True
        self.kv.put(cache_key, {"result": result})
        return json.dumps(result, ensure_ascii=False), False

    # -- tracker-backed Symphony tools ---------------------------------------------
    def create_issues(self, args: dict) -> list[dict]:
        """`symphony_create_issues`: sub-issues of this ticket, idempotent per (ticket, key)."""
        items = _validate_issues(args)
        records = _KVRecords(self.kv, self.issue_id)
        with self._ticket_lock:
            known = {
                blocker
                for item in items
                for blocker in item["blocked_by"]
                if records.issue(blocker) is not None
            }
            ordered = [
                SubIssue(key=item["key"], title=item["title"], description=item["description"],
                         labels=tuple(item["labels"]), blocked_by=tuple(item["blocked_by"]))
                for item in _dependency_order(items, known)
            ]
            made = self.tracker.create_sub_issues(
                self.issue_id, ordered, labels=self._inherit, records=records
            )
        by_key = {entry["key"]: entry for entry in made}
        return [by_key[item["key"]] for item in items]

    def attach_evidence(self, args: dict) -> dict:
        """`symphony_attach_evidence`: workspace file → the tracker's evidence on the ticket."""
        path = str(args.get("path") or "").strip()
        title = str(args.get("title") or "").strip()
        kind = args.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ToolInputError(f"kind must be one of {', '.join(EVIDENCE_KINDS)}")
        if not path or "\x00" in path or not title:
            raise ToolInputError("path and title are required")
        if self.sandbox is None:
            raise ToolInputError("no workspace is attached to this attempt")
        data = self._read_workspace_file(path)
        filename = os.path.basename(path)
        attached = self.tracker.attach_evidence(
            self.issue_id, title=title, kind=kind, filename=filename,
            content_type=mimetypes.guess_type(filename)[0] or _DEFAULT_TYPES[kind], data=data,
        )
        with self._ticket_lock:
            meta = self.store.meta(self.sid) or {}
            self.store.update(self.sid, evidence=[*(meta.get("evidence") or []), attached])
        return {"url": attached["url"]}

    def _read_workspace_file(self, path: str) -> bytes:
        """Read a file through the sandbox, refusing anything that resolves (symlinks
        included) outside the workspace root, and anything over the size cap."""
        script = (
            f"root=$(cd {shlex.quote(self.workspace_root)} 2>/dev/null && pwd -P) "
            "|| { echo symphony:no-root; exit 2; }\n"
            'cd "$root" || exit 2\n'
            f"p=$(realpath -- {shlex.quote(path)} 2>/dev/null) || {{ echo symphony:missing; exit 3; }}\n"
            'case "$p" in "$root"/*) ;; *) echo symphony:outside; exit 4;; esac\n'
            '[ -f "$p" ] || { echo symphony:missing; exit 3; }\n'
            "size=$(wc -c < \"$p\" | tr -d ' ')\n"
            f'[ "$size" -le {EVIDENCE_MAX_BYTES} ] || {{ echo "symphony:too-large $size"; exit 5; }}\n'
            'echo "symphony:size $size"\n'
            'base64 < "$p"\n'
        )
        code, out = self.sandbox.exec_script(script, timeout_s=300)
        refusals = {
            3: f"no such file in the workspace: {path}",
            4: f"refused: {path} resolves outside the workspace",
            5: f"refused: {path} is larger than 25 MB",
        }
        if code in refusals:
            raise ToolInputError(refusals[code])
        lines = out.splitlines()
        marker = next((i for i, line in enumerate(lines) if line.startswith("symphony:size ")), None)
        if code != 0 or marker is None:
            raise RuntimeError(f"could not read {path} from the workspace (exit {code})")
        size = int(lines[marker].split()[1])
        data = base64.b64decode("".join(lines[marker + 1 :]))
        if len(data) != size:
            raise RuntimeError(f"short read of {path}: {len(data)} of {size} bytes")
        return data

    # -- symphony_blocked ----------------------------------------------------------
    def _blocked(self, call_id: str, args: dict) -> None:
        reason = " ".join(str(args.get("reason") or "").split())
        needs = args.get("needs")
        if not reason or needs not in BLOCKED_NEEDS:
            self._respond(
                call_id,
                "symphony_blocked needs a reason and `needs`, one of " + ", ".join(BLOCKED_NEEDS),
                is_error=True,
            )
            return
        if len(reason) > BLOCKED_REASON_MAX:
            self._respond(call_id, f"reason is over {BLOCKED_REASON_MAX} characters; shorten it",
                          is_error=True)
            return
        if not self._owns():
            self._respond(call_id, "this attempt no longer owns the issue; stop working on it",
                          is_error=True)
            return
        self.blocked = {"reason": reason, "needs": needs}
        self._log("agent_blocked_on_person", issue_id=self.issue_id, needs=needs)
        self._respond(call_id, BLOCKED_RESULT, is_error=False)

    # -- symphony_ask --------------------------------------------------------------
    def _ask(self, call_id: str, args: dict) -> None:
        question = str(args.get("question") or "").strip()
        if not question:
            self._respond(call_id, "symphony_ask needs a non-empty question", is_error=True)
            return
        raw_options = args.get("options")
        options = [str(o) for o in raw_options] if isinstance(raw_options, list) else []
        if any(_is_secret_request(text) for text in (question, *options)):
            self._respond(call_id, _SECRET_REFUSAL, is_error=True)
            return
        problem = _ask_problem(question, options)
        if problem:
            self._respond(call_id, problem, is_error=True)
            return
        pending = _Ask()
        with self._lock:
            if self._closed:
                return
            self._asks[call_id] = pending
        self.store.request_input(self.sid, call_id, question, options)
        self._record(call_id, question, options, str(args.get("to") or "").strip() or None)
        answered = pending.done.wait(self.input_timeout_s)
        with self._lock:
            self._asks.pop(call_id, None)
        if pending.cancelled:
            self.store.resolve_input(self.sid, call_id, answer=None, by="cancelled")
            return
        if answered:
            by = pending.by or "human"
            self.store.resolve_input(self.sid, call_id, answer=pending.answer, by=by)
            self._close_question(call_id, pending.answer or "", by)
            self._respond(call_id, pending.answer or "", is_error=False)
            return
        self.store.resolve_input(self.sid, call_id, answer=None, by="timeout")
        self._respond(call_id, ASK_FALLBACK, is_error=False)

    def _record(self, call_id: str, question: str, options: list[str], to: str | None) -> None:
        """The question outlives this run (`questions.py`): the daemon posts it on the ticket
        and in Slack, reminds the person and delivers a late answer."""
        try:
            questions.ask(self.store.client, self.store.ns, qid=call_id, issue_id=self.issue_id,
                          identifier=self.identifier, sid=self.sid, url=self.notify_link,
                          question=question, options=options, to=to, now_ms=_now_ms())
        except Exception as exc:  # noqa: BLE001 — Tern still shows it; the agent never blocks on it
            self._log("question_record_failed", error=str(exc))

    def _close_question(self, call_id: str, answer: str, by: str) -> None:
        """Answered here (Tern, the CLI, or a delivered answer, which already won): the ticket
        and Slack say so."""
        try:
            questions.submit(self.store.client, self.store.ns, call_id, answer, by=by,
                             via="symphony", now_ms=_now_ms(), delivered="answer")
        except Exception as exc:  # noqa: BLE001 — the run has its answer; the record is advice
            self._log("question_close_failed", error=str(exc))

    def pending_request(self) -> str | None:
        with self._lock:
            return next(iter(self._asks), None)

    def answer(self, request_id: str | None, text: str, by: str) -> bool:
        """Complete a pending ask. Without a `request_id` the oldest pending one is answered.
        False when nothing matches (it timed out, was cancelled, or never existed)."""
        with self._lock:
            rid = request_id if request_id in self._asks else None
            if rid is None and not request_id:
                rid = next(iter(self._asks), None)
            pending = self._asks.pop(rid, None) if rid is not None else None
        if pending is None:
            return False
        pending.answer, pending.by = text, by
        pending.done.set()
        return True

    def _cancel(self, target_id: str) -> None:
        with self._lock:
            pending = self._asks.pop(target_id, None)
        if pending is not None:
            pending.cancelled = True
            pending.done.set()

    def close(self, timeout_s: float = 5.0) -> None:
        """Release every waiting ask (the engine is stopping) and join the workers."""
        with self._lock:
            self._closed = True
            asks, self._asks = list(self._asks.values()), {}
            threads = list(self._threads)
        for pending in asks:
            pending.cancelled = True
            pending.done.set()
        for worker in threads:
            worker.join(timeout_s)
