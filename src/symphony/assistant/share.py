"""`share_file`: a file the agent made in the thread's sandbox, posted in the thread, and on
request proposed as evidence on a ticket.

- **Reading** (`read_file`): one script in the pod resolves the path with `realpath`, so a
  symlink counts where it points, and reads the file only when it lies under a clone of the
  thread (`/workspace/<owner>/<name>`) or under `/tmp`, outside every `.git` directory and
  the credential files and directories (`CREDENTIAL_GLOBS`), is a regular file and is at
  most `MAX_BYTES`. The bytes come back base64 over the exec stream. A file whose bytes match
  one of the merger precheck's secret patterns (`merger.rules.SECRETS`) is refused.
- **Posting:** the transport's `upload` (Slack's external upload, Discord's multipart Create
  Message), in the thread the assistant answers in. The audit entry `assistant.file_shared`
  names the asker's GitHub login, the file's name and size.
- **Evidence:** with `ticket`, the file is also proposed for that ticket. The proposal
  (`ProposalStore` kind `evidence`) and the bytes beside it live one hour, and only files up
  to `EVIDENCE_MAX_BYTES` wait. The thread sees "Attach" and "Dismiss"; nothing reaches the
  tracker until a person mapped in `notify.users` clicks Attach. A claim key lets one click
  work at a time, and the proposal's status, read again under the claim, attaches it once.
  The audit entry `assistant.evidence_attached` names that person.
- **Proposal files** (`proposal_attacher`): the `attachments` of a `propose_tickets` call go
  through the same reader when the call is made, at most `tools.MAX_ATTACHMENTS` files and
  `tools.MAX_ATTACHMENT_BYTES` per proposal, and wait in `confirm.ProposalFiles` for Create.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import posixpath
import re
import shlex
from collections.abc import Callable, Sequence

from ..merger.agents import HostTool, HostToolError, _obj
from ..merger.rules import SECRETS
from . import Action, Button, FileTooLarge, Outbound, Ref
from .confirm import ProposalFiles, ProposalStore, mapped_login
from .sandbox import NEEDS_MAPPING, SandboxError
from .tools import MAX_ATTACHMENT_BYTES

TOOL = "share_file"
MAX_BYTES = 25 * 1024 * 1024
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
EVIDENCE_TTL_S = 3600
READ_TIMEOUT_S = 300
SCRATCH_ROOTS = ("/tmp",)
MAX_TITLE = 200
MAX_COMMENT = 1000
TICKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.#/-]{0,99}\Z")
SHARE_AUDIT = "assistant.file_shared"
EVIDENCE_AUDIT = "assistant.evidence_attached"
ONLY_MAPPED = "Only people mapped in notify.users can attach files to tickets."
CHOICES = ("attach", "dismiss")
# Matched against the resolved path plus a trailing "/": a directory and everything in it,
# or one file, wherever it is (a home directory may sit under a readable root).
CREDENTIAL_GLOBS = ("*/.git/*", "*/.ssh/*", "*/.aws/*", "*/.kube/*", "*/.config/gh/*",
                    "*/.git-credentials/", "*/.netrc/", "*/.npmrc/")
TOO_LARGE_HINT = ("Make it smaller and share it again: a video at 720p or a higher CRF "
                  "(ffmpeg -i in.mp4 -vf scale=-2:720 -crf 28 out.mp4), an image resized, or the "
                  "file split in parts.")
_SECRET_NAMES = ".git, .ssh, .aws, .kube, .config/gh, .git-credentials, .netrc and .npmrc"


class Refused(ValueError):
    """A file share_file will not read or propose; the message says why."""


def size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} bytes"


def read_script(path: str, roots: Sequence[str], max_bytes: int) -> str:
    """Resolve, check and print `symphony:size <n>` then the base64 of the file. Exit codes:
    3 missing or not a regular file, 4 outside the roots, 5 too large, 6 a credential path."""
    return (
        f"p=$(realpath -- {shlex.quote(path)} 2>/dev/null) || {{ echo symphony:missing; exit 3; }}\n"
        "inside=\n"
        f"for r in {' '.join(shlex.quote(r) for r in roots)}; do\n"
        '  r=$(cd "$r" 2>/dev/null && pwd -P) || continue\n'
        '  case "$p" in "$r"/*) inside=1;; esac\n'
        "done\n"
        '[ -n "$inside" ] || { echo symphony:outside; exit 4; }\n'
        f'case "$p/" in {"|".join(CREDENTIAL_GLOBS)}) echo symphony:credential; exit 6;; esac\n'
        '[ -f "$p" ] || { echo symphony:missing; exit 3; }\n'
        "size=$(wc -c < \"$p\" | tr -d ' ')\n"
        f'[ "$size" -le {max_bytes} ] || {{ echo "symphony:too-large $size"; exit 5; }}\n'
        'echo "symphony:size $size"\n'
        'base64 < "$p"\n'
    )


def read_file(sandbox, path: str, clones: Sequence[str], scratch: Sequence[str] = SCRATCH_ROOTS,
              max_bytes: int = MAX_BYTES, tool: str = TOOL) -> bytes:
    """The bytes of one file of the sandbox (`sandbox.exec_script`). A relative path is read
    in the thread's only clone. Raises Refused with what the agent can do about it; `tool`
    names the reader in those messages."""
    if not path.strip() or "\x00" in path or "\n" in path:
        raise Refused("path must be one file path")
    prefix = ""
    if not path.startswith("/"):
        if len(clones) != 1:
            raise Refused(f"give the absolute path of {path}: sandbox_run runs each command in "
                          "/workspace/<owner>/<name>")
        prefix = f"cd {shlex.quote(clones[0])} || exit 2\n"
    roots = [*clones, *scratch]
    code, out = sandbox.exec_script(prefix + read_script(path, roots, max_bytes),
                                    timeout_s=READ_TIMEOUT_S)
    lines = out.splitlines()
    if code == 3:
        raise Refused(f"no such file in the sandbox: {path}")
    if code == 4:
        raise Refused(f"refused: {path} resolves outside {', '.join(roots)}; {tool} reads "
                      "only files under the repository clones and /tmp. Copy the file there first.")
    if code == 5:
        size = next((int(x.split()[1]) for x in lines if x.startswith("symphony:too-large ")), 0)
        raise Refused(f"refused: {path} is {size_text(size)}, over {tool}'s limit of "
                      f"{max_bytes // (1024 * 1024)} MB. {TOO_LARGE_HINT}")
    if code == 6:
        raise Refused(f"refused: {path} is inside a .git directory or is a credential file; "
                      f"{_SECRET_NAMES} are never shared.")
    marker = next((i for i, line in enumerate(lines) if line.startswith("symphony:size ")), None)
    if code != 0 or marker is None:
        raise RuntimeError(f"could not read {path} from the sandbox (exit {code})")
    size = int(lines[marker].split()[1])
    data = base64.b64decode("".join(lines[marker + 1:]))
    if len(data) != size:
        raise RuntimeError(f"short read of {path}: {len(data)} of {size} bytes")
    text = data.decode("latin-1")  # one character per byte: binary files are checked too
    for name, pattern in SECRETS:
        if pattern.search(text):
            raise Refused(f"refused: {path} holds what looks like a credential ({name}). Never "
                          "share a credential; remove it from the file first.")
    return data


def evidence_kind(content_type: str) -> str:
    """The tracker's evidence kind (`hosttools.EVIDENCE_KINDS`) for a content type."""
    if content_type.startswith("image/"):
        return "screenshot"
    if content_type.startswith("video/"):
        return "video"
    if content_type == "text/plain":
        return "log"
    return "report"


