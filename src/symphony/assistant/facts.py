"""Team facts by pull request: the assistant proposes, a mapped person confirms, Symphony opens a PR.

`propose_team_fact(text, section?)` stores a proposal at `sym:<ns>:assistant:fact:<id>` (7 days)
and the engine shows it with "Open PR" and "Dismiss" (button ids `fact:<id>:pr|dismiss`). Only
a person mapped in `notify.users` may click. A confirmed click makes this trusted process open a
PR on the soul repository with the GitHub App: the fact goes under `## Team facts` of the soul
file, on the branch `symphony/assistant-fact-<id>`. Each step is idempotent (the branch, the
commit and the PR are found again on a retry), a claim key lets one click work at a time, and the
audit entry is `assistant.team_fact_pr`.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable

from . import Action, Button, Outbound, Ref
from .confirm import ProposalStore, mapped_login
from .memory import MAX_TEXT, TurnNotes, looks_like_credential
from .soul import MAX_BYTES

ONLY_MAPPED = "Only people mapped in notify.users can open team fact PRs."
AUDIT_ACTION = "assistant.team_fact_pr"
CHOICES = ("pr", "dismiss")
SECTION = "Team facts"
MAX_SECTION = 60
MAX_FACTS_PER_TURN = 3
# What the App needs on the soul repository to open the PR.
PERMISSIONS = {"contents": "write", "pull_requests": "write"}
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def branch_of(fid: str) -> str:
    return f"symphony/assistant-fact-{fid}"


def parse_action(action_id: str) -> tuple[str, str] | None:
    """`fact:<id>:<choice>` → (id, choice)."""
    parts = action_id.split(":")
    if len(parts) != 3 or parts[0] != "fact" or parts[2] not in CHOICES or not parts[1]:
        return None
    return parts[1], parts[2]


def buttons(fid: str) -> list[Button]:
    return [Button(f"fact:{fid}:pr", "Open PR", "primary"),
            Button(f"fact:{fid}:dismiss", "Dismiss", "danger")]


def render(doc: dict) -> str:
    where = f"{doc['repo']}:{doc['path']}"
    under = SECTION + (f" › {doc['section']}" if doc.get("section") else "")
    return "\n".join([f"Proposed team fact for {where} ({under}):", "", f"> {doc['text']}", "",
                      "Nothing changes until someone mapped in notify.users clicks Open PR."])


def validate(args: dict) -> str | None:
    text = " ".join(args["text"].split())
    if not text or len(text) > MAX_TEXT:
        return f"text must be 1-{MAX_TEXT} characters"
    section = args.get("section")
    if section is not None:
        section = section.strip()
        if not section or len(section) > MAX_SECTION or "\n" in section or "#" in section:
            return f"section must be one line of 1-{MAX_SECTION} characters, without #"
    return None


def fact_tool(notes: TurnNotes, source: Callable[[], tuple[str, str] | None]):
    """`propose_team_fact` for one session. The proposal waits in `notes.facts` until the
    engine posts it after the turn."""
    from ..merger.agents import HostTool, _obj

    def answer(args: dict, _index: int) -> str:
        if looks_like_credential(args["text"]):
            return "refused: this looks like a credential. Team facts never hold secrets."
        src = source()
        if src is None:
            return "refused: no soul repository is configured (assistant.soul.repo)."
        if len(notes.facts) >= MAX_FACTS_PER_TURN:
            return f"refused: at most {MAX_FACTS_PER_TURN} team facts per answer."
        section = (args.get("section") or "").strip() or None
        notes.facts.append({"text": " ".join(args["text"].split()), "section": section,
                            "repo": src[0], "path": src[1]})
        return ("Proposed. The people in the thread see it with an Open PR button; nothing "
                "changes until a mapped person clicks it.")

    _s = {"type": "string"}
    return HostTool(
        "propose_team_fact",
        "Propose a lasting fact about the team (how it works, who owns what) for the assistant's "
        "soul file in Git. People confirm it with a button, then Symphony opens a pull request. "
        f"text: one fact, at most {MAX_TEXT} characters. section: an optional sub-heading under "
        f"'{SECTION}'.",
        _obj(["text"], {"text": _s, "section": _s}),
        check=validate,
        answer=answer,
    )


def append_fact(doc: str, fact: str, section: str | None = None) -> str:
    """`doc` with `- fact` at the end of `## Team facts` (or of its `### section`); the
    headings are added when missing."""
    lines = doc.rstrip("\n").split("\n") if doc.strip() else []
    item = f"- {fact.strip()}"

    def heading(i: int) -> tuple[int, str] | None:
        m = _HEADING.match(lines[i])
        return (len(m.group(1)), m.group(2).strip()) if m else None

    def end_of(start: int, level: int) -> int:
        for i in range(start + 1, len(lines)):
            h = heading(i)
            if h and h[0] <= level:
                return i
        return len(lines)

    def insert_at(at: int, new: list[str]) -> None:
        """Puts `new` after the last text line before `at`, one blank line before what follows."""
        start = at
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        lines[start:at] = new + ([""] if at < len(lines) else [])

    top = next((i for i in range(len(lines)) if heading(i) and heading(i)[0] == 2
                and heading(i)[1].casefold() == SECTION.casefold()), None)
    if top is None:
        if lines:
            lines.append("")
        lines += [f"## {SECTION}", ""] + ([f"### {section}", ""] if section else []) + [item]
        return "\n".join(lines) + "\n"
    end = end_of(top, 2)
    if section is None:
        first_sub = next((i for i in range(top + 1, end) if heading(i)), end)
        insert_at(first_sub, [item])
        return "\n".join(lines) + "\n"
    sub = next((i for i in range(top + 1, end) if heading(i) and heading(i)[0] == 3
                and heading(i)[1].casefold() == section.casefold()), None)
    if sub is None:
        insert_at(end, ["", f"### {section}", "", item])
    else:
        insert_at(end_of(sub, 3), [item])
    return "\n".join(lines) + "\n"


class FactError(Exception):
    """The PR could not be opened; the message is for the people in the thread."""


def no_write_access(repo: str) -> FactError:
    return FactError(f"The GitHub App cannot open this PR: it needs contents: write and "
                     f"pull requests: write on {repo}.")


class FactPRs:
    """Opens the team fact PR over the GitHub REST API with an App installation token."""

    def __init__(self, token_for: Callable[[str], str], *, api_url: str, transport=None) -> None:
        self.token_for = token_for  # repo → a token with PERMISSIONS on it
        self.api_url = api_url.rstrip("/")
        self.transport = transport  # httpx transport (tests)

    def open(self, fid: str, doc: dict, login: str) -> dict:
        """`{url, number, branch}` of the PR (an existing one on a retry)."""
        import httpx

        from ..ghapp import CredentialError

        repo, path = doc["repo"], doc["path"]
        try:
            token = self.token_for(repo)
        except CredentialError as exc:
            raise no_write_access(repo) from exc
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        with httpx.Client(base_url=self.api_url, headers=headers, timeout=20.0,
                          transport=self.transport) as gh:
            def call(method: str, url: str, ok=(200, 201), **kw):
                resp = gh.request(method, url, **kw)
                if resp.status_code in (401, 403):
                    raise no_write_access(repo)
                if resp.status_code not in ok:
                    raise FactError(f"GitHub answered {resp.status_code} to {method} {url.split('?')[0]}")
                return resp

            meta = call("GET", f"/repos/{repo}").json()
            base = meta.get("default_branch") or "main"
            branch = branch_of(fid)
            owner = repo.split("/", 1)[0]
            pulls = call("GET", f"/repos/{repo}/pulls",
                         params={"head": f"{owner}:{branch}", "state": "all"}).json()
            if pulls:
                return {"url": pulls[0]["html_url"], "number": pulls[0]["number"], "branch": branch}
            head = call("GET", f"/repos/{repo}/git/ref/heads/{base}").json()["object"]["sha"]
            made = call("POST", f"/repos/{repo}/git/refs", ok=(201, 422),
                        json={"ref": f"refs/heads/{branch}", "sha": head})
            if made.status_code == 422 and "already exists" not in made.text.lower():
                raise FactError(f"GitHub refused the branch {branch}: {made.text[:200]}")
            current = call("GET", f"/repos/{repo}/contents/{path}", ok=(200, 404), params={"ref": branch})
            old, old_sha = "", None
            if current.status_code == 200:
                body = current.json()
                old_sha = body.get("sha")
                old = base64.b64decode(body.get("content") or "").decode("utf-8", errors="replace")
            if f"- {doc['text']}" not in old:
                new = append_fact(old, doc["text"], doc.get("section"))
                if len(new.encode()) > MAX_BYTES:
                    raise FactError(f"{path} would pass {MAX_BYTES // 1024} KiB, the most the "
                                    "assistant reads. Shorten the file first.")
                body = {"message": "docs(assistant): add a team fact", "branch": branch,
                        "content": base64.b64encode(new.encode()).decode()}
                if old_sha:
                    body["sha"] = old_sha
                call("PUT", f"/repos/{repo}/contents/{path}", json=body)
            pr = call("POST", f"/repos/{repo}/pulls", json={
                "title": "docs(assistant): add a team fact", "head": branch, "base": base,
                "body": (f"A team fact for the assistant's soul file `{path}`, under "
                         f"`## {SECTION}`:\n\n> {doc['text']}\n\nProposed by {doc.get('asker') or 'the assistant'} "
                         f"in {doc.get('transport') or 'chat'}; confirmed by {login}."),
            }).json()
            return {"url": pr["html_url"], "number": pr["number"], "branch": branch}


class FactConfirmer:
    def __init__(self, store: ProposalStore, *, opener: Callable[[str, dict, str], dict],
                 notify_users: Callable[[], dict], audit: Callable[[str, str, str], None],
                 logger=None) -> None:
        self.store = store
        self.opener = opener  # (fact id, proposal, login) → {url, number, branch}
        self.notify_users = notify_users
        self.audit = audit  # (actor, target, reason)
        self.log = logger

    def save(self, doc: dict) -> str:
        return self.store.save(doc)

    def handle(self, action: Action, out: Outbound) -> str:
        parsed = parse_action(action.action_id)
        if parsed is None:
            out.ack_action(action)
            return "unknown_action"
        fid, choice = parsed
        login = mapped_login(self.notify_users(), action.transport, action.user_id)
        if login is None:
            out.ack_action(action, ONLY_MAPPED)
            return "unmapped"
        doc = self.store.get(fid)
        if doc is None:
            out.ack_action(action, "This proposal expired. Ask the assistant again.")
            return "expired"
        if doc["status"] in ("opened", "dismissed"):
            out.ack_action(action, f"This team fact is already {doc['status']}.")
            return "already_" + doc["status"]
        if not self.store.claim(fid):
            out.ack_action(action, "The PR is being opened.")
            return "busy"
        out.ack_action(action)
        ref = Ref(action.channel, action.message_id)
        try:
            if choice == "dismiss":
                self.store.update(fid, status="dismissed", by=login)
                out.update(ref, f"Dismissed by {login}. No PR was opened.", ())
                return "dismissed"
            out.update(ref, "Opening the PR…", ())
            try:
                pr = self.opener(fid, doc, login)
            except Exception as exc:  # noqa: BLE001 — the people may retry
                if self.log is not None:
                    self.log.warning("assistant_fact_pr_failed", fact=fid, repo=doc["repo"],
                                     error=type(exc).__name__, detail=str(exc)[:200])
                why = str(exc) if isinstance(exc, FactError) else f"{type(exc).__name__}: {str(exc)[:200]}"
                out.update(ref, render(doc) + f"\n\nOpening the PR failed. {why} Click again to retry.",
                           buttons(fid))
                return "failed"
            self.store.update(fid, status="opened", by=login, pr=pr)
            out.update(ref, f"PR opened by {login}: {pr['url']}\n\n> {doc['text']}", ())
            try:
                self.audit(login, f"fact:{fid}", f"{doc['repo']}#{pr['number']} {pr['url']}")
            except Exception as exc:  # noqa: BLE001 — the PR exists; the log keeps the trace
                if self.log is not None:
                    self.log.warning("assistant_audit_failed", fact=fid, error=type(exc).__name__)
            return "opened"
        finally:
            self.store.release(fid)
