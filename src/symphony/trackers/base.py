"""Tracker adapter contract.

The orchestrator only knows this Protocol — never provider semantics. The tracker
credential stays on the host: `execute_agent_tool` ALWAYS runs outside the agent's sandbox.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Issue:
    """Normalized model (data-model.md §1). Never persisted in the Dict."""

    id: str
    identifier: str
    title: str | None
    description: str | None
    state: str
    state_type: str  # "active" | "terminal" | "unknown"
    priority: int | None = None
    created_at: str | None = None  # ISO-8601 UTC — sorts lexicographically
    updated_at: str | None = None
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    dispatchable: bool = False
    # fields required by the upstream spec's normalization (§4.1.1/§11.3) — exposed to
    # the Liquid prompt so WORKFLOW.md stays interchangeable with the reference
    url: str | None = None
    branch_name: str | None = None
    assignee_id: str | None = None
    assignee_email: str | None = None  # trackers that know it (Linear); GitHub gives a login
    native_ref: str | None = None
    blocked_by: tuple[dict, ...] = ()  # blocker refs {id, identifier, state} (upstream §4.1.1)
    # The repositories this issue's work belongs in, resolved by the ADAPTER from the
    # ticket (the orchestrator interprets no provider conventions), primary first;
    # `target_repo` is the primary. `undispatchable_reason` says why the issue is held back
    # when they cannot be resolved.
    target_repo: str | None = None
    target_repos: tuple[str, ...] = ()
    undispatchable_reason: str | None = None

    @property
    def assignee_keys(self) -> tuple[str, ...]:
        """What names the assignee in `notify.users`: the e-mail, then the name or login."""
        return tuple(k for k in (self.assignee_email, self.assignee) if k)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict = field(default_factory=dict)


class TrackerUnavailable(Exception):
    """Transport/5xx/429 — NEVER interpreted as 'issue vanished' (FR-010)."""

    def __init__(self, message: str = "tracker unavailable", retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TrackerInputError(ValueError):
    """The request cannot be served as asked (unknown label, unsupported evidence); the
    message tells the caller what to fix."""


WORKPAD_HEADING = "## Workpad"


@dataclass(frozen=True)
class SubIssue:
    """One child for `create_sub_issues`: validated, `blocked_by` naming other keys."""

    key: str
    title: str
    description: str
    labels: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()


class SubIssueRecords(Protocol):
    """What earlier `create_sub_issues` calls made for one parent, kept by the caller so
    retries and later attempts never duplicate a child or an edge."""

    def issue(self, key: str) -> dict | None:
        """`{id, identifier, url}` of the child made for `key`, if any."""
        ...

    def save_issue(self, key: str, record: dict) -> None: ...

    def relation(self, blocker: str, blocked: str) -> bool: ...

    def save_relation(self, blocker: str, blocked: str) -> None: ...


def classify_state(state: str, active_states: Sequence[str], terminal_states: Sequence[str]) -> str:
    """Case-insensitive comparison, as required by the upstream spec (§8.2)."""
    folded = state.casefold()
    if any(folded == s.casefold() for s in active_states):
        return "active"
    if any(folded == s.casefold() for s in terminal_states):
        return "terminal"
    return "unknown"


class TrackerAdapter(Protocol):
    """Every `issue`/`identifier` argument accepts the adapter's `Issue.id` or
    `Issue.identifier`. State names are WORKFLOW.md names (`tracker.states`, active and
    terminal states)."""

    kind: str  # "linear" | "github" | "memory"

    def fetch_issues_by_states(self, states: Sequence[str]) -> list[Issue]: ...

    def fetch_issues_by_ids(self, ids: Sequence[str]) -> list[Issue]: ...

    def agent_tool_specs(self) -> list[ToolSpec]: ...

    def execute_agent_tool(self, name: str, args: dict) -> dict: ...

    def secret_environment_names(self) -> list[str]: ...

    def move(self, issue: str, state_name: str) -> bool:
        """Set the ticket's state. False when the ticket is unknown or already there; an
        unknown state name raises."""
        ...

    def comment(self, issue: str, body: str, *, parent: str | None = None) -> str:
        """Add a comment, as a reply to comment `parent` when given and the tracker threads
        comments (else on the ticket); returns its id."""
        ...

    def person(self, issue: str, hint: str | None = None) -> dict | None:
        """Someone on the ticket: the author, assignee or a commenter whose name or e-mail
        matches `hint` (case-insensitive), or the ticket's author without one. `{name,
        email, mention}` (`mention`: text that notifies them in a comment, "" when the
        tracker has none); None when nobody matches."""
        ...

    def assignee(self, issue: str) -> dict | None:
        """The ticket's assignee, shaped as `person` returns it (`{name, email, mention}`);
        None when nobody is assigned."""
        ...

    def upsert_workpad(self, issue: str, body: str) -> str:
        """Create, or edit in place, the one comment starting with `## Workpad`; returns its id."""
        ...

    def fetch_issue_workpad(self, identifier: str) -> str:
        """The ticket's comments, joined, for the reflector's harvest ("" when unavailable)."""
        ...

    def create_sub_issues(
        self, parent: str, items: Sequence[SubIssue], *, labels: Sequence[str],
        records: SubIssueRecords,
    ) -> list[dict]:
        """Children of `parent`, blockers first (`items` arrives dependency-ordered), each
        `blocked_by` edge a blocks relation. Children carry the parent's `repo:` labels, those
        of `labels` (case-insensitive) the parent carries, and their own labels (unknown ones
        raise TrackerInputError). Idempotent through `records`. Returns
        `[{key, identifier, url}]` in `items` order."""
        ...

    def attach_evidence(
        self, issue: str, *, title: str, kind: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        """Attach a file as proof of work; returns `{title, url, kind}`."""
        ...

    def create_issue(
        self, title: str, body: str, *, labels: Sequence[str], state: str | None = None,
        repo: str | None = None,
    ) -> dict:
        """A new ticket on the board (`repo`: the `owner/name` it is about); labels missing in
        the tracker are created. Returns `{identifier, url}`."""
        ...

    def ensure_labels(self, names: Sequence[str]) -> None:
        """Create the labels among `names` that the board does not have yet (case-insensitive).
        Existing ones are left alone."""
        ...

    def state_of(self, identifier: str) -> dict | None:
        """`{name, type}`, `type` being "active" | "terminal" | "unknown"; None when unknown."""
        ...

    def attachments(self, identifier: str) -> list[dict]:
        """The ticket's evidence: `[{title, url, kind}]` (`kind` may be "")."""
        ...

    def identifier_in(self, text: str, repo: str | None = None) -> str | None:
        """The first identifier of this tracker's tickets in `text` (a PR branch, title or
        body), canonical. `repo` (`owner/name`) is the PR's repository, for trackers whose
        tickets a bare `#n` can name."""
        ...

    def add_blocked_by(self, issue: str, blocker: str) -> bool:
        """Record that `blocker` blocks `issue`, any two tickets of the board. True when the
        relation is new, False when it was already there. A relation the tracker refuses
        raises TrackerInputError."""
        ...

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Tickets whose title or body holds `query`, newest first, at most `limit`:
        `[{identifier, title, state, url}]`. Read-only; for the assistant's `tracker_search`."""
        ...


def make_dispatchable(
    issue_state_type: str, has_nonterminal_blocker: bool, target_repo: str | None = "-"
) -> bool:
    """Any ACTIVE state with no non-terminal blocker is dispatchable — this applies
    equally to Todo, In Progress and Rework (without it, issues sent back by the merger
    would never get an agent).

    The target repository is folded in here rather than added as a gate to `eligible()`:
    the orchestrator stays free of provider conventions, and there is no separate code
    path a later change could forget. `target_repo` defaults to a non-None sentinel for
    callers that route no repository.
    """
    return issue_state_type == "active" and not has_nonterminal_blocker and target_repo is not None


def pick_person(people: Sequence[dict], hint: str | None) -> dict | None:
    """`TrackerAdapter.person` over `people` (author first, each `{name, email, mention}`,
    plus an optional `display`): without a hint the first one; with one, an exact name,
    display name, e-mail or e-mail user, else the first whose name starts with the hint."""
    people = [p for p in people if p and (p.get("name") or p.get("email"))]
    if not hint or not hint.strip():
        return _person(people[0]) if people else None
    wanted = hint.strip().lstrip("@").casefold()

    def names(p: dict) -> set[str]:
        email = (p.get("email") or "").casefold()
        return {(p.get("name") or "").casefold(), (p.get("display") or "").casefold(), email,
                email.split("@", 1)[0]} - {""}

    found = (next((p for p in people if wanted in names(p)), None)
             or next((p for p in people if (p.get("name") or "").casefold().startswith(wanted)), None))
    return _person(found) if found else None


def _person(p: dict) -> dict:
    return {"name": p.get("name") or p.get("email") or "", "email": p.get("email") or "",
            "mention": p.get("mention") or ""}


class GitHubLike(Protocol):
    """Minimal read boundary over the code hosting used by the reflector."""

    def get_file(self, path: str) -> str | None:
        """File content on the default branch (the REAL curation baseline) or None."""
        ...

    def get_pr_for_issue(self, identifier: str) -> dict | None: ...

    def get_pr_diff(self, pr_number: int) -> str: ...

    def get_review_threads(self, pr_number: int) -> list[dict]: ...

    def get_ci_results(self, pr_number: int) -> list[dict]: ...

    def open_pr(self, branch: str, title: str, body: str, files: dict[str, str], labels: list[str]) -> str: ...