def content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def parse_action(action_id: str) -> tuple[str, str] | None:
    """`evidence:<id>:attach|dismiss` → (id, choice)."""
    parts = action_id.split(":")
    if len(parts) != 3 or parts[0] != "evidence" or not parts[1] or parts[2] not in CHOICES:
        return None
    return parts[1], parts[2]


def buttons(pid: str) -> list[Button]:
    return [Button(f"evidence:{pid}:attach", "Attach", "primary"),
            Button(f"evidence:{pid}:dismiss", "Dismiss", "danger")]


def render(doc: dict) -> str:
    return (f"Attach {doc['name']} ({size_text(doc['size'])}) to {doc['ticket']} as "
            f"\"{doc['title']}\"?\n\nNothing is attached until someone mapped in notify.users "
            "clicks Attach. The file waits one hour.")


class Evidence:
    """Proposals to attach a shared file to a ticket, and their clicks."""

    def __init__(self, store: ProposalStore, *, tracker: Callable[[], object],
                 notify_users: Callable[[], dict], audit: Callable[[str, str, str], None],
                 logger=None) -> None:
        self.store = store
        self.tracker = tracker
        self.notify_users = notify_users
        self.audit = audit  # (actor, target, reason)
        self.log = logger

    def _data_key(self, pid: str) -> str:
        return self.store.prefix + pid + ":data"

    def propose(self, out: Outbound, channel: str, thread: str, *, login: str, ticket: str,
                title: str, name: str, data: bytes) -> str:
        """Store the file and post the proposal with its buttons. Raises Refused."""
        if len(data) > EVIDENCE_MAX_BYTES:
            raise Refused(f"{name} is {size_text(len(data))}: only files up to "
                          f"{EVIDENCE_MAX_BYTES // (1024 * 1024)} MB can wait for an Attach "
                          f"click, so nothing was proposed for {ticket}. The file is in the "
                          "thread; share a smaller one to attach it.")
        if self.tracker().state_of(ticket) is None:
            raise Refused(f"the tracker has no ticket {ticket}, so nothing was proposed")
        doc = {"ticket": ticket, "title": title, "name": name, "size": len(data),
               "content_type": content_type(name), "by": login, "channel": channel,
               "thread": thread}
        pid = self.store.save(doc, ttl_s=EVIDENCE_TTL_S)
        self.store.client.set(self._data_key(pid), base64.b64encode(data).decode(),
                              ex=EVIDENCE_TTL_S)
        out.post(channel, thread, render(doc), buttons(pid))
        return f"proposed: the thread sees an Attach button for {ticket}"

    def handle(self, action: Action, out: Outbound) -> str:
        """Answers one click; returns what happened (for logs and tests)."""
        parsed = parse_action(action.action_id)
        if parsed is None:
            out.ack_action(action)
            return "unknown_action"
        pid, choice = parsed
        login = mapped_login(self.notify_users(), action.transport, action.user_id)
        if login is None:
            out.ack_action(action, ONLY_MAPPED)
            return "unmapped"
        if not self.store.claim(pid):
            out.ack_action(action, "This file is being attached.")
            return "busy"
        try:
            doc = self.store.get(pid)  # read under the claim: a click that just finished counts
            if doc is None:
                out.ack_action(action, "This file expired. Ask the assistant to share it again.")
                return "expired"
            if doc["status"] in ("attached", "dismissed"):
                out.ack_action(action, f"This file is already {doc['status']}.")
                return "already_" + doc["status"]
            out.ack_action(action)  # at once: Discord allows 3 s
            ref = Ref(action.channel, action.message_id)
            if choice == "dismiss":
                self.store.update(pid, status="dismissed", by=login)
                self.store.client.delete(self._data_key(pid))
                out.update(ref, f"Dismissed by {login}. Nothing was attached to {doc['ticket']}.", ())
                return "dismissed"
            return self._attach(pid, doc, login, ref, out)
        finally:
            self.store.release(pid)

    def _attach(self, pid: str, doc: dict, login: str, ref: Ref, out: Outbound) -> str:
        raw = self.store.client.get(self._data_key(pid))
        if raw is None:
            out.update(ref, "This file expired. Ask the assistant to share it again.", ())
            return "expired"
        try:
            attached = self.tracker().attach_evidence(
                doc["ticket"], title=doc["title"], kind=evidence_kind(doc["content_type"]),
                filename=doc["name"], content_type=doc["content_type"], data=base64.b64decode(raw))
        except Exception as exc:  # noqa: BLE001 — a tracker failure: the people may retry
            if self.log is not None:
                self.log.warning("assistant_evidence_failed", proposal=pid,
                                 error=type(exc).__name__, detail=str(exc)[:200])
            out.update(ref, render(doc) + "\n\nAttaching failed: "
                       f"{str(exc)[:200] or type(exc).__name__}. Click again to retry.", buttons(pid))
            return "failed"
        url = attached.get("url") or ""
        self.store.update(pid, status="attached", by=login, url=url)
        self.store.client.delete(self._data_key(pid))
        out.update(ref, f"Attached by {login} to {doc['ticket']}: {doc['title']} {url}".rstrip(), ())
        try:
            self.audit(login, doc["ticket"][:200], f"{doc['name']} ({size_text(doc['size'])})")
        except Exception as exc:  # noqa: BLE001 — the file is attached; the log keeps the trace
            if self.log is not None:
                self.log.warning("assistant_audit_failed", proposal=pid, error=type(exc).__name__)
        return "attached"


