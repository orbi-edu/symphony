"""Linear adapter: GraphQL reads (the reference's 2 operations), the ticket writes of the
host tools and the merger, and the agent's raw `linear_graphql` tool.

The credential (`LINEAR_API_KEY`) lives ONLY on the host (daemon/runner/merger) — never in
the sandbox (constitution III). Agent mutations run in the runner, inside the idempotency
cache.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence

import httpx

from ..endpoints import linear_api_url
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
    pick_person,
)

TERMINAL_STATE_TYPES = ("completed", "canceled", "duplicate")  # WorkflowState.type closed values
IDS_BATCH = 50  # keep nested-connection query complexity well under Linear's limits
IDENTIFIER = re.compile(r"\b([A-Za-z][A-Za-z0-9]{0,9}-\d{1,7})\b")
UPLOAD_TIMEOUT_S = 300.0

ISSUE_FIELDS = """
      id identifier title description priority createdAt updatedAt url branchName
      state { name }
      labels(first: 25) { nodes { name } }
      assignee { id name }
      inverseRelations(first: 20) { nodes { type issue { id identifier state { name type } } } }
"""
# blockers: in IssueRelation, `issue` is the SOURCE and `relatedIssue` the target — whatever
# blocks THIS issue shows up in inverseRelations with type=blocks (Linear's schema)

# Two scopes, never none. A board is scoped either to ONE project or to a whole team;
# an unscoped query would sweep every issue the API key can see, which grows silently as
# the workspace does. `$scope` is named the same in both so the call site stays uniform.
ISSUES_BY_PROJECT_QUERY = f"""
query IssuesByStates($scope: String!, $states: [String!]!, $after: String) {{
  issues(
    filter: {{project: {{slugId: {{eq: $scope}}}}, state: {{name: {{in: $states}}}}}}
    first: 50, after: $after
  ) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{{ISSUE_FIELDS}}}
  }}
}}
"""

# Team scope: one board driving several projects at once. The `symphony` label remains the
# actual dispatch gate (`orchestrator._has_required_labels`), so widening the query widens
# what is CONSIDERED, never what is worked.
ISSUES_BY_TEAM_QUERY = f"""
query IssuesByStates($scope: String!, $states: [String!]!, $after: String) {{
  issues(
    filter: {{team: {{key: {{eq: $scope}}}}, state: {{name: {{in: $states}}}}}}
    first: 50, after: $after
  ) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{{ISSUE_FIELDS}}}
  }}
}}
"""

ISSUES_BY_IDS_QUERY = f"""
query IssuesByIds($ids: [ID!]!) {{
  issues(filter: {{id: {{in: $ids}}}}, first: 50) {{
    nodes {{{ISSUE_FIELDS}}}
  }}
}}
"""

# The assistant's `tracker_search`: title or description holds the term, on this board only.
_SEARCH_SCOPES = {
    "project": "project: {slugId: {eq: $scope}}",
    "team": "team: {key: {eq: $scope}}",
}
SEARCH_QUERIES = {
    name: f"""
