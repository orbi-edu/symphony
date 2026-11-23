"""State and Linear semantics of the mock: entities, the mutations both the GraphQL API and the
`/_mock/*` control API go through, and the webhook events they record.

Rows are plain dicts keyed by their GraphQL field names (plus foreign keys such as `teamId`),
with `__typename` and an insertion sequence `_seq`. A field a row does not store and no resolver
computes is not modelled: reading it is an error, never a made-up value.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_API_KEY = "lin_api_" + "mocksymphony".ljust(40, "0")  # Linear keys: lin_api_ + 40 chars
DEFAULT_TEAMS = (("ENG", "Engineering"),)
# name, type, color, position. Types are WorkflowState.type's documented values.
STATES = (
    ("Backlog", "backlog", "#bec2c8", 0.0),
    ("Todo", "unstarted", "#e2e2e2", 1.0),
    ("In Progress", "started", "#f2c94c", 2.0),
    ("In Review", "started", "#5e6ad2", 3.0),
    ("Rework", "started", "#eb5757", 4.0),
    ("Merging", "started", "#26b5ce", 5.0),
    ("Done", "completed", "#5e6ad2", 6.0),
    ("Canceled", "canceled", "#95a2b3", 7.0),
    ("Duplicate", "duplicate", "#95a2b3", 8.0),
)
SEED_LABELS = (("symphony", "#5e6ad2"), ("investigation", "#f2994a"), ("bug", "#eb5757"),
               ("feature", "#bb87fc"))
PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
TABLES = ("Organization", "User", "Team", "WorkflowState", "IssueLabel", "Project", "Issue",
          "Comment", "IssueRelation", "Attachment", "Document")
WEBHOOK_TYPES = ("Issue", "Comment", "Attachment", "Document")
IDENTIFIER = re.compile(r"([A-Z][A-Z0-9]{0,9})-(\d+)")
# Linear's create inputs take "The identifier in UUID v4 format": version nibble 4, variant 8-b.
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")


class LinearError(Exception):
    """An error in Linear's envelope: `extensions.{type, code, statusCode, userError,
    userPresentableMessage}` (the `type` strings are the SDK's `LinearErrorType` map)."""

    def __init__(self, message: str, *, type: str = "invalid input", code: str = "INVALID_INPUT",
                 status: int = 400, presentable: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.type = type
        self.code = code
        self.status = status
        self.presentable = presentable or message

    def extensions(self) -> dict:
        return {"type": self.type, "code": self.code, "statusCode": self.status,
                "userError": self.status < 500, "userPresentableMessage": self.presentable,
                "http": {"status": self.status}}


class Unmodelled(LinearError):
    """The mock does not model this part of the schema: fail loudly."""

    def __init__(self, what: str) -> None:
        super().__init__(f"Linear mock: {what} is not modelled", type="internal error",
                         code="MOCK_NOT_IMPLEMENTED", status=501)


def not_found(typename: str) -> LinearError:
    return LinearError(f"Entity not found: {typename}",
                       presentable=f"Could not find referenced {typename}.")


def invalid(presentable: str) -> LinearError:
    return LinearError("Argument Validation Error", presentable=presentable)


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def slugify(text: str, limit: int = 60) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:limit].strip("-")


def _only(inp: dict, type_name: str, allowed: set[str]) -> None:
    extra = sorted(k for k, v in inp.items() if k not in allowed and v is not None)
    if extra:
        raise Unmodelled(f"{type_name}.{extra[0]}")


class Store:
    def __init__(self, state_dir: Path | None, *, public_url: str,
                 teams: tuple[tuple[str, str], ...] = DEFAULT_TEAMS,
                 api_keys: tuple[str, ...] = (DEFAULT_API_KEY,), seed: str = "symphony") -> None:
        self.lock = threading.RLock()
        self.state_dir = Path(state_dir) if state_dir else None
        self.public_url = public_url.rstrip("/")
        self.seed = seed
        self._teams_cfg = tuple(teams)
        self._keys_cfg = tuple(api_keys)
        self.events: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.data: dict[str, Any] = {}
        path = self._path()
        if path and path.exists():
            self.data = json.loads(path.read_text())
            # a restart with more keys or teams on the command line adds them to the state
            for key in self._keys_cfg:
                self.data["apiKeys"].setdefault(key, self.data["botId"])
            for key, name in self._teams_cfg:
                if self.find_team(key) is None:
                    self.add_team(key, name)
            self.save()
        else:
            self.reset()

    # -- persistence ---------------------------------------------------------------
    def _path(self) -> Path | None:
        return self.state_dir / "linear.json" if self.state_dir else None

    def uploads_dir(self) -> Path | None:
        return self.state_dir / "linear-uploads" if self.state_dir else None

    def save(self) -> None:
        path = self._path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, path)

    def put_blob(self, file_id: str, body: bytes) -> None:
        directory = self.uploads_dir()
        if directory is None:
            self.blobs[file_id] = body
            return
        directory.mkdir(parents=True, exist_ok=True)
        (directory / file_id).write_bytes(body)

    def get_blob(self, file_id: str) -> bytes | None:
        directory = self.uploads_dir()
        if directory is None:
            return self.blobs.get(file_id)
        path = directory / file_id
        return path.read_bytes() if path.exists() else None

    # -- seed ----------------------------------------------------------------------
    def reset(self) -> None:
        with self.lock:
            self.data = {"seq": 0, "syncId": 0, "clockMs": 0, "apiKeys": {}, "teamCounters": {},
                         "uploads": {}, "tables": {t: {} for t in TABLES}}
            self.events = []
            self.blobs = {}
            directory = self.uploads_dir()
            if directory and directory.exists():
                for child in directory.iterdir():
                    child.unlink()
            org = self.insert("Organization", {"name": "Symphony Mock", "urlKey": "symphony-mock",
                                               "logoUrl": None})
            self.data["orgId"] = org["id"]
            bot = self.add_user("Symphony", "symphony@example.com", "symphony", admin=True)
            human = self.add_user("Ada Lovelace", "ada@example.com", "ada", admin=True)
            self.data["botId"], self.data["humanId"] = bot["id"], human["id"]
            for key in self._keys_cfg:
                self.data["apiKeys"][key] = bot["id"]
            for name, color in SEED_LABELS:
                self.add_label(None, name, color=color, actor=bot)
            for key, name in self._teams_cfg:
                team = self.add_team(key, name)
                if "projectId" not in self.data:
                    project = self.insert("Project", {
                        "name": "Symphony", "slugId": self._hexid(team["id"]),
                        "description": "", "icon": None, "color": "#5e6ad2",
                        "teamIds": [team["id"]], "creatorId": bot["id"]})
                    self.data["projectId"] = project["id"]
            self.events = []
            self.save()

    def add_user(self, name: str, email: str, display_name: str, *, admin: bool = False) -> dict:
        return self.insert("User", {
            "name": name, "displayName": display_name, "email": email, "active": True,
            "admin": admin, "guest": False, "app": False, "owner": admin, "avatarUrl": None,
            "avatarBackgroundColor": "#5e6ad2", "description": None, "timezone": "Etc/UTC",
            "isAssignable": True, "isMentionable": True, "statusEmoji": None,
            "statusLabel": None, "lastSeen": None})

    def add_team(self, key: str, name: str) -> dict:
        key = key.upper()
        if not IDENTIFIER.fullmatch(f"{key}-1"):
            raise invalid(f"team key {key!r} must be 1-10 uppercase letters or digits")
        if any(t["key"] == key for t in self.rows("Team", archived=True)):
            raise invalid(f"a team with key {key} already exists")
        team = self.insert("Team", {
            "key": key, "name": name, "displayName": name, "description": None, "icon": None,
            "color": "#5e6ad2", "private": False, "timezone": "Etc/UTC", "triageEnabled": False,
            "cyclesEnabled": False, "retiredAt": None,
            "memberIds": [self.data["botId"], self.data["humanId"]]})
        self.data["teamCounters"][team["id"]] = 0
        for state_name, state_type, color, position in STATES:
            self.insert("WorkflowState", {"name": state_name, "type": state_type, "color": color,
                                          "position": position, "description": None,
                                          "teamId": team["id"]})
        return team

    # -- primitives ----------------------------------------------------------------
    def now(self) -> str:
        ms = max(int(time.time() * 1000), self.data.get("clockMs", 0) + 1)
        self.data["clockMs"] = ms
        return iso_ms(ms)

    def new_id(self) -> str:
        self.data["seq"] += 1
        digest = hashlib.sha256(f"{self.seed}:{self.data['seq']}".encode()).digest()
        return str(uuid.UUID(bytes=digest[:16], version=4))

    @staticmethod
    def _hexid(seed: str) -> str:
        return hashlib.sha256(seed.encode()).hexdigest()[:12]

    def sync_id(self) -> float:
        self.data["syncId"] += 1
        return float(self.data["syncId"])

    def table(self, typename: str) -> dict[str, dict]:
        return self.data["tables"][typename]

    def rows(self, typename: str, *, archived: bool = False) -> list[dict]:
        return [r for r in self.table(typename).values() if archived or r.get("archivedAt") is None]

    def get(self, typename: str, entity_id: str | None) -> dict | None:
        return self.table(typename).get(entity_id) if entity_id else None

    def insert(self, typename: str, fields: dict, *, entity_id: str | None = None) -> dict:
        if entity_id is not None:
            if not UUID.fullmatch(entity_id):
                raise invalid("id must be a UUID")
            if any(entity_id in self.table(t) for t in TABLES):
                raise LinearError(f"Entity with id {entity_id} already exists",
                                  presentable="An entity with this id already exists.")
        stamp = self.now()
        row = {"__typename": typename, "id": entity_id or self.new_id(), "createdAt": stamp,
               "updatedAt": stamp, "archivedAt": None, **fields}
        self.data["rowSeq"] = row["_seq"] = self.data.get("rowSeq", 0) + 1
        self.table(typename)[row["id"]] = row
        return row

    @property
    def org(self) -> dict:
        return self.table("Organization")[self.data["orgId"]]

    def user_for_key(self, key: str) -> dict | None:
        return self.get("User", self.data["apiKeys"].get(key))

    # -- lookups -------------------------------------------------------------------
    def find_issue(self, ref: str | None, *, archived: bool = True) -> dict | None:
        if not ref:
            return None
        row = self.get("Issue", ref)
        if row is None:
            match = IDENTIFIER.fullmatch(ref.upper())
            if match:
                ident = match.group(0)
                row = next((i for i in self.rows("Issue", archived=True)
                            if self.identifier(i) == ident or ident in i["previousIdentifiers"]),
                           None)
        if row is not None and not archived and row.get("archivedAt"):
            return None
        return row

    def require_issue(self, ref: str | None) -> dict:
        row = self.find_issue(ref)
        if row is None:
            raise not_found("Issue")
        return row

    def require(self, typename: str, ref: str | None) -> dict:
        row = self.get(typename, ref)
        if row is None and ref and typename in ("Project", "Document"):
            row = next((r for r in self.rows(typename, archived=True) if r["slugId"] == ref), None)
        if row is None:
            raise not_found(typename)
        return row

    def find_user(self, ref: str | None) -> dict | None:
        if not ref:
            return None
        return self.get("User", ref) or next(
            (u for u in self.rows("User", archived=True)
             if ref.lower() in (u["email"].lower(), u["displayName"].lower(), u["name"].lower())),
            None)

    def find_team(self, ref: str | None) -> dict | None:
        if not ref:
            return None
        return self.get("Team", ref) or next(
            (t for t in self.rows("Team") if t["key"] == ref.upper()), None)

    def state_named(self, team: dict, name: str) -> dict | None:
        return next((s for s in self.rows("WorkflowState")
                     if s["teamId"] == team["id"] and s["name"].lower() == name.lower()), None)

    def find_label(self, name: str, team_id: str | None) -> dict | None:
        """The team's label of that name, else the workspace one (Linear's precedence)."""
        candidates = [lb for lb in self.rows("IssueLabel") if lb["name"].lower() == name.lower()]
        return next((lb for lb in candidates if lb["teamId"] == team_id), None) or next(
            (lb for lb in candidates if lb["teamId"] is None), None)

    # -- computed fields -----------------------------------------------------------
    def identifier(self, issue: dict) -> str:
        return f"{self.table('Team')[issue['teamId']]['key']}-{issue['number']}"

    def issue_url(self, issue: dict) -> str:
        return (f"{self.public_url}/{self.org['urlKey']}/issue/{self.identifier(issue)}/"
                f"{slugify(issue['title'])}")

    def branch_name(self, issue: dict, user: dict) -> str:
        return f"{user['displayName']}/{self.identifier(issue).lower()}-{slugify(issue['title'], 40)}"

    def comment_url(self, comment: dict) -> str:
        issue = self.get("Issue", comment["issueId"])
        return f"{self.issue_url(issue)}#comment-{comment['id'][:8]}"

    def document_url(self, doc: dict) -> str:
        return f"{self.public_url}/{self.org['urlKey']}/document/{slugify(doc['title'])}-{doc['slugId']}"

    def project_url(self, project: dict) -> str:
        return (f"{self.public_url}/{self.org['urlKey']}/project/{slugify(project['name'])}-"
                f"{project['slugId']}")

    def user_url(self, user: dict) -> str:
        return f"{self.public_url}/{self.org['urlKey']}/profiles/{user['displayName']}"

    # -- webhook events --------------------------------------------------------------
    def webhook_data(self, typename: str, row: dict) -> dict:
        """The entity as a data-change webhook serializes it (Linear-Webhooks schema)."""
        base = {k: v for k, v in row.items() if not k.startswith("_")}
        if typename == "Issue":
            team = self.table("Team")[row["teamId"]]
            state = self.table("WorkflowState")[row["stateId"]]
            assignee = self.get("User", row["assigneeId"])
            base.update(
                identifier=self.identifier(row), url=self.issue_url(row),
                priorityLabel=PRIORITY_LABELS[int(row["priority"])],
                state={k: state[k] for k in ("id", "name", "color", "type")},
                team={k: team[k] for k in ("id", "key", "name")},
                labels=[{"id": lb["id"], "name": lb["name"], "color": lb["color"],
                         "parentId": lb["parentId"]}
                        for lb in (self.get("IssueLabel", i) for i in row["labelIds"]) if lb],
                assignee=({"id": assignee["id"], "name": assignee["name"],
                           "email": assignee["email"], "url": self.user_url(assignee)}
                          if assignee else None))
        elif typename == "Comment":
            issue = self.get("Issue", row["issueId"])
            user = self.get("User", row["userId"])
            base["issue"] = {"id": issue["id"], "title": issue["title"],
                             "identifier": self.identifier(issue), "teamId": issue["teamId"],
                             "url": self.issue_url(issue)}
            base["user"] = ({"id": user["id"], "name": user["name"], "email": user["email"],
                             "url": self.user_url(user)} if user else None)
        elif typename == "Document":
            base["url"] = self.document_url(row)
        return base

    def entity_url(self, typename: str, row: dict) -> str:
        if typename == "Issue":
            return self.issue_url(row)
        if typename == "Comment":
            return self.comment_url(row)
        if typename == "Attachment":
            return self.issue_url(self.get("Issue", row["issueId"]))
        return self.document_url(row)

    def _snapshot(self, typename: str, row: dict) -> dict:
        return copy.deepcopy(self.webhook_data(typename, row))

    def _emit(self, action: str, typename: str, row: dict, actor: dict,
              before: dict | None = None) -> None:
        if typename not in WEBHOOK_TYPES:
            return
        data = self._snapshot(typename, row)
        event = {"action": action, "type": typename, "data": data,
                 "url": self.entity_url(typename, row),
                 "createdAt": row["updatedAt"] if action != "create" else row["createdAt"],
                 "actor": {"id": actor["id"], "type": "user", "name": actor["name"],
                           "email": actor["email"], "url": self.user_url(actor)}}
        if action == "update" and before is not None:
            changed = {k: before.get(k) for k, v in data.items()
                       if not isinstance(v, dict) and before.get(k) != v}
            if not changed:
                return
            event["updatedFrom"] = changed
        self.events.append(event)

    def drain_events(self) -> list[dict]:
        with self.lock:
            events, self.events = self.events, []
            return events

    # -- labels --------------------------------------------------------------------
    def add_label(self, team_id: str | None, name: str, *, actor: dict, color: str | None = None,
                  description: str | None = None, parent_id: str | None = None,
                  is_group: bool = False, entity_id: str | None = None) -> dict:
        name = name.strip()
        if not name:
            raise invalid("name should not be empty")
        if team_id is not None and self.get("Team", team_id) is None:
            raise not_found("Team")
        if parent_id is not None:
            parent = self.require("IssueLabel", parent_id)
            if not parent["isGroup"]:
                raise invalid("the parent label must be a label group")
            if parent["teamId"] != team_id:
                raise invalid("Cannot add a label to a group from a different team.")
        clash = [lb for lb in self.rows("IssueLabel")
                 if lb["name"].lower() == name.lower() and lb["teamId"] in (team_id, None)]
        if clash:
            raise LinearError("Duplicate label name", presentable=f"A label named {name} already exists.")
        return self.insert("IssueLabel", {
            "name": name, "color": color or "#bec2c8", "description": description,
            "isGroup": is_group, "teamId": team_id, "parentId": parent_id,
            "creatorId": actor["id"], "lastAppliedAt": None}, entity_id=entity_id)

    def _label_ids(self, team: dict, ids: list[str]) -> list[str]:
        out = []
        for label_id in ids:
            label = self.get("IssueLabel", label_id)
            if label is None or label.get("archivedAt"):
                raise not_found("IssueLabel")
            if label["teamId"] not in (None, team["id"]):
                raise invalid(f"label {label['name']} belongs to another team")
            if label["isGroup"]:
                raise invalid(f"label {label['name']} is a group and cannot be applied")
            out.append(label["id"])
        return list(dict.fromkeys(out))

    # -- issues ----------------------------------------------------------------------
    CREATE_FIELDS = {"id", "teamId", "title", "description", "stateId", "parentId", "labelIds",
                     "assigneeId", "priority", "projectId", "estimate", "dueDate", "sortOrder",
                     "subscriberIds"}
    UPDATE_FIELDS = {"title", "description", "stateId", "assigneeId", "priority", "labelIds",
                     "addedLabelIds", "removedLabelIds", "parentId", "projectId", "estimate",
                     "dueDate", "sortOrder", "subscriberIds"}

    @staticmethod
    def _apply_state(issue: dict, state: dict, stamp: str) -> None:
        kind = state["type"]
        issue["stateId"] = state["id"]
        if kind in ("backlog", "unstarted", "triage"):
            issue.update(startedAt=None, completedAt=None, canceledAt=None)
        elif kind == "started":
            issue.update(startedAt=issue["startedAt"] or stamp, completedAt=None, canceledAt=None)
        elif kind == "completed":
            issue.update(completedAt=stamp, canceledAt=None)
        else:  # canceled, duplicate
            issue.update(canceledAt=stamp, completedAt=None)

    def _priority(self, value: Any) -> int:
        if value is None:
            return 0
        if value not in PRIORITY_LABELS:
            raise invalid("priority must be between 0 and 4")
        return int(value)

    def _check_parent(self, issue_id: str | None, parent_ref: str) -> str:
        parent = self.require_issue(parent_ref)
        cursor: dict | None = parent
        while cursor is not None:
            if cursor["id"] == issue_id:
                raise invalid("an issue cannot be its own parent or the parent of an ancestor")
            cursor = self.get("Issue", cursor["parentId"])
        return parent["id"]

    def create_issue(self, actor: dict, inp: dict) -> dict:
        _only(inp, "IssueCreateInput", self.CREATE_FIELDS)
        team = self.get("Team", inp.get("teamId"))
        if team is None:
            raise not_found("Team")
        title = (inp.get("title") or "").strip()
        if not title:
            raise invalid("title should not be empty")
        if inp.get("stateId"):
            state = self.get("WorkflowState", inp["stateId"])
            if state is None:
                raise not_found("WorkflowState")
            if state["teamId"] != team["id"]:
                raise invalid("the workflow state belongs to another team")
        else:  # Linear: the team's first Backlog state (no triage in the mock)
            states = sorted((s for s in self.rows("WorkflowState") if s["teamId"] == team["id"]),
                            key=lambda s: s["position"])
            state = next((s for s in states if s["type"] == "backlog"), None) or next(
                s for s in states if s["type"] == "unstarted")
        assignee = None
        if inp.get("assigneeId"):
            assignee = self.get("User", inp["assigneeId"]) or _raise(not_found("User"))
        project = self.require("Project", inp["projectId"]) if inp.get("projectId") else None
        parent_id = self._check_parent(None, inp["parentId"]) if inp.get("parentId") else None
        label_ids = self._label_ids(team, inp.get("labelIds") or [])
        subscribers = [actor["id"], *([assignee["id"]] if assignee else [])]
        for user_id in inp.get("subscriberIds") or []:
            subscribers.append((self.get("User", user_id) or _raise(not_found("User")))["id"])
        number = self.data["teamCounters"][team["id"]] + 1
        stamp_issue = {
            "teamId": team["id"], "number": number, "title": title,
            "description": inp.get("description"), "priority": self._priority(inp.get("priority")),
            "estimate": inp.get("estimate"), "dueDate": inp.get("dueDate"),
            "stateId": state["id"], "parentId": parent_id,
            "assigneeId": assignee["id"] if assignee else None, "creatorId": actor["id"],
            "projectId": project["id"] if project else None, "labelIds": label_ids,
            "subscriberIds": list(dict.fromkeys(subscribers)),
            "sortOrder": float(inp["sortOrder"]) if inp.get("sortOrder") is not None
            else -float(number), "prioritySortOrder": 0.0, "subIssueSortOrder": None,
            "startedAt": None, "completedAt": None, "canceledAt": None, "autoArchivedAt": None,
            "autoClosedAt": None, "snoozedUntilAt": None, "triagedAt": None,
            "startedTriageAt": None, "trashed": None, "previousIdentifiers": [],
            "customerTicketCount": 0, "reactionData": {}, "reactions": [],
            "addedToTeamAt": None, "addedToProjectAt": None, "addedToCycleAt": None}
        issue = self.insert("Issue", stamp_issue, entity_id=inp.get("id"))
        self.data["teamCounters"][team["id"]] = number
        issue["addedToTeamAt"] = issue["createdAt"]
        if project:
            issue["addedToProjectAt"] = issue["createdAt"]
        self._apply_state(issue, state, issue["createdAt"])
        self._emit("create", "Issue", issue, actor)
        return issue

    def update_issue(self, actor: dict, ref: str, inp: dict) -> dict:
        _only(inp, "IssueUpdateInput", self.UPDATE_FIELDS)
        issue = self.require_issue(ref)
        team = self.table("Team")[issue["teamId"]]
        before = self._snapshot("Issue", issue)
        stamp = self.now()
        changes: dict[str, Any] = {}
        if "title" in inp:
            title = (inp["title"] or "").strip()
            if not title:
                raise invalid("title should not be empty")
            changes["title"] = title
        for key in ("description", "estimate", "dueDate"):
            if key in inp:
                changes[key] = inp[key]
        if "priority" in inp:
            changes["priority"] = self._priority(inp["priority"])
        if "sortOrder" in inp and inp["sortOrder"] is not None:
            changes["sortOrder"] = float(inp["sortOrder"])
        if "assigneeId" in inp:
            assignee = self.get("User", inp["assigneeId"]) if inp["assigneeId"] else None
            if inp["assigneeId"] and assignee is None:
                raise not_found("User")
            changes["assigneeId"] = assignee["id"] if assignee else None
        if "projectId" in inp:
            project = self.require("Project", inp["projectId"]) if inp["projectId"] else None
            changes["projectId"] = project["id"] if project else None
        if "parentId" in inp:
            changes["parentId"] = self._check_parent(issue["id"], inp["parentId"]) \
                if inp["parentId"] else None
        labels = list(issue["labelIds"])
        if inp.get("labelIds") is not None:
            labels = self._label_ids(team, inp["labelIds"])
        if inp.get("addedLabelIds"):
            labels = list(dict.fromkeys([*labels, *self._label_ids(team, inp["addedLabelIds"])]))
        if inp.get("removedLabelIds"):
            labels = [i for i in labels if i not in set(inp["removedLabelIds"])]
        changes["labelIds"] = labels
        if inp.get("subscriberIds") is not None:
            changes["subscriberIds"] = [
                (self.get("User", u) or _raise(not_found("User")))["id"] for u in inp["subscriberIds"]]
        state = None
        if inp.get("stateId"):
            state = self.get("WorkflowState", inp["stateId"])
            if state is None:
                raise not_found("WorkflowState")
            if state["teamId"] != team["id"]:
                raise invalid("the workflow state belongs to another team")
        if "projectId" in changes and changes["projectId"] != issue["projectId"]:
            changes["addedToProjectAt"] = stamp if changes["projectId"] else None
        issue.update(changes)
        if state is not None and state["id"] != issue["stateId"]:
            self._apply_state(issue, state, stamp)
        issue["updatedAt"] = stamp
        self._emit("update", "Issue", issue, actor, before)
        return issue

    def set_labels(self, actor: dict, ref: str, add: list[str], remove: list[str]) -> dict:
        return self.update_issue(actor, ref, {"addedLabelIds": add, "removedLabelIds": remove})

    def archive_issue(self, actor: dict, ref: str, *, trash: bool, permanently: bool = False) -> dict:
        issue = self.require_issue(ref)
        if permanently:
            for child in self.rows("Issue", archived=True):
                if child["parentId"] == issue["id"]:
                    child["parentId"] = None
            for typename, key in (("Comment", "issueId"), ("Attachment", "issueId"),
                                  ("IssueRelation", "issueId"), ("IssueRelation", "relatedIssueId")):
                for row in list(self.table(typename).values()):
                    if row[key] == issue["id"]:
                        del self.table(typename)[row["id"]]
            del self.table("Issue")[issue["id"]]
            self._emit("remove", "Issue", issue, actor)
            return issue
        before = self._snapshot("Issue", issue)
        stamp = self.now()
        issue.update(archivedAt=stamp, updatedAt=stamp, trashed=True if trash else None)
        self._emit("remove" if trash else "update", "Issue", issue, actor, before)
        return issue

    def unarchive_issue(self, actor: dict, ref: str) -> dict:
        issue = self.require_issue(ref)
        before = self._snapshot("Issue", issue)
        issue.update(archivedAt=None, trashed=None, updatedAt=self.now())
        self._emit("update", "Issue", issue, actor, before)
        return issue

    # -- comments ----------------------------------------------------------------------
    def create_comment(self, actor: dict, inp: dict) -> dict:
        _only(inp, "CommentCreateInput", {"id", "body", "issueId", "parentId",
                                          "doNotSubscribeToIssue"})
        if not inp.get("issueId"):
            raise invalid("a comment needs an issueId (the mock models issue comments only)")
        issue = self.require_issue(inp["issueId"])
        body = inp.get("body")
        if not body or not body.strip():
            raise invalid("body should not be empty")
        parent_id = None
        if inp.get("parentId"):
            parent = self.require("Comment", inp["parentId"])
            if parent["issueId"] != issue["id"]:
                raise invalid("the parent comment belongs to another issue")
            parent_id = parent["parentId"] or parent["id"]  # Linear threads are one level deep
        comment = self.insert("Comment", {
            "body": body, "issueId": issue["id"], "parentId": parent_id, "userId": actor["id"],
            "editedAt": None, "resolvedAt": None, "resolvingCommentId": None,
            "resolvingUserId": None, "quotedText": None, "reactionData": {}, "reactions": [],
            "projectId": None, "projectUpdateId": None, "initiativeId": None,
            "initiativeUpdateId": None, "documentContentId": None}, entity_id=inp.get("id"))
        if not inp.get("doNotSubscribeToIssue") and actor["id"] not in issue["subscriberIds"]:
            issue["subscriberIds"].append(actor["id"])
        self._emit("create", "Comment", comment, actor)
        return comment

    def update_comment(self, actor: dict, ref: str, inp: dict, *, skip_edited_at: bool) -> dict:
        _only(inp, "CommentUpdateInput", {"body"})
        comment = self.require("Comment", ref)
        if comment["userId"] != actor["id"]:
            raise LinearError("Forbidden", type="forbidden", code="FORBIDDEN", status=403,
                              presentable="You can only edit your own comments.")
        before = self._snapshot("Comment", comment)
        if "body" in inp:
            if not inp["body"] or not inp["body"].strip():
                raise invalid("body should not be empty")
            comment["body"] = inp["body"]
        stamp = self.now()
        comment["updatedAt"] = stamp
        if not skip_edited_at:
            comment["editedAt"] = stamp
        self._emit("update", "Comment", comment, actor, before)
        return comment

    def delete_comment(self, actor: dict, ref: str) -> str:
        comment = self.require("Comment", ref)
        if comment["userId"] != actor["id"] and not actor["admin"]:
            raise LinearError("Forbidden", type="forbidden", code="FORBIDDEN", status=403,
                              presentable="You can only delete your own comments.")
        for child in [c for c in self.table("Comment").values() if c["parentId"] == comment["id"]]:
            del self.table("Comment")[child["id"]]
        del self.table("Comment")[comment["id"]]
        self._emit("remove", "Comment", comment, actor)
        return comment["id"]

    # -- relations ---------------------------------------------------------------------
    def create_relation(self, actor: dict, inp: dict) -> dict:
        issue = self.require_issue(inp["issueId"])
        related = self.require_issue(inp["relatedIssueId"])
        if issue["id"] == related["id"]:
            raise invalid("an issue cannot be related to itself")
        kind = inp["type"]
        if any(r["issueId"] == issue["id"] and r["relatedIssueId"] == related["id"]
               and r["type"] == kind for r in self.rows("IssueRelation")):
            raise LinearError("Issue relation already exists",
                              presentable="These issues are already related.")
        return self.insert("IssueRelation", {"type": kind, "issueId": issue["id"],
                                             "relatedIssueId": related["id"]},
                           entity_id=inp.get("id"))

    def delete_relation(self, ref: str) -> str:
        relation = self.require("IssueRelation", ref)
        del self.table("IssueRelation")[relation["id"]]
        return relation["id"]

    # -- attachments -------------------------------------------------------------------
    def create_attachment(self, actor: dict, inp: dict) -> dict:
        _only(inp, "AttachmentCreateInput", {"id", "issueId", "title", "subtitle", "url",
                                             "metadata", "iconUrl", "groupBySource"})
        issue = self.require_issue(inp["issueId"])
        url = inp["url"].strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url):
            raise invalid("url must be a URL")
        existing = next((a for a in self.rows("Attachment")
                         if a["issueId"] == issue["id"] and a["url"] == url), None)
        if existing is not None:  # documented: same URL on the same issue updates the original
            return self.update_attachment(actor, existing["id"], {
                k: v for k, v in inp.items() if k in ("title", "subtitle", "metadata", "iconUrl")})
        attachment = self.insert("Attachment", {
            "issueId": issue["id"], "title": inp["title"], "subtitle": inp.get("subtitle"),
            "url": url, "metadata": inp.get("metadata") or {}, "iconUrl": inp.get("iconUrl"),
            "groupBySource": bool(inp.get("groupBySource")), "creatorId": actor["id"]},
            entity_id=inp.get("id"))
        self._emit("create", "Attachment", attachment, actor)
        return attachment

    def update_attachment(self, actor: dict, ref: str, inp: dict) -> dict:
        attachment = self.require("Attachment", ref)
        before = self._snapshot("Attachment", attachment)
        for key in ("title", "subtitle", "metadata", "iconUrl"):
            if key in inp and inp[key] is not None:
                attachment[key] = inp[key]
        attachment["updatedAt"] = self.now()
        self._emit("update", "Attachment", attachment, actor, before)
        return attachment

    def delete_attachment(self, actor: dict, ref: str) -> str:
        attachment = self.require("Attachment", ref)
        del self.table("Attachment")[attachment["id"]]
        self._emit("remove", "Attachment", attachment, actor)
        return attachment["id"]

    # -- documents ---------------------------------------------------------------------
    DOC_FIELDS = {"id", "title", "content", "icon", "color", "issueId", "projectId", "sortOrder"}

    def create_document(self, actor: dict, inp: dict) -> dict:
        _only(inp, "DocumentCreateInput", self.DOC_FIELDS)
        title = (inp.get("title") or "").strip()
        if not title:
            raise invalid("title should not be empty")
        issue = self.require_issue(inp["issueId"]) if inp.get("issueId") else None
        project = self.require("Project", inp["projectId"]) if inp.get("projectId") else None
        if not issue and not project:
            raise invalid("a document needs a parent: issueId or projectId")
        entity_id = inp.get("id")
        doc = self.insert("Document", {
            "title": title, "content": inp.get("content"), "icon": inp.get("icon"),
            "color": inp.get("color"), "issueId": issue["id"] if issue else None,
            "projectId": project["id"] if project else None, "creatorId": actor["id"],
            "updatedById": actor["id"], "sortOrder": float(inp.get("sortOrder") or 0.0),
            "hiddenAt": None, "trashed": None, "slugId": ""}, entity_id=entity_id)
        doc["slugId"] = self._hexid(doc["id"])
        self._emit("create", "Document", doc, actor)
        return doc

    def update_document(self, actor: dict, ref: str, inp: dict) -> dict:
        _only(inp, "DocumentUpdateInput", self.DOC_FIELDS - {"id"})
        doc = self.require("Document", ref)
        before = self._snapshot("Document", doc)
        if "title" in inp:
            if not (inp["title"] or "").strip():
                raise invalid("title should not be empty")
            doc["title"] = inp["title"].strip()
        for key in ("content", "icon", "color"):
            if key in inp:
                doc[key] = inp[key]
        if inp.get("sortOrder") is not None:
            doc["sortOrder"] = float(inp["sortOrder"])
        if "issueId" in inp:
            doc["issueId"] = self.require_issue(inp["issueId"])["id"] if inp["issueId"] else None
        if "projectId" in inp:
            doc["projectId"] = self.require("Project", inp["projectId"])["id"] \
                if inp["projectId"] else None
        doc.update(updatedAt=self.now(), updatedById=actor["id"])
        self._emit("update", "Document", doc, actor, before)
        return doc

    # -- uploads -----------------------------------------------------------------------
    def create_upload(self, actor: dict, content_type: str, filename: str, size: int,
                      make_public: bool, meta: Any) -> dict:
        if size <= 0:
            raise invalid("size must be positive")
        if not filename.strip() or "/" in filename:
            raise invalid("filename must be a plain file name")
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", content_type):
            raise invalid("contentType must be a MIME type")
        file_id = self.new_id()
        asset_key = self.new_id()
        path = f"{self.org['id']}/{file_id}/{asset_key}"
        record = {"fileId": file_id, "path": path, "contentType": content_type,
                  "originalName": filename, "size": size, "public": bool(make_public),
                  "metaData": meta, "uploaded": False, "createdMs": self.data["clockMs"],
                  "userId": actor["id"]}
        self.data["uploads"][file_id] = record
        return record


def _raise(exc: Exception) -> Any:
    raise exc