def share_file_tool(*, files: Callable[[], tuple[object, list[str]]],
                    login: Callable[[], str | None], out: Outbound, channel: str, thread: str,
                    audit: Callable[[str, str, str], None], evidence: Evidence | None = None,
                    scratch: Sequence[str] = SCRATCH_ROOTS, logger=None) -> HostTool:
    """`share_file(path, title, comment?, ticket?)` for the current turn's mapped asker.
    `files()` gives the thread's sandbox and its clone directories (`AssistantSandbox.readable`)."""

    def check(args: dict) -> str | None:
        if login() is None:
            return NEEDS_MAPPING
        if not args["path"].strip():
            return "path is required"
        if not args["title"].strip() or len(args["title"]) > MAX_TITLE:
            return f"title must be 1-{MAX_TITLE} characters"
        if len(args.get("comment") or "") > MAX_COMMENT:
            return f"comment must be at most {MAX_COMMENT} characters"
        ticket = args.get("ticket")
        if ticket is not None:
            if evidence is None:
                return "ticket attachments are not available: share the file without ticket"
            if not TICKET.fullmatch(ticket.strip()):
                return "ticket must be a tracker identifier such as ENG-12"
        return None

    def answer(args: dict, _index: int) -> str:
        who = login()
        if who is None:  # the asker changed between the check and the call
            raise HostToolError(NEEDS_MAPPING)
        path, title = args["path"].strip(), args["title"].strip()
        try:
            sandbox, clones = files()
            data = read_file(sandbox, path, clones, scratch)
        except (Refused, SandboxError) as exc:
            raise HostToolError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — a dead pod or a cluster error
            raise HostToolError(f"could not read {path} from the sandbox: "
                                f"{str(exc)[:200] or type(exc).__name__}") from exc
        name = posixpath.basename(path)
        try:
            permalink = out.upload(channel, thread, name, data, title=title,
                                   comment=(args.get("comment") or "").strip())
        except FileTooLarge as exc:
            raise HostToolError(f"{name} is {size_text(len(data))}, over what {out.name} accepts "
                                f"here ({size_text(exc.limit)}). {TOO_LARGE_HINT}") from exc
        except Exception as exc:  # noqa: BLE001 — the vendor refused; the agent reports it
            raise HostToolError(f"{out.name} did not take {name}: "
                                f"{str(exc)[:200] or type(exc).__name__}") from exc
        try:
            audit(who, f"{name[:150]} ({size_text(len(data))})", title)
        except Exception as exc:  # noqa: BLE001 — the file is posted; the log keeps the trace
            if logger is not None:
                logger.warning("assistant_audit_failed", tool=TOOL, error=type(exc).__name__)
        result: dict = {"ok": True, "name": name, "size": len(data)}
        if permalink:
            result["permalink"] = permalink
        ticket = (args.get("ticket") or "").strip()
        if ticket and evidence is not None:
            try:
                result["ticket"] = evidence.propose(out, channel, thread, login=who, ticket=ticket,
                                                    title=title, name=name, data=data)
            except Refused as exc:
                result["ticket_error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 — the file is posted; the proposal failed
                result["ticket_error"] = f"the proposal failed: {str(exc)[:200] or type(exc).__name__}"
        return json.dumps(result, ensure_ascii=False)

    props = {
        "path": {"type": "string", "description": "the file in the sandbox: absolute, or relative "
                 "to the repository root sandbox_run runs in"},
        "title": {"type": "string", "description": f"what the file shows, up to {MAX_TITLE} characters"},
        "comment": {"type": "string", "description": "optional message posted with the file"},
    }
    if evidence is not None:
        props["ticket"] = {"type": "string", "description": "optional ticket identifier: also "
                           "propose attaching the file to it; a mapped person confirms"}
    return HostTool(
        TOOL,
        "Post one file you made in the sandbox (a video, screenshot, PDF, HTML page, spreadsheet, "
        "document) in this thread, so the person gets the file itself. The file must be under a "
        f"repository clone or /tmp, at most {MAX_BYTES // (1024 * 1024)} MB; .git, credential "
        "files and files holding a credential are refused. "
        + ("With `ticket`, the thread also gets an Attach button that puts the file on that ticket "
           f"(files up to {EVIDENCE_MAX_BYTES // (1024 * 1024)} MB). " if evidence is not None else "")
        + "Returns {ok, name, size, permalink?}.",
        _obj(["path", "title"], props),
        check=check,
        answer=answer,
    )


def proposal_attacher(*, files: Callable[[], tuple[object, list[str]]],
                      login: Callable[[], str | None], store: ProposalFiles,
                      scratch: Sequence[str] = SCRATCH_ROOTS) -> Callable[[list[dict]], int]:
    """`propose_tickets`' reader of `attachments`, for the current turn's mapped asker. It
    reads every file of the call with `read_file`, stores the bytes in `store`, and records on
    each attachment `file` (its id there), `name`, `size`, `content_type` and `kind`. Returns
    how many files it read; raises HostToolError (nothing stored) on the first refusal."""

    def attach(tickets: list[dict]) -> int:
        wanted = [a for t in tickets for a in t.get("attachments") or []]
        if not wanted:
            return 0
        if login() is None:
            raise HostToolError(NEEDS_MAPPING)
        try:
            sandbox, clones = files()
        except SandboxError as exc:
            raise HostToolError(str(exc)) from exc
        read: list[tuple[dict, bytes]] = []
        total = 0
        for a in wanted:
            path = a["path"].strip()
            try:
                data = read_file(sandbox, path, clones, scratch, tool="propose_tickets")
            except (Refused, SandboxError) as exc:
                raise HostToolError(f"{exc} Nothing was proposed: fix the attachments and call "
                                    "propose_tickets again.") from exc
            except Exception as exc:  # noqa: BLE001 — a dead pod or a cluster error
                raise HostToolError(f"could not read {path} from the sandbox: "
                                    f"{str(exc)[:200] or type(exc).__name__}") from exc
            total += len(data)
            if total > MAX_ATTACHMENT_BYTES:
                raise HostToolError(
                    f"refused: the attachments add up to more than "
                    f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB, the limit per proposal. Nothing "
                    "was proposed: attach fewer or smaller files (tar czf packs and compresses "
                    "them) and call propose_tickets again.")
            read.append((a, data))
        for a, data in read:
            name = posixpath.basename(a["path"].strip())
            kind_of = content_type(name)
            a.update(file=store.put(data), name=name, size=len(data), content_type=kind_of,
                     kind=evidence_kind(kind_of))
        return len(read)

    return attach