query SymphonySearch($scope: String!, $term: String!, $first: Int!) {{
  issues(
    filter: {{{scope}, or: [{{title: {{containsIgnoreCase: $term}}}},
                            {{description: {{containsIgnoreCase: $term}}}}]}}
    first: $first, orderBy: updatedAt
  ) {{
    nodes {{ identifier title url state {{ name }} }}
  }}
}}
"""
    for name, scope in _SEARCH_SCOPES.items()
}

# -- writes -------------------------------------------------------------------------------
# `issue(id:)` takes the UUID or the human identifier (ENG-123).
TICKET_QUERY = """
query SymphonyTicket($id: String!) {
  issue(id: $id) {
    id identifier url state { name type }
    team { id key states { nodes { id name } } }
    labels { nodes { id name } }
  }
}
"""
TEAM_QUERY = """
query SymphonyTeam($key: String!) {
  teams(filter: {key: {eq: $key}}) { nodes { id key states { nodes { id name } } } }
}
"""
PROJECT_QUERY = """
query SymphonyProject($id: String!) {
  project(id: $id) { id teams { nodes { id key states { nodes { id name } } } } }
}
"""
UPDATE_STATE_MUTATION = """
mutation SymphonyMove($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) { success }
}
"""
COMMENTS_QUERY = """
query SymphonyComments($id: String!) {
  issue(id: $id) { comments(first: 50) { nodes { id body } } }
}
"""
COMMENT_MUTATION = """
mutation SymphonyComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id } }
}
"""
COMMENT_UPDATE_MUTATION = """
mutation SymphonyCommentUpdate($id: String!, $input: CommentUpdateInput!) {
  commentUpdate(id: $id, input: $input) { success comment { id } }
}
"""
PEOPLE_QUERY = """
query SymphonyPeople($id: String!) {
  issue(id: $id) {
    creator { name displayName email url }
    assignee { name displayName email url }
    comments(first: 100) { nodes { user { name displayName email url } } }
  }
}
"""
LABELS_QUERY = """
query SymphonyLabels($names: [String!]!) {
  issueLabels(filter: {name: {in: $names}}, first: 100) { nodes { id name isGroup team { id } } }
}
"""
LABEL_CREATE_MUTATION = """
mutation SymphonyLabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id name } }
}
"""
FIND_ISSUE_QUERY = """
query SymphonyFindIssue($id: ID!) {
  issues(filter: {id: {eq: $id}}, first: 1) { nodes { id identifier url } }
}
"""
CREATE_ISSUE_MUTATION = """
mutation SymphonyCreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url } }
}
"""
CREATE_RELATION_MUTATION = """
mutation SymphonyBlocks($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) { success issueRelation { id } }
}
"""
FILE_UPLOAD_MUTATION = """
mutation SymphonyUpload($contentType: String!, $filename: String!, $size: Int!) {
  fileUpload(contentType: $contentType, filename: $filename, size: $size) {
    success uploadFile { uploadUrl assetUrl headers { key value } }
  }
}
"""
ATTACHMENT_MUTATION = """
mutation SymphonyAttach($input: AttachmentCreateInput!) {
  attachmentCreate(input: $input) { success attachment { id url } }
}
"""
ATTACHMENTS_QUERY = """
query SymphonyAttachments($id: String!) {
  issue(id: $id) { attachments(first: 50) { nodes { title url subtitle } } }
}
"""


def _stable_id(*parts: str) -> str:
    """A deterministic UUID for a Linear entity: a create replayed after a crash between
    Linear's commit and our record finds the entity instead of making a second one. Linear
    accepts only version 4 UUIDs, so the hash goes into the version 4 layout."""
    digest = hashlib.sha256(("symphony:" + ":".join(parts)).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _already_exists(exc: Exception) -> bool:
    """A replayed create of our deterministic id, which Linear already committed."""
    text = str(exc).lower()
    return "already exists" in text or "duplicate" in text


class LinearTracker:
    kind = "linear"

    def __init__(
        self,
        api_key: str,
        project_slug: str,
        active_states: Sequence[str],
        terminal_states: Sequence[str],
        assignee: str | None = None,
        client: httpx.Client | None = None,
        default_repo: str | None = None,
        team_key: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.project_slug = project_slug
        # A project designation wins: it is the narrower scope, and an operator who set
        # both almost certainly meant the specific one. Neither is refused rather than
        # defaulted — an unscoped board silently grows with the workspace.
        self.team_key = team_key
        if not (project_slug or team_key):
            raise ValueError(
                "tracker.provider needs either project_slug (one project) or team_key "
                "(every project on a team) — an unscoped board would sweep the workspace"
            )
        self.active_states = list(active_states)
        self.terminal_states = list(terminal_states)
        self.assignee = assignee
        self.default_repo = default_repo
        self._client = client or httpx.Client(timeout=15.0)

    # -- transport --------------------------------------------------------
    def _post(self, query: str, variables: dict) -> dict:
        try:
            resp = self._client.post(
                linear_api_url(),
                json={"query": query, "variables": variables},
                headers={"Authorization": self.api_key},
            )
        except httpx.HTTPError as exc:
            raise TrackerUnavailable(f"transport error: {exc}") from exc
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "10"))
            raise TrackerUnavailable("rate limited", retry_after_s=retry_after)
        if resp.status_code >= 500:
            raise TrackerUnavailable(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            # Linear signals rate limiting with HTTP 400 + errors[].extensions.code
            # == RATELIMITED (linear.app/developers/rate-limiting), not with 429
            retry_after = self._ratelimited_retry_after(resp)
            if retry_after is not None:
                raise TrackerUnavailable("rate limited", retry_after_s=retry_after)
            raise TrackerUnavailable(f"request rejected {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("errors"):
            raise TrackerUnavailable(f"graphql errors: {data['errors'][:1]}")
        return data["data"]

    @staticmethod
    def _ratelimited_retry_after(resp: httpx.Response) -> float | None:
        try:
            errors = resp.json().get("errors") or []
        except ValueError:
            return None
        if not any((e.get("extensions") or {}).get("code") == "RATELIMITED" for e in errors):
            return None
        reset = resp.headers.get("X-RateLimit-Requests-Reset")
        if reset:
            try:
                import time as _time

                return max(1.0, float(reset) / 1000 - _time.time())
            except ValueError:
                pass
        return 10.0

    # -- normalization ----------------------------------------------------
    def _normalize(self, node: dict) -> Issue:
        state_name = (node.get("state") or {}).get("name", "")
        state_type = classify_state(state_name, self.active_states, self.terminal_states)
        blockers = self._nonterminal_blockers(node)
        assignee = (node.get("assignee") or {}).get("name")
        routed = self.assignee is None or assignee == self.assignee
        labels = tuple(
            dict.fromkeys(
                s
                for s in (
                    (lbl.get("name") or "").strip().lower()
                    for lbl in (node.get("labels") or {}).get("nodes", [])
                )
                if s
            )
        )
        # The target repositories are a property of the ticket, resolved HERE so the
        # orchestrator never interprets provider conventions — the same boundary that
        # already decides `dispatchable`.
        target_repos, repo_reason = resolve_target_repos(labels, self.default_repo)
        target_repo = target_repos[0] if target_repos else None
        return Issue(
            id=node["id"],
            identifier=node.get("identifier", node["id"]),
            title=node.get("title"),
            description=node.get("description"),
            state=state_name,
            state_type=state_type,
            priority=node.get("priority"),
            created_at=node.get("createdAt"),
            updated_at=node.get("updatedAt"),
            # upstream normalization MUST: trim, lowercase, drop blanks, dedupe (order kept)
            labels=labels,
            assignee=assignee,
            assignee_id=(node.get("assignee") or {}).get("id"),
            url=node.get("url"),
            branch_name=node.get("branchName"),
            native_ref=node["id"],
            blocked_by=tuple(blockers),
            target_repo=target_repo,
            target_repos=target_repos,
            undispatchable_reason=repo_reason,
            dispatchable=make_dispatchable(state_type, bool(blockers), target_repo) and routed,
        )

    @staticmethod
    def _nonterminal_blockers(node: dict) -> list[dict]:
        """Issues blocking THIS one: inverseRelations with type=blocks — Linear's
        issue/relatedIssue pair is source→target, so whatever blocks us is the
        SOURCE (the `issue` field) of the inverse relation."""
        out = []
        for rel in (node.get("inverseRelations") or {}).get("nodes", []):
            if rel.get("type") != "blocks":
                continue
            blocking = rel.get("issue") or {}
            if ((blocking.get("state") or {}).get("type")) not in TERMINAL_STATE_TYPES:
                if blocking.get("id"):
                    # blocker REFS (upstream §4.1.1): templates may use b.identifier/b.state
                    out.append(
                        {
                            "id": blocking["id"],
                            "identifier": blocking.get("identifier") or blocking["id"],
                            "state": (blocking.get("state") or {}).get("name", ""),
                        }
                    )
        return out

    # -- TrackerAdapter ---------------------------------------------------
    def fetch_issues_by_states(self, states: Sequence[str]) -> list[Issue]:
        if not states:
            return []  # upstream §17.3: empty input returns empty WITHOUT a provider request
        nodes: list[dict] = []
        after: str | None = None
        while True:
            query, scope = (
                (ISSUES_BY_PROJECT_QUERY, self.project_slug)
                if self.project_slug
                else (ISSUES_BY_TEAM_QUERY, self.team_key)
            )
            data = self._post(
                query, {"scope": scope, "states": list(states), "after": after}
            )
            page = data["issues"]
            nodes.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return [self._normalize(n) for n in nodes]

    def fetch_issues_by_ids(self, ids: Sequence[str]) -> list[Issue]:
        if not ids:
            return []  # upstream §17.3: empty input returns empty WITHOUT a provider request
        nodes: list[dict] = []
        for start in range(0, len(ids), IDS_BATCH):
            batch = list(ids[start : start + IDS_BATCH])
            data = self._post(ISSUES_BY_IDS_QUERY, {"ids": batch})
            nodes.extend(data["issues"]["nodes"])
        return [self._normalize(n) for n in nodes]

    def agent_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="linear_graphql",
                description=(
                    "Executes a GraphQL operation against Linear (comments, state "
                    "transitions, issues, sub-issues and relations). Runs on the host with "
                    "the adapter's credential — use it to move the ticket, keep the Workpad, "
                    "and create follow-up or sub-issues."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "variables": {"type": "object"}},
                    "required": ["query"],
                },
            )
        ]

    def execute_agent_tool(self, name: str, args: dict) -> dict:
        if name != "linear_graphql":
            raise ValueError(f"unknown tool {name!r}")
        return self._post(args["query"], args.get("variables") or {})

    def secret_environment_names(self) -> list[str]:
        return ["LINEAR_API_KEY"]

    def fetch_issue_workpad(self, identifier: str) -> str:
        """Ticket comments (the agent's Workpad) for the reflector's harvest.
        The singular `issue(id:)` query officially accepts the human identifier
        (e.g. ENG-123) — IssueFilter has NO `identifier` field."""
        query = """
        query Workpad($id: String!) {
          issue(id: $id) {
            comments(first: 50) { nodes { body createdAt user { name } } }
          }
        }
        """
        try:
            data = self._post(query, {"id": identifier})
        except TrackerUnavailable:
            return ""
        issue = data.get("issue") or {}
        comments = (issue.get("comments") or {}).get("nodes") or []
        return "\n\n".join(c.get("body", "") for c in comments)

    def _ticket(self, issue: str) -> dict | None:
        return self._post(TICKET_QUERY, {"id": issue}).get("issue")

    def move(self, issue: str, state_name: str) -> bool:
        ticket = self._ticket(issue)
        if not ticket or ticket["state"]["name"] == state_name:
            return False
        self._post(
            UPDATE_STATE_MUTATION,
            {"id": ticket["id"], "stateId": self._state_id(ticket["team"], state_name)},
        )
        return True

    @staticmethod
    def _state_id(team: dict, state_name: str) -> str:
        found = next((s for s in team["states"]["nodes"] if s["name"] == state_name), None)
        if found is None:
            raise TrackerInputError(f"team {team['key']} has no state named {state_name!r}")
        return found["id"]

    def comment(self, issue: str, body: str, *, parent: str | None = None) -> str:
        data = {"issueId": issue, "body": body, **({"parentId": parent} if parent else {})}
        out = self._post(COMMENT_MUTATION, {"input": data})
        payload = out.get("commentCreate") or {}
        if not payload.get("success"):
            raise RuntimeError("Linear did not create the comment")
        return payload["comment"]["id"]

    def person(self, issue: str, hint: str | None = None) -> dict | None:
        # Linear turns a profile URL in a comment into a mention of that person.
        ticket = self._post(PEOPLE_QUERY, {"id": issue}).get("issue") or {}
        users = [ticket.get("creator"), ticket.get("assignee"),
                 *(c.get("user") for c in (ticket.get("comments") or {}).get("nodes") or [])]
        return pick_person([{"name": u.get("name"), "display": u.get("displayName"),
                             "email": u.get("email"), "mention": u.get("url") or ""}
                            for u in users if u], hint)

    def upsert_workpad(self, issue: str, body: str) -> str:
        nodes = ((self._post(COMMENTS_QUERY, {"id": issue}).get("issue") or {}).get("comments")
                 or {}).get("nodes") or []
        pad = next((c for c in nodes if (c.get("body") or "").startswith(WORKPAD_HEADING)), None)
        if pad is None:
            return self.comment(issue, body)
        out = self._post(COMMENT_UPDATE_MUTATION, {"id": pad["id"], "input": {"body": body}})
        if not (out.get("commentUpdate") or {}).get("success"):
            raise RuntimeError("Linear did not update the Workpad")
        return pad["id"]

    def state_of(self, identifier: str) -> dict | None:
        ticket = self._ticket(identifier)
        if not ticket:
            return None
        name = ticket["state"]["name"]
        kind = classify_state(name, self.active_states, self.terminal_states)
        if kind == "unknown" and ticket["state"].get("type") in TERMINAL_STATE_TYPES:
            kind = "terminal"
        return {"name": name, "type": kind}

    def attachments(self, identifier: str) -> list[dict]:
        issue = self._post(ATTACHMENTS_QUERY, {"id": identifier}).get("issue")
        if not issue:
            return []
        return [
            {"title": a.get("title") or a["url"], "url": a["url"], "kind": a.get("subtitle") or ""}
            for a in issue["attachments"]["nodes"]
        ]

    def identifier_in(self, text: str, repo: str | None = None) -> str | None:
        m = IDENTIFIER.search(text or "")
        return m.group(1).upper() if m else None

    def add_blocked_by(self, issue: str, blocker: str) -> bool:
        blocked, blocking = self._ticket(issue), self._ticket(blocker)
        if not blocked or not blocking:
            raise TrackerInputError(f"no Linear ticket {issue if not blocked else blocker!r}")
        try:
            payload = self._post(CREATE_RELATION_MUTATION, {"input": {
                "id": _stable_id(blocking["id"], "blocks", blocked["id"]),
                "issueId": blocking["id"],
                "relatedIssueId": blocked["id"],
                "type": "blocks",
            }}).get("issueRelationCreate") or {}
        except TrackerUnavailable as exc:
            if _already_exists(exc):
                return False
            raise
        if not payload.get("success") or not payload.get("issueRelation"):
            raise TrackerInputError(f"Linear did not record that {blocker} blocks {issue}")
        return True

    def search(self, query: str, limit: int = 10) -> list[dict]:
        name, scope = ("project", self.project_slug) if self.project_slug else ("team", self.team_key)
        data = self._post(SEARCH_QUERIES[name], {"scope": scope, "term": query, "first": limit})
        return [
            {"identifier": n["identifier"], "title": n.get("title") or "",
             "state": (n.get("state") or {}).get("name", ""), "url": n.get("url") or ""}
            for n in (data.get("issues") or {}).get("nodes") or []
        ][:limit]

    def _label_ids(self, team_id: str, names: Sequence[str], *, create: bool) -> dict[str, str]:
        """Label names (case-insensitive) → ids usable on the team, a team label winning over
        a workspace one. A missing label is created when `create`, else refused: inside the
        group its prefix names (`repo:x` → group `repo`) when one exists, else on the team."""
        if not names:
            return {}
        prefixes = {n.split(":", 1)[0] for n in names if ":" in n}
        nodes = (self._post(LABELS_QUERY, {"names": sorted({*names, *prefixes})})
                 .get("issueLabels") or {}).get("nodes") or []
        found: dict[str, str] = {}
        groups: dict[str, dict] = {}
        for node in nodes:
            owner = (node.get("team") or {}).get("id")
            folded = node["name"].casefold()
            if node.get("isGroup"):
                # Linear keeps a group's children on the group's team, or on the workspace.
                if owner == team_id or (owner is None and folded not in groups):
                    groups[folded] = {"parentId": node["id"], **({"teamId": owner} if owner else {})}
            elif owner == team_id or (owner is None and folded not in found):
                found[folded] = node["id"]
        missing = sorted({n for n in names if n.casefold() not in found})
        if missing and not create:
            raise TrackerInputError(f"unknown labels: {', '.join(missing)}")
        for name in missing:
            group = groups.get(name.split(":", 1)[0].casefold()) if ":" in name else None
            data = {"name": name, **(group if group is not None else {"teamId": team_id})}
            out = self._post(LABEL_CREATE_MUTATION, {"input": data})
            payload = out.get("issueLabelCreate") or {}
            if not payload.get("success"):
                raise RuntimeError(f"Linear did not create the label {name!r}")
            found[name.casefold()] = payload["issueLabel"]["id"]
        return found

    def _board_team(self) -> tuple[dict, str | None]:
        """Where a new ticket goes so this board sees it: the project (and its team, the
        configured one when it is among them) or the team."""
        if self.project_slug:
            project = self._post(PROJECT_QUERY, {"id": self.project_slug}).get("project")
            teams = ((project or {}).get("teams") or {}).get("nodes") or []
            if not teams:
                raise TrackerInputError(f"Linear project {self.project_slug!r} has no team")
            team = next((t for t in teams if t["key"] == self.team_key), teams[0])
            return team, project["id"]
        teams = self._post(TEAM_QUERY, {"key": self.team_key})["teams"]["nodes"]
        if not teams:
            raise TrackerInputError(f"no Linear team with key {self.team_key!r}")
        return teams[0], None

    def ensure_labels(self, names: Sequence[str]) -> None:
        """Missing labels are created (see `_label_ids` for where), in batches the label query
        allows."""
        team, _ = self._board_team()
        wanted = sorted({n for n in names if n.strip()})
        for start in range(0, len(wanted), 50):
            self._label_ids(team["id"], wanted[start:start + 50], create=True)

    def create_issue(
        self, title: str, body: str, *, labels: Sequence[str], state: str | None = None,
        repo: str | None = None,
    ) -> dict:
        """The ticket goes on the board's project or team; `repo` travels in its labels."""
        team, project_id = self._board_team()
        data: dict = {"teamId": team["id"], "title": title, "description": body}
        if project_id:
            data["projectId"] = project_id
        if state:
            data["stateId"] = self._state_id(team, state)
        names = list(dict.fromkeys(labels))
        if names:
            ids = self._label_ids(team["id"], names, create=True)
            data["labelIds"] = list(dict.fromkeys(ids[n.casefold()] for n in names))
        out = self._post(CREATE_ISSUE_MUTATION, {"input": data}).get("issueCreate") or {}
        if not out.get("success") or not out.get("issue"):
            raise RuntimeError("Linear did not create the issue")
        return {"identifier": out["issue"]["identifier"], "url": out["issue"]["url"]}

    def create_sub_issues(
        self, parent: str, items: Sequence[SubIssue], *, labels: Sequence[str],
        records: SubIssueRecords,
    ) -> list[dict]:
        ticket = self._ticket(parent)
        if not ticket:
            raise TrackerInputError("the current ticket was not found on Linear")
        inherit = {name.casefold() for name in labels}
        inherited = [
            node["id"]
            for node in (ticket.get("labels") or {}).get("nodes") or []
            if node["name"].lower().startswith(REPO_LABEL_PREFIX) or node["name"].casefold() in inherit
        ]
        extra = self._label_ids(
            ticket["team"]["id"], [n for item in items for n in item.labels], create=False
        )
        made: dict[str, dict] = {}
        for item in items:
            label_ids = [*inherited, *(extra[n.casefold()] for n in item.labels)]
            made[item.key] = self._ensure_subissue(
                parent, ticket, item, list(dict.fromkeys(label_ids)), records
            )
        for item in items:
            for blocker in item.blocked_by:
                blocker_record = made.get(blocker) or records.issue(blocker)
                self._ensure_blocks(parent, blocker, blocker_record, item.key, made[item.key], records)
        return [
            {"key": item.key, "identifier": made[item.key]["identifier"], "url": made[item.key]["url"]}
            for item in items
        ]

    def _ensure_subissue(
        self, parent: str, ticket: dict, item: SubIssue, label_ids: list[str],
        records: SubIssueRecords,
    ) -> dict:
        record = records.issue(item.key)
        if record is not None:
            return record
        stable = _stable_id(parent, item.key)
        found = (self._post(FIND_ISSUE_QUERY, {"id": stable}).get("issues") or {}).get("nodes") or []
        if found:
            created = found[0]
        else:
            payload = self._post(
                CREATE_ISSUE_MUTATION,
                {
                    "input": {
                        "id": stable,
                        "teamId": ticket["team"]["id"],
                        "parentId": ticket["id"],
                        "title": item.title,
                        "description": item.description,
                        "labelIds": label_ids,
                    }
                },
            ).get("issueCreate") or {}
            if not payload.get("success") or not payload.get("issue"):
                raise RuntimeError(f"Linear did not create sub-issue {item.key!r}")
            created = payload["issue"]
        record = {"id": created["id"], "identifier": created["identifier"], "url": created["url"]}
        records.save_issue(item.key, record)
        return record

    def _ensure_blocks(
        self, parent: str, blocker: str, blocker_record: dict, blocked: str, blocked_record: dict,
        records: SubIssueRecords,
    ) -> None:
        if records.relation(blocker, blocked):
            return
        try:
            payload = self._post(
                CREATE_RELATION_MUTATION,
                {
                    "input": {
                        "id": _stable_id(parent, blocker, "blocks", blocked),
                        "issueId": blocker_record["id"],
                        "relatedIssueId": blocked_record["id"],
                        "type": "blocks",
                    }
                },
            ).get("issueRelationCreate") or {}
        except TrackerUnavailable as exc:
            if not _already_exists(exc):  # a replay of a committed relation is done, not failed
                raise
        else:
            # Linear reports a refused mutation as HTTP 200 + success:false. Recording it
            # would leave the edge missing forever; unrecorded, the next call retries it.
            if not payload.get("success") or not payload.get("issueRelation"):
                raise RuntimeError(
                    f"Linear did not record that {blocker!r} blocks {blocked!r}; call again to retry"
                )
        records.save_relation(blocker, blocked)

    def attach_evidence(
        self, issue: str, *, title: str, kind: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        """Linear file upload (signed PUT) → ticket attachment, `subtitle` holding the kind."""
        upload = self._post(
            FILE_UPLOAD_MUTATION,
            {"contentType": content_type, "filename": filename, "size": len(data)},
        ).get("fileUpload") or {}
        target = upload.get("uploadFile")
        if not upload.get("success") or not target:
            raise RuntimeError("Linear did not grant an upload URL")
        headers = {"Content-Type": content_type, "Cache-Control": "public, max-age=31536000"}
        headers.update({h["key"]: h["value"] for h in target.get("headers") or []})
        response = self._client.put(
            target["uploadUrl"], content=data, headers=headers, timeout=UPLOAD_TIMEOUT_S
        )
        if response.status_code >= 300:
            raise RuntimeError(f"evidence upload failed with HTTP {response.status_code}")
        attached = self._post(
            ATTACHMENT_MUTATION,
            {"input": {"issueId": issue, "title": title, "subtitle": kind,
                       "url": target["assetUrl"]}},
        ).get("attachmentCreate") or {}
        if not attached.get("success"):
            raise RuntimeError("Linear did not attach the evidence to the ticket")
        return {"title": title, "url": target["assetUrl"], "kind": kind}
