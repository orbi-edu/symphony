"""GitHub adapter: issues on a Projects (v2) board, the board's single-select field as state.

A ticket is an issue (not a draft item, not a pull request) in the project. Reads and the
state move go through GraphQL (Projects v2 exists only there); comments, labels, sub-issues
and dependencies go through REST. The credential is the GitHub App installation token,
minted on the host and never in the workspace. `github_graphql` is the
agent's raw tool, executed here with that token.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Sequence

import httpx

from ..endpoints import github_api_url, github_graphql_url
from ..repos import PREFIX as REPO_LABEL_PREFIX
from ..repos import resolve_target_repos
from .base import (
    WORKPAD_HEADING,
    Issue,
    SubIssue,
    SubIssueRecords,
    ToolSpec,
    TrackerInputError,
    TrackerUnavailable,
    classify_state,
    make_dispatchable,
)

GRAPHQL_TOOL = "github_graphql"
PRIORITY_FIELD = "Priority"  # optional board field; lower value = more urgent
IDS_BATCH = 50
PAGE = 100
# GitHub has no public API to upload a file to an issue, so evidence is a comment. A comment
# body holds at most 65,536 characters.
COMMENT_MAX_CHARS = 65536
TEXT_TYPES = frozenset({
    "application/json", "application/x-ndjson", "application/xml", "application/yaml",
    "application/x-yaml", "application/toml", "application/javascript", "application/x-sh",
})
LABEL_COLOR = "ededed"
API_VERSION = "2022-11-28"

EVIDENCE_MARKER = re.compile(r"\A<!-- symphony:evidence (\{.*?\}) -->")
NODE_ID = re.compile(r"[A-Z][A-Z0-9]*_[A-Za-z0-9_-]+")
NATIVE_REF = re.compile(r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)#(\d+)")
IDENTIFIER = re.compile(r"([A-Za-z0-9._-]+)-(\d+)")
# PR → ticket: Symphony's branch `symphony/<identifier>`, a native ref, or an issue URL.
BRANCH_REF = re.compile(r"(?:^|[\s/])symphony/([a-z0-9._-]+-\d+)\b", re.IGNORECASE)
ISSUE_URL = re.compile(r"https?://[^/\s]+/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/issues/(\d+)")
# GitHub's closing keywords with a bare `#n`: the PR's own repository (`owner/repo#n` after a
# keyword is already a NATIVE_REF).
CLOSING_REF = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b", re.IGNORECASE)
# Last resort: text that STARTS with the identifier, as the prompt asks the agent to name the
# branch and title the PR ("app-5-farewell", "app-5: Add farewell()").
LEADING_REF = re.compile(r"\A([A-Za-z0-9._]+(?:-[A-Za-z0-9._]+)*?)-(\d+)(?=$|[-:/\s])")

PROJECT_QUERY = """
query SymphonyProject($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        fields(first: 50) {
          nodes {
            ... on ProjectV2Field { id name dataType }
            ... on ProjectV2SingleSelectField { id name dataType options { id name } }
          }
        }
      }
    }
  }
}
"""

# `$status` and `$priority` are the board's field names. A blocker's state is its own item on
# this board (or its open/closed state when it is not on the board).
ISSUE_FIELDS = """
      id databaseId number title body url createdAt updatedAt state
      repository { name nameWithOwner }
      labels(first: 50) { nodes { name } }
      assignees(first: 10) { nodes { id login } }
      blockedBy(first: 25) {
        nodes {
          id number state repository { name nameWithOwner }
          projectItems(first: 10) {
            nodes {
              project { id }
              status: fieldValueByName(name: $status) {
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
            }
          }
        }
      }
"""
ITEM_VALUES = """
      status: fieldValueByName(name: $status) {
        ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
      }
      priority: fieldValueByName(name: $priority) {
        ... on ProjectV2ItemFieldSingleSelectValue { optionId }
        ... on ProjectV2ItemFieldNumberValue { number }
      }
"""
ISSUE_WITH_ITEMS = f"""
      {ISSUE_FIELDS}
      projectItems(first: 20) {{ nodes {{ id project {{ id }} {ITEM_VALUES} }} }}
"""

ITEMS_QUERY = f"""
query SymphonyItems($project: ID!, $status: String!, $priority: String!, $after: String) {{
  node(id: $project) {{
    ... on ProjectV2 {{
      items(first: 50, after: $after) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id type {ITEM_VALUES}
          content {{ ... on Issue {{ {ISSUE_FIELDS} }} }}
        }}
      }}
    }}
  }}
}}
"""

ISSUES_BY_IDS_QUERY = f"""
query SymphonyIssues($ids: [ID!]!, $status: String!, $priority: String!) {{
  nodes(ids: $ids) {{ ... on Issue {{ {ISSUE_WITH_ITEMS} }} }}
}}
"""

ISSUE_BY_NUMBER_QUERY = f"""
query SymphonyIssue($owner: String!, $name: String!, $number: Int!, $status: String!,
                    $priority: String!) {{
  repository(owner: $owner, name: $name) {{ issue(number: $number) {{ {ISSUE_WITH_ITEMS} }} }}
}}
"""

SET_STATUS_MUTATION = """
mutation SymphonyMove($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field,
            value: {singleSelectOptionId: $option}}
  ) { projectV2Item { id } }
}
"""

ADD_ITEM_MUTATION = """
mutation SymphonyAddItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}
"""


class GitHubTracker:
    kind = "github"

    def __init__(
        self,
        owner: str,
        project_number: int,
        active_states: Sequence[str],
        terminal_states: Sequence[str],
        token: Callable[[], str],
        status_field: str = "Status",
        default_repo: str | None = None,
        done_state: str = "Done",
        client: httpx.Client | None = None,
    ) -> None:
        self.owner = owner
        self.project_number = project_number
        self.status_field = status_field
        self.active_states = list(active_states)
        self.terminal_states = list(terminal_states)
        self.default_repo = default_repo
        self.done_state = done_state
        self._token = token
        self._client = client or httpx.Client(timeout=15.0)
        self._board: dict | None = None

    # -- transport --------------------------------------------------------
    def _headers(self) -> dict:
        try:
            token = self._token()
        except Exception as exc:  # noqa: BLE001 — a mint failure is an outage, not a crash
            raise TrackerUnavailable(f"GitHub App credential: {exc}") from exc
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = self._client.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            raise TrackerUnavailable(f"transport error: {exc}") from exc
        if resp.status_code in (403, 429):
            # GitHub's rate-limit rules: `retry-after` wins; with no remaining quota wait for
            # `x-ratelimit-reset`; a secondary limit without either means a minute.
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                raise TrackerUnavailable("rate limited", retry_after_s=float(retry_after))
            if resp.headers.get("x-ratelimit-remaining") == "0":
                raise TrackerUnavailable("rate limited", retry_after_s=_until_reset(resp))
            if resp.status_code == 429 or "rate limit" in resp.text.lower():
                raise TrackerUnavailable("rate limited", retry_after_s=60.0)
        if resp.status_code >= 500:
            raise TrackerUnavailable(f"server error {resp.status_code}")
        return resp

    def _graphql(self, query: str, variables: dict, *, missing_ok: bool = False) -> dict:
        resp = self._send("POST", github_graphql_url(), json={"query": query, "variables": variables})
        if resp.status_code >= 400:
            raise TrackerUnavailable(f"request rejected {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        errors = payload.get("errors") or []
        if any(e.get("type") == "RATE_LIMITED" for e in errors):
            raise TrackerUnavailable("rate limited", retry_after_s=_until_reset(resp))
        # NOT_FOUND leaves `null` where the object would be; callers that look things up
        # read that as "unknown" rather than as an outage.
        if errors and not (missing_ok and all(e.get("type") == "NOT_FOUND" for e in errors)):
            raise TrackerUnavailable(f"graphql errors: {errors[:1]}")
        return payload.get("data") or {}

    def _rest(self, method: str, path: str, *, allow: tuple[int, ...] = (), **kw):
        """JSON of a REST call; None for a status in `allow` (e.g. 404 = unknown)."""
        resp = self._send(method, f"{github_api_url()}{path}", **kw)
        if resp.status_code in allow:
            return None
        if resp.status_code >= 400:
            raise TrackerUnavailable(f"request rejected {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def _rest_pages(self, path: str) -> list[dict]:
        """Every element of a paginated list, following the `Link` header."""
        out: list[dict] = []
        url: str | None = f"{github_api_url()}{path}"
        params: dict | None = {"per_page": PAGE}
        while url:
            resp = self._send("GET", url, params=params)
            if resp.status_code >= 400:
                raise TrackerUnavailable(f"request rejected {resp.status_code}: {resp.text[:200]}")
            out.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
            params = None  # the next URL carries them
        return out

    # -- the board --------------------------------------------------------
    def _project(self) -> dict:
        """The project's id, the state field and its options, and the priority field (cached:
        a board's shape changes rarely, and a stale option id fails loudly on the move)."""
        if self._board is not None:
            return self._board
        data = self._graphql(
            PROJECT_QUERY, {"owner": self.owner, "number": self.project_number}, missing_ok=True
        )
        project = (data.get("repositoryOwner") or {}).get("projectV2")
        where = f"GitHub project {self.owner}#{self.project_number}"
        if not project:
            raise TrackerUnavailable(
                f"{where} was not found or the GitHub App cannot read it "
                "(it needs organization_projects or repository_projects permission)"
            )
        fields = {f.get("name"): f for f in (project.get("fields") or {}).get("nodes") or [] if f}
        status = fields.get(self.status_field)
        if not status or status.get("dataType") != "SINGLE_SELECT":
            raise TrackerUnavailable(f"{where} has no single-select field {self.status_field!r}")
        priority = fields.get(PRIORITY_FIELD) or {}
        self._board = {
            "id": project["id"],
            "status_field": status["id"],
            "options": {o["name"].casefold(): o["id"] for o in status.get("options") or []},
            # single-select priority: the option's position (first option = most urgent)
            "priority_rank": {
                o["id"]: n for n, o in enumerate(priority.get("options") or [], start=1)
            },
        }
        return self._board

    def _variables(self) -> dict:
        return {"status": self.status_field, "priority": PRIORITY_FIELD}

    def _option(self, state_name: str) -> str:
        option = self._project()["options"].get(state_name.casefold())
        if option is None:
            raise ValueError(
                f"the {self.status_field!r} field of GitHub project "
                f"{self.owner}#{self.project_number} has no option {state_name!r}"
            )
        return option

    def _item_of(self, node: dict) -> dict | None:
        """This board's item for an issue node fetched with ISSUE_WITH_ITEMS."""
        project_id = self._project()["id"]
        return next(
            (i for i in (node.get("projectItems") or {}).get("nodes") or []
             if i and (i.get("project") or {}).get("id") == project_id),
            None,
        )

    # -- normalization ----------------------------------------------------
    def _state_type(self, status: str, issue_state: str | None) -> str:
        kind = classify_state(status, self.active_states, self.terminal_states)
        # a closed issue is finished whatever its board column says
        return "terminal" if issue_state == "CLOSED" else kind

    def _normalize(self, node: dict, item: dict) -> Issue:
        status = (item.get("status") or {}).get("name") or ""
        state_type = self._state_type(status, node.get("state"))
        blockers = self._nonterminal_blockers(node)
        repo = node["repository"]
        identifier = _identifier(repo["name"], node["number"])
        labels = tuple(dict.fromkeys(
            s for s in ((lbl.get("name") or "").strip().lower()
                        for lbl in (node.get("labels") or {}).get("nodes") or [])
            if s
        ))
        # the issue's own repository, unless `repo:` labels designate others
        target_repos, repo_reason = resolve_target_repos(labels, repo["nameWithOwner"])
        target_repo = target_repos[0] if target_repos else None
        assignees = (node.get("assignees") or {}).get("nodes") or []
        first = assignees[0] if assignees else {}
        return Issue(
            id=node["id"],
            identifier=identifier,
            title=node.get("title"),
            description=node.get("body"),
            state=status,
            state_type=state_type,
            priority=self._priority(item.get("priority")),
            created_at=node.get("createdAt"),
            updated_at=node.get("updatedAt"),
            labels=labels,
            assignee=first.get("login"),
            assignee_id=first.get("id"),
            url=node.get("url"),
            branch_name=f"symphony/{identifier}",
            native_ref=f"{repo['nameWithOwner']}#{node['number']}",
            blocked_by=tuple(blockers),
            target_repo=target_repo,
            target_repos=target_repos,
            undispatchable_reason=repo_reason,
            dispatchable=make_dispatchable(state_type, bool(blockers), target_repo),
        )

    def _priority(self, value: dict | None) -> int | None:
        if not value:
            return None
        if value.get("number") is not None:
            return int(value["number"])
        return self._project()["priority_rank"].get(value.get("optionId"))

    def _nonterminal_blockers(self, node: dict) -> list[dict]:
        project_id = self._project()["id"]
        out = []
        for blocker in (node.get("blockedBy") or {}).get("nodes") or []:
            if not blocker:
                continue
            item = next(
                (i for i in (blocker.get("projectItems") or {}).get("nodes") or []
                 if i and (i.get("project") or {}).get("id") == project_id),
                None,
            )
            status = ((item or {}).get("status") or {}).get("name") or ""
            if self._state_type(status, blocker.get("state")) == "terminal":
                continue
            out.append({
                "id": blocker["id"],
                "identifier": _identifier(blocker["repository"]["name"], blocker["number"]),
                "state": status or str(blocker.get("state") or "").title(),
            })
        return out

    # -- TrackerAdapter: reads --------------------------------------------
    def fetch_issues_by_states(self, states: Sequence[str]) -> list[Issue]:
        if not states:
            return []  # upstream §17.3: empty input returns empty WITHOUT a provider request
        wanted = {s.casefold() for s in states}
        out: list[Issue] = []
        after: str | None = None
        while True:
            data = self._graphql(
                ITEMS_QUERY, {"project": self._project()["id"], "after": after, **self._variables()}
            )
            page = (data.get("node") or {}).get("items") or {}
            for item in page.get("nodes") or []:
                content = (item or {}).get("content")
                if item.get("type") != "ISSUE" or not content:
                    continue  # draft items and pull requests are not tickets
                status = (item.get("status") or {}).get("name") or ""
                if status.casefold() in wanted:
                    out.append(self._normalize(content, item))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            after = info["endCursor"]

    def fetch_issues_by_ids(self, ids: Sequence[str]) -> list[Issue]:
        if not ids:
            return []  # upstream §17.3: empty input returns empty WITHOUT a provider request
        out: list[Issue] = []
        for start in range(0, len(ids), IDS_BATCH):
            data = self._graphql(
                ISSUES_BY_IDS_QUERY,
                {"ids": list(ids[start : start + IDS_BATCH]), **self._variables()},
                missing_ok=True,
            )
            for node in data.get("nodes") or []:
                item = self._item_of(node) if node and node.get("number") else None
                if item is not None:  # off the board = gone
                    out.append(self._normalize(node, item))
        return out

    def _locate(self, ref: str) -> dict | None:
        """The issue node (with its board items) for an id, identifier or `owner/repo#n`."""
        ref = ref.strip()
        native = NATIVE_REF.fullmatch(ref)
        ident = None if NODE_ID.fullmatch(ref) else IDENTIFIER.fullmatch(ref)
        if native or ident:
            owner, name, number = (
                native.groups() if native else (self.owner, *ident.groups())
            )
            data = self._graphql(
                ISSUE_BY_NUMBER_QUERY,
                {"owner": owner, "name": name, "number": int(number), **self._variables()},
                missing_ok=True,
            )
            node = (data.get("repository") or {}).get("issue")
            if node or native:
                return node
            # an issue of another owner's repository on this board
            return self._find_on_board(name.lower(), int(number))
        data = self._graphql(
            ISSUES_BY_IDS_QUERY, {"ids": [ref], **self._variables()}, missing_ok=True
        )
        nodes = [n for n in data.get("nodes") or [] if n and n.get("number")]
        return nodes[0] if nodes else None

    def _find_on_board(self, repo_name: str, number: int) -> dict | None:
        after: str | None = None
        while True:
            data = self._graphql(
                ITEMS_QUERY, {"project": self._project()["id"], "after": after, **self._variables()}
            )
            page = (data.get("node") or {}).get("items") or {}
            for item in page.get("nodes") or []:
                content = (item or {}).get("content") or {}
                if (content.get("number") == number
                        and content["repository"]["name"].lower() == repo_name):
                    return self._locate(content["id"])
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return None
            after = info["endCursor"]

    def _require(self, ref: str) -> dict:
        node = self._locate(ref)
        if node is None:
            raise TrackerInputError(f"no GitHub issue {ref!r}")
        return node

    def state_of(self, identifier: str) -> dict | None:
        node = self._locate(identifier)
        item = self._item_of(node) if node else None
        if item is None:
            return None
        name = (item.get("status") or {}).get("name") or ""
        return {"name": name, "type": self._state_type(name, node.get("state"))}

    def _comments(self, node: dict) -> list[dict]:
        repo = node["repository"]["nameWithOwner"]
        return self._rest_pages(f"/repos/{repo}/issues/{node['number']}/comments")

    def fetch_issue_workpad(self, identifier: str) -> str:
        """Ticket comments (the agent's Workpad) for the reflector's harvest."""
        try:
            node = self._locate(identifier)
            comments = self._comments(node) if node else []
        except TrackerUnavailable:
            return ""
        return "\n\n".join(c.get("body") or "" for c in comments)

    def attachments(self, identifier: str) -> list[dict]:
        node = self._locate(identifier)
        if node is None:
            return []
        out = []
        for c in self._comments(node):
            m = EVIDENCE_MARKER.match(c.get("body") or "")
            if m:
                meta = json.loads(m.group(1))
                out.append({"title": meta.get("title", ""), "url": c["html_url"],
                            "kind": meta.get("kind", "")})
        return out

    def identifier_in(self, text: str, repo: str | None = None) -> str | None:
        text = text or ""
        hits = [(m.start(), m.group(1).lower()) for m in BRANCH_REF.finditer(text)]
        hits += [(m.start(), _identifier(m.group(2), m.group(3)))
                 for pattern in (NATIVE_REF, ISSUE_URL) for m in pattern.finditer(text)]
        if not hits and repo:
            name = repo.split("/", 1)[-1]
            hits = [(m.start(), _identifier(name, m.group(1))) for m in CLOSING_REF.finditer(text)]
        if not hits and (leading := LEADING_REF.match(text.strip())):
            hits = [(0, _identifier(*leading.groups()))]
        return min(hits)[1] if hits else None

    def add_blocked_by(self, issue: str, blocker: str) -> bool:
        """An issue dependency (`blocked_by`), across repositories of the board too."""
        blocked, blocking = self._require(issue), self._require(blocker)
        repo = blocked["repository"]["nameWithOwner"]
        path = f"/repos/{repo}/issues/{blocked['number']}/dependencies/blocked_by"
        if any(dep.get("id") == blocking["databaseId"] for dep in self._rest_pages(path)):
            return False
        if self._rest("POST", path, allow=(403, 404, 422),
                      json={"issue_id": blocking["databaseId"]}) is None:
            raise TrackerInputError(f"GitHub refused that {blocker} blocks {issue}")
        return True

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """GitHub issue search over the board owner's repositories (open and closed)."""
        terms = " ".join(query.replace('"', " ").split())[:200]
        found = self._rest("GET", "/search/issues", params={
            "q": f"{terms} is:issue user:{self.owner}", "per_page": min(max(limit, 1), 100),
            "sort": "updated", "order": "desc",
        }) or {}
        out = []
        for item in found.get("items") or []:
            name = (item.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1]
            if not name:
                continue
            out.append({"identifier": _identifier(name, item["number"]),
                        "title": item.get("title") or "", "state": item.get("state") or "",
                        "url": item.get("html_url") or ""})
        return out[:limit]

    # -- TrackerAdapter: agent tool ---------------------------------------
    def agent_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=GRAPHQL_TOOL,
                description=(
                    "Executes a GraphQL operation against GitHub (issues, comments, labels, "
                    "Projects v2 items and fields, sub-issues). Runs on the host with the "
                    "GitHub App installation token, which never reaches the workspace — use it "
                    "to read and update the ticket."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "variables": {"type": "object"}},
                    "required": ["query"],
                },
            )
        ]

    def execute_agent_tool(self, name: str, args: dict) -> dict:
        if name != GRAPHQL_TOOL:
            raise ValueError(f"unknown tool {name!r}")
        return self._graphql(args["query"], args.get("variables") or {})

    def secret_environment_names(self) -> list[str]:
        return ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_INSTALLATION_ID"]

    # -- TrackerAdapter: writes -------------------------------------------
    def _set_status(self, item_id: str, option: str) -> None:
        board = self._project()
        self._graphql(SET_STATUS_MUTATION, {
            "project": board["id"], "item": item_id, "field": board["status_field"],
            "option": option,
        })

    def _add_to_board(self, content_id: str) -> str:
        out = self._graphql(
            ADD_ITEM_MUTATION, {"project": self._project()["id"], "content": content_id}
        )
        return out["addProjectV2ItemById"]["item"]["id"]

    def move(self, issue: str, state_name: str) -> bool:
        """Sets the board option and keeps the issue's open/closed state in step: a terminal
        state closes it (`completed` for `tracker.states.done`, `not_planned` otherwise), any
        other state reopens it."""
        option = self._option(state_name)
        node = self._locate(issue)
        item = self._item_of(node) if node else None
        if item is None:
            return False
        changed = False
        if (item.get("status") or {}).get("optionId") != option:
            self._set_status(item["id"], option)
            changed = True
        terminal = classify_state(state_name, self.active_states, self.terminal_states) == "terminal"
        closed = node.get("state") == "CLOSED"
        if terminal != closed:
            reason = "completed" if state_name.casefold() == self.done_state.casefold() else "not_planned"
            body = {"state": "closed", "state_reason": reason} if terminal else {"state": "open"}
            repo = node["repository"]["nameWithOwner"]
            self._rest("PATCH", f"/repos/{repo}/issues/{node['number']}", json=body)
            changed = True
        return changed

    def comment(self, issue: str, body: str) -> str:
        node = self._require(issue)
        repo = node["repository"]["nameWithOwner"]
        return str(self._post_comment(repo, node["number"], body)["id"])

    def _post_comment(self, repo: str, number: int, body: str) -> dict:
        return self._rest("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def upsert_workpad(self, issue: str, body: str) -> str:
        node = self._require(issue)
        repo = node["repository"]["nameWithOwner"]
        pad = next(
            (c for c in self._comments(node) if (c.get("body") or "").startswith(WORKPAD_HEADING)),
            None,
        )
        if pad is None:
            return str(self._post_comment(repo, node["number"], body)["id"])
        self._rest("PATCH", f"/repos/{repo}/issues/comments/{pad['id']}", json={"body": body})
        return str(pad["id"])

    def _labels(self, repo: str, names: Sequence[str], *, create: bool) -> dict[str, str]:
        """Label names (case-insensitive) → the repository's spelling. Missing ones are
        created when `create`, refused otherwise."""
        wanted = {n.casefold(): n for n in names if n.strip()}
        if not wanted:
            return {}
        found = {lab["name"].casefold(): lab["name"] for lab in self._rest_pages(f"/repos/{repo}/labels")}
        missing = [wanted[k] for k in sorted(wanted) if k not in found]
        if missing and not create:
            raise TrackerInputError(f"unknown labels in {repo}: {', '.join(missing)}")
        for name in missing:
            # 422: someone created it meanwhile — the label exists, which is what we need
            self._rest("POST", f"/repos/{repo}/labels", allow=(422,),
                       json={"name": name, "color": LABEL_COLOR})
            found[name.casefold()] = name
        return {k: found[k] for k in wanted}

    def ensure_labels(self, names: Sequence[str]) -> None:
        """In the repository new issues go to (`kubernetes.default_repo`); without one there is
        no single place for board labels, and they are created on first use instead."""
        if self.default_repo:
            self._labels(self.default_repo, names, create=True)

    def create_issue(
        self, title: str, body: str, *, labels: Sequence[str], state: str | None = None,
        repo: str | None = None,
    ) -> dict:
        """A new issue in `repo` (else `kubernetes.default_repo`), put on the board."""
        repo = repo or self.default_repo
        if not repo:
            raise TrackerInputError(
                "a GitHub ticket needs a repository: pass repo or set kubernetes.default_repo"
            )
        option = self._option(state) if state else None
        names = list(self._labels(repo, labels, create=True).values())
        created = self._rest("POST", f"/repos/{repo}/issues",
                             json={"title": title, "body": body, "labels": names})
        item = self._add_to_board(created["node_id"])
        if option:
            self._set_status(item, option)
        return {"identifier": _identifier(repo.split("/", 1)[1], created["number"]),
                "url": created["html_url"]}

    def create_sub_issues(
        self, parent: str, items: Sequence[SubIssue], *, labels: Sequence[str],
        records: SubIssueRecords,
    ) -> list[dict]:
        node = self._require(parent)
        repo = node["repository"]["nameWithOwner"]
        inherit = {name.casefold() for name in labels}
        inherited = [
            lab["name"] for lab in (node.get("labels") or {}).get("nodes") or []
            if lab["name"].lower().startswith(REPO_LABEL_PREFIX) or lab["name"].casefold() in inherit
        ]
        extra = self._labels(repo, [n for item in items for n in item.labels], create=False)
        made: dict[str, dict] = {}
        for item in items:
            record = records.issue(item.key)
            if record is None:
                names = list(dict.fromkeys([*inherited, *(extra[n.casefold()] for n in item.labels)]))
                record = self._ensure_child(node, item, names)
                records.save_issue(item.key, record)
            made[item.key] = record
        for item in items:
            for blocker in item.blocked_by:
                if records.relation(blocker, item.key):
                    continue
                blocker_record = made.get(blocker) or records.issue(blocker)
                if blocker_record is None:
                    raise TrackerInputError(f"unknown blocker key {blocker!r}")
                self._ensure_blocked_by(made[item.key], blocker_record)
                records.save_relation(blocker, item.key)
        return [{"key": item.key, "identifier": made[item.key]["identifier"],
                 "url": made[item.key]["url"]} for item in items]

    def _ensure_child(self, parent: dict, item: SubIssue, labels: list[str]) -> dict:
        """Find or create the child for `item`. GitHub takes no client-chosen id, so a hidden
        marker in the body finds a child that an interrupted call already created; the
        `parent_issue_id` on create makes it a sub-issue in the same request."""
        repo = parent["repository"]["nameWithOwner"]
        marker = f"<!-- symphony:subissue {parent['id']}:{item.key} -->"
        existing = next(
            (c for c in self._rest_pages(f"/repos/{repo}/issues/{parent['number']}/sub_issues")
             if marker in (c.get("body") or "")),
            None,
        )
        if existing is None:
            created = self._rest("POST", f"/repos/{repo}/issues", json={
                "title": item.title, "body": f"{item.description}\n\n{marker}",
                "labels": labels, "parent_issue_id": parent["databaseId"],
            })
            item_id = self._add_to_board(created["node_id"])
            # a child starts in the first active state, so the daemon picks it up when unblocked
            self._set_status(item_id, self._option(self.active_states[0]))
        else:
            created = existing
            located = self._locate(created["node_id"])
            board_item = self._item_of(located) if located else None
            if board_item is None or not board_item.get("status"):
                item_id = board_item["id"] if board_item else self._add_to_board(created["node_id"])
                self._set_status(item_id, self._option(self.active_states[0]))
        return {
            "id": created["node_id"],
            "identifier": _identifier(repo.split("/", 1)[1], created["number"]),
            "url": created["html_url"],
            "repo": repo,
            "number": created["number"],
            "rest_id": created["id"],
        }

    def _ensure_blocked_by(self, blocked: dict, blocker: dict) -> None:
        path = f"/repos/{blocked['repo']}/issues/{blocked['number']}/dependencies/blocked_by"
        if any(dep.get("id") == blocker["rest_id"] for dep in self._rest_pages(path)):
            return
        self._rest("POST", path, json={"issue_id": blocker["rest_id"]})

    def attach_evidence(
        self, issue: str, *, title: str, kind: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        """Text evidence as a collapsible comment. GitHub has no public API to upload a file
        to an issue, so binary evidence (screenshots, videos) cannot be attached."""
        base_type = content_type.split(";", 1)[0].strip().lower()
        is_text = (base_type.startswith("text/") or base_type in TEXT_TYPES
                   or base_type.endswith(("+json", "+xml")))
        if not is_text:
            raise TrackerInputError(
                f"cannot attach {filename} ({content_type}): GitHub has no API to upload files "
                "to an issue, so only text evidence (text/*, JSON, logs) can be attached, as a "
                "comment. Describe the result in the Workpad or commit the file to the branch."
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise TrackerInputError(f"cannot attach {filename}: it is not UTF-8 text") from None
        meta = json.dumps({"title": title, "kind": kind}).replace(">", "\\u003e")
        fence = "`" * max(3, _longest_backtick_run(text) + 1)
        body = (
            f"<!-- symphony:evidence {meta} -->\n<details>\n"
            f"<summary>Evidence ({html.escape(kind)}): {html.escape(title)} — "
            f"{html.escape(filename)}</summary>\n\n{fence}text\n{text.rstrip(chr(10))}\n{fence}\n\n"
            "</details>\n"
        )
        if len(body) > COMMENT_MAX_CHARS:
            raise TrackerInputError(
                f"cannot attach {filename}: text evidence must fit in one GitHub comment "
                f"({COMMENT_MAX_CHARS} characters); attach an excerpt"
            )
        node = self._require(issue)
        posted = self._post_comment(node["repository"]["nameWithOwner"], node["number"], body)
        return {"title": title, "url": posted["html_url"], "kind": kind}


def _identifier(repo_name: str, number) -> str:
    return f"{repo_name.lower()}-{number}"


def _until_reset(resp: httpx.Response) -> float:
    reset = resp.headers.get("x-ratelimit-reset")
    try:
        return max(1.0, float(reset) - time.time()) if reset else 60.0
    except ValueError:
        return 60.0


def _longest_backtick_run(text: str) -> int:
    return max((len(m) for m in re.findall(r"`+", text)), default=0)
