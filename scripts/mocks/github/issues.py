"""Issues as a ticket tracker: issues (open, edit, close, reopen), sub-issues, issue dependencies
(`blocked_by`) and Projects (v2) with their fields and items, each change sending the webhook
GitHub documents for it (`issues`, `sub_issues`, `issue_dependencies`, `projects_v2_item`).

Rules follow GitHub's docs: a sub-issue belongs to the same owner as its parent and has one parent
(`replace_parent` moves it), at most 100 sub-issues per parent and 8 levels; at most 50
`blocked_by` per issue; Projects v2 webhooks exist only for organization projects."""

from __future__ import annotations

import hashlib

from .spec import SpecError
from .state import iso, node_id, parse_node_id

STATUS_OPTIONS = ["Todo", "In Progress", "In Review", "Rework", "Merging", "Done", "Canceled"]
OPTION_COLORS = ["GRAY", "BLUE", "PURPLE", "ORANGE", "YELLOW", "GREEN", "RED", "PINK"]
MAX_SUB_ISSUES = 100
MAX_DEPTH = 8
MAX_BLOCKED_BY = 50
SUB_ISSUES_DOCS = "https://docs.github.com/rest/issues/sub-issues"
DEPENDENCIES_DOCS = "https://docs.github.com/rest/issues/issue-dependencies"


def _invalid(resource: str, message: str, docs: str) -> SpecError:
    return SpecError(422, "Validation Failed", docs,
                     errors=[{"resource": resource, "code": "custom", "message": message}])


def option_id(field_id: int, name: str) -> str:
    """GitHub's single-select option ids are 8 hex characters."""
    return hashlib.sha256(f"{field_id}:{name}".encode()).hexdigest()[:8]


class IssuesMixin:
    """Mixed into `MockGitHub`; uses its store, lock, hooks and renderer."""

    # -- lookup ----------------------------------------------------------------------------
    def issue_by_id(self, issue_id: int) -> tuple[dict, dict] | None:
        for repo in self.store.data["repos"].values():
            for issue in repo["issues"].values():
                if issue["id"] == int(issue_id):
                    return repo, issue
        return None

    def _issue_hook(self, repo: dict, issue: dict, action: str, sender: str, **extra) -> None:
        self.hooks.emit("issues", action, repo, {"issue": self.render().issue(repo, issue), **extra}, sender)

    # -- issues -------------------------------------------------------------------------------
    def create_issue(self, repo: dict, user: str, title: str, body: str | None = None,
                     labels: list[str] | tuple = ()) -> dict:
        with self.lock:
            issue = self._new_issue(repo, user, title, body)
            for name in labels:
                lab = self.store.label(repo, name, create=True)
                if lab["name"] not in issue["labels"]:
                    issue["labels"].append(lab["name"])
            self.store.save()
            self._issue_hook(repo, issue, "opened", user)
            return issue

    def update_issue(self, repo: dict, issue: dict, actor: str, *, title=None, body=None, state=None,
                     state_reason=None, labels=None) -> None:
        with self.lock:
            changes = {}
            if title is not None and title != issue["title"]:
                changes["title"] = {"from": issue["title"]}
                issue["title"] = title
            if body is not None and body != issue.get("body"):
                changes["body"] = {"from": issue.get("body") or ""}
                issue["body"] = body
            if changes:
                self.touch(issue)
                self.store.save()
                self._issue_hook(repo, issue, "edited", actor, changes=changes)
            if labels is not None:
                wanted = [self.store.label(repo, n, create=True)["name"] for n in labels]
                for name in [x for x in issue["labels"] if x not in wanted]:
                    self.remove_label(repo, issue, name, actor)
                self.add_labels(repo, issue, wanted, actor)
            if state is not None and state != issue["state"]:
                if state == "closed":
                    issue.update(state="closed", closed_at=iso(), closed_by=actor,
                                 state_reason=state_reason or "completed")
                    action = "closed"
                else:
                    issue.update(state="open", closed_at=None, closed_by=None, state_reason="reopened")
                    action = "reopened"
                self.touch(issue)
                self.store.save()
                self._issue_hook(repo, issue, action, actor)
            elif state_reason is not None and issue["state"] == "closed":
                issue["state_reason"] = state_reason
                self.touch(issue)
                self.store.save()

    # -- sub-issues ------------------------------------------------------------------------------
    def _depth(self, issue: dict) -> int:
        depth = 1
        while issue.get("parent"):
            found = self.issue_by_id(issue["parent"])
            if found is None:
                break
            issue = found[1]
            depth += 1
        return depth

    def _ancestors(self, issue: dict) -> set[int]:
        out = set()
        while issue.get("parent"):
            out.add(issue["parent"])
            found = self.issue_by_id(issue["parent"])
            if found is None:
                break
            issue = found[1]
        return out

    def _height(self, issue: dict) -> int:
        kids = [self.issue_by_id(i) for i in issue.get("sub_issues") or []]
        return 1 + max((self._height(k[1]) for k in kids if k), default=0)

    def add_sub_issue(self, repo: dict, parent: dict, child_id: int, replace_parent: bool, actor: str) -> None:
        with self.lock:
            found = self.issue_by_id(child_id)
            if found is None:
                raise SpecError(404, "Not Found", SUB_ISSUES_DOCS)
            child_repo, child = found
            if child.get("pull"):
                raise _invalid("Issue", "Pull requests cannot be sub-issues", SUB_ISSUES_DOCS)
            if child_repo["owner"].lower() != repo["owner"].lower():
                raise _invalid("Issue", "Sub-issue must belong to the same owner as the parent issue",
                               SUB_ISSUES_DOCS)
            if child["id"] == parent["id"] or child["id"] in self._ancestors(parent):
                raise _invalid("Issue", "An issue cannot be a sub-issue of itself or of its own sub-issue",
                               SUB_ISSUES_DOCS)
            if child.get("parent") == parent["id"]:
                raise _invalid("Issue", "Issue is already a sub-issue of this parent", SUB_ISSUES_DOCS)
            if child.get("parent") and not replace_parent:
                raise _invalid("Issue", "Issue may not contain duplicate sub-issues and must not have a parent",
                               SUB_ISSUES_DOCS)
            if len(parent.get("sub_issues") or []) >= MAX_SUB_ISSUES:
                raise _invalid("Issue", f"Parent cannot have more than {MAX_SUB_ISSUES} sub-issues",
                               SUB_ISSUES_DOCS)
            if self._depth(parent) + self._height(child) > MAX_DEPTH:
                raise _invalid("Issue", f"Sub-issues cannot be nested more than {MAX_DEPTH} levels",
                               SUB_ISSUES_DOCS)
            if child.get("parent"):
                old = self.issue_by_id(child["parent"])
                if old is not None:
                    self.remove_sub_issue(old[0], old[1], child["id"], actor)
            parent.setdefault("sub_issues", []).append(child["id"])
            child["parent"] = parent["id"]
            self.touch(parent)
            self.touch(child)
            self.store.save()
            self._sub_issue_hooks(repo, parent, child_repo, child, "added", actor)

    def remove_sub_issue(self, repo: dict, parent: dict, child_id: int, actor: str) -> bool:
        with self.lock:
            if child_id not in (parent.get("sub_issues") or []):
                return False
            child_repo, child = self.issue_by_id(child_id)
            parent["sub_issues"].remove(child_id)
            child["parent"] = None
            self.touch(parent)
            self.touch(child)
            self.store.save()
            self._sub_issue_hooks(repo, parent, child_repo, child, "removed", actor)
            return True

    def reprioritize_sub_issue(self, parent: dict, child_id: int, after_id: int | None,
                               before_id: int | None) -> None:
        with self.lock:
            subs = parent.get("sub_issues") or []
            anchor = after_id if after_id is not None else before_id
            if child_id not in subs or anchor not in subs or (after_id is None) == (before_id is None):
                raise _invalid("Issue", "Provide one of after_id or before_id naming a sub-issue of this parent",
                               SUB_ISSUES_DOCS)
            subs.remove(child_id)
            at = subs.index(anchor)
            subs.insert(at + 1 if after_id is not None else at, child_id)
            self.touch(parent)
            self.store.save()

    def _sub_issue_hooks(self, repo, parent, child_repo, child, verb: str, actor: str) -> None:
        r = self.render()
        p, c = r.issue(repo, parent), r.issue(child_repo, child)
        self.hooks.emit("sub_issues", f"sub_issue_{verb}", repo, {
            "sub_issue_id": child["id"], "sub_issue": c, "sub_issue_repo": r.repo(child_repo),
            "parent_issue_id": parent["id"], "parent_issue": p}, actor)
        self.hooks.emit("sub_issues", f"parent_issue_{verb}", child_repo, {
            "parent_issue_id": parent["id"], "parent_issue": p, "parent_issue_repo": r.repo(repo),
            "sub_issue_id": child["id"], "sub_issue": c}, actor)

    # -- dependencies ---------------------------------------------------------------------------
    def blocking_of(self, issue: dict) -> list[tuple[dict, dict]]:
        return [(r, i) for r in self.store.data["repos"].values() for i in r["issues"].values()
                if issue["id"] in (i.get("blocked_by") or [])]

    def add_blocked_by(self, repo: dict, issue: dict, blocker_id: int, actor: str) -> None:
        with self.lock:
            found = self.issue_by_id(blocker_id)
            if found is None:
                raise SpecError(404, "Not Found", DEPENDENCIES_DOCS)
            blocker_repo, blocker = found
            if blocker["id"] == issue["id"]:
                raise _invalid("IssueDependency", "An issue cannot be blocked by itself", DEPENDENCIES_DOCS)
            if blocker["id"] in (issue.get("blocked_by") or []):
                raise _invalid("IssueDependency", "Issue is already blocked by this issue", DEPENDENCIES_DOCS)
            if issue["id"] in (blocker.get("blocked_by") or []):
                raise _invalid("IssueDependency", "Issues cannot block each other", DEPENDENCIES_DOCS)
            if len(issue.get("blocked_by") or []) >= MAX_BLOCKED_BY:
                raise _invalid("IssueDependency", f"An issue cannot be blocked by more than {MAX_BLOCKED_BY} issues",
                               DEPENDENCIES_DOCS)
            issue.setdefault("blocked_by", []).append(blocker["id"])
            self.touch(issue)
            self.store.save()
            self._dependency_hooks(repo, issue, blocker_repo, blocker, "added", actor)

    def remove_blocked_by(self, repo: dict, issue: dict, blocker_id: int, actor: str) -> bool:
        with self.lock:
            if blocker_id not in (issue.get("blocked_by") or []):
                return False
            issue["blocked_by"].remove(blocker_id)
            self.touch(issue)
            self.store.save()
            found = self.issue_by_id(blocker_id)
            if found is not None:
                self._dependency_hooks(repo, issue, found[0], found[1], "removed", actor)
            return True

    def _dependency_hooks(self, repo, issue, blocker_repo, blocker, verb: str, actor: str) -> None:
        r = self.render()
        body = {"blocked_issue_id": issue["id"], "blocked_issue": r.issue(repo, issue),
                "blocking_issue_id": blocker["id"], "blocking_issue": r.issue(blocker_repo, blocker),
                "blocking_issue_repo": r.repo(blocker_repo)}
        self.hooks.emit("issue_dependencies", f"blocked_by_{verb}", repo, body, actor)
        if blocker_repo is not repo:
            self.hooks.emit("issue_dependencies", f"blocking_{verb}", blocker_repo, body, actor)

    # -- Projects (v2) ------------------------------------------------------------------------------
    def projects(self) -> dict:
        return self.store.data["projects"]

    def project(self, owner: str, number: int) -> dict | None:
        return next((p for p in self.projects().values()
                     if p["owner"].lower() == owner.lower() and p["number"] == int(number)), None)

    def project_by_id(self, project_id: int) -> dict | None:
        return self.projects().get(str(project_id))

    def create_project(self, owner: str, number: int | None = None, *, title: str = "Symphony",
                       priority: bool | list[str] = False, creator: str | None = None) -> dict:
        """An owner's project with GitHub's default `Title` field, a `Status` single-select
        (Symphony's workflow states) and, with `priority`, a `Priority` field: a NUMBER field for
        `True`, a single-select with those options for a list."""
        with self.lock:
            acc = self.store.account(owner, "Organization")
            taken = [p["number"] for p in self.projects().values() if p["owner"].lower() == owner.lower()]
            if number is None:
                number = max(taken, default=0) + 1
            if number in taken:
                raise SpecError(422, f"project {owner}#{number} already exists")
            pid = self.store.next_id()
            now = iso()
            project = {"id": pid, "owner": acc["login"], "number": int(number), "title": title,
                       "creator": creator or acc["login"], "created_at": now, "updated_at": now,
                       "fields": [], "items": []}
            self._field(project, "Title", "TITLE")
            self._field(project, "Status", "SINGLE_SELECT", STATUS_OPTIONS)
            if priority is True:
                self._field(project, "Priority", "NUMBER")
            elif priority:
                self._field(project, "Priority", "SINGLE_SELECT", list(priority))
            self.projects()[str(pid)] = project
            self.store.save()
            return project

    def _field(self, project: dict, name: str, kind: str, options: list[str] | None = None) -> dict:
        fid = self.store.next_id()
        field = {"id": fid, "name": name, "type": kind, "created_at": project["created_at"]}
        if kind == "SINGLE_SELECT":
            field["options"] = [{"id": option_id(fid, o), "name": o, "color": OPTION_COLORS[i % len(OPTION_COLORS)],
                                 "description": ""} for i, o in enumerate(options or [])]
        project["fields"].append(field)
        return field

    def project_field(self, project: dict, name: str) -> dict | None:
        return next((f for f in project["fields"] if f["name"].lower() == name.lower()), None)

    def project_item(self, project: dict, item_id: int) -> dict | None:
        return next((i for i in project["items"] if i["id"] == int(item_id)), None)

    def item_content(self, item: dict) -> tuple[dict, dict] | None:
        if item["type"] == "DRAFT_ISSUE":
            return None
        repo = self.repo_by_id(item["repo_id"])
        issue = self.issue(repo, item["number"]) if repo else None
        return (repo, issue) if issue else None

    def add_project_item(self, owner: str, number: int, *, repo: dict | None = None, issue_number: int | None = None,
                         draft_title: str | None = None, actor: str | None = None) -> str:
        """Seeding shortcut: adds issue/PR `issue_number` of `repo` (or a draft titled `draft_title`)
        to project `owner`#`number`; returns the item's node id."""
        project = self.project(owner, number)
        if project is None:
            raise SpecError(404, f"no project {owner}#{number}")
        issue = None
        if repo is not None:
            issue = self.issue(repo, int(issue_number))
            if issue is None:
                raise SpecError(404, f"no issue or pull request {repo['full_name']}#{issue_number}")
        elif draft_title is None:
            raise SpecError(400, "give repo and issue_number, or draft_title")
        item = self.add_item(project, actor or project["owner"], repo=repo, issue=issue, draft_title=draft_title)
        return self.item_node_id(project, item)

    def add_item(self, project: dict, actor: str, *, repo: dict | None = None, issue: dict | None = None,
                 draft_title: str | None = None, draft_body: str = "") -> dict:
        """Adds an issue or pull request (or a draft) to `project`; an issue already there returns
        its item, as `addProjectV2ItemById` does."""
        with self.lock:
            if issue is not None:
                for item in project["items"]:
                    if item["type"] != "DRAFT_ISSUE" and item["repo_id"] == repo["id"] \
                            and item["number"] == issue["number"]:
                        return item
            now = iso()
            item = {"id": self.store.next_id(), "creator": actor, "created_at": now, "updated_at": now,
                    "archived_at": None, "values": {}}
            if issue is not None:
                item.update(type="PULL_REQUEST" if issue.get("pull") else "ISSUE", repo_id=repo["id"],
                            number=issue["number"])
            else:
                item.update(type="DRAFT_ISSUE", draft_id=self.store.next_id(), title=draft_title or "",
                            body=draft_body)
            project["items"].append(item)
            project["updated_at"] = now
            self.store.save()
            self._item_hook(project, item, "created", actor)
            return item

    def delete_project_item(self, project: dict, item: dict, actor: str) -> None:
        with self.lock:
            project["items"].remove(item)
            project["updated_at"] = iso()
            self.store.save()
            self._item_hook(project, item, "deleted", actor)

    def set_item_value(self, project: dict, item: dict, field: dict, value, actor: str) -> None:
        """Sets a field: an option id for a single-select, a number, or text. Raises ValueError
        (with GitHub's message) when the value does not fit the field."""
        with self.lock:
            kind = field["type"]
            if kind == "SINGLE_SELECT":
                if not any(o["id"] == value for o in field["options"]):
                    raise ValueError(f"The single select option Id does not belong to the field {field['name']}")
            elif kind == "NUMBER":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"The field {field['name']} requires a number value")
            elif kind == "TEXT":
                if not isinstance(value, str):
                    raise ValueError(f"The field {field['name']} requires a text value")
            else:
                raise ValueError(f"The field of type {kind} cannot be updated")
            before = item["values"].get(str(field["id"]))
            item["values"][str(field["id"])] = value
            item["updated_at"] = project["updated_at"] = iso()
            self.store.save()
            if before != value:
                self._item_hook(project, item, "edited", actor, changes={"field_value": {
                    "field_node_id": self.field_node_id(project, field), "field_type": kind.lower(),
                    "field_name": field["name"], "project_number": project["number"],
                    "from": self._hook_value(field, before), "to": self._hook_value(field, value)}})

    def set_item_field(self, owner: str, number: int, item_id: int | str, field_name: str, value,
                       actor: str | None = None) -> dict:
        """Seeding shortcut: sets `field_name` on an item (database id or node id) by option NAME
        (single-select) or value."""
        project = self.project(owner, number)
        if project is None:
            raise SpecError(404, f"no project {owner}#{number}")
        if isinstance(item_id, str) and not item_id.isdigit():
            parsed = parse_node_id(item_id)
            item_id = int(parsed[1][-1]) if parsed and parsed[0] == "PVTI" else -1
        item = self.project_item(project, item_id)
        field = self.project_field(project, field_name)
        if item is None or field is None:
            raise SpecError(404, f"no item {item_id} or field {field_name!r} in {owner}#{number}")
        if field["type"] == "SINGLE_SELECT":
            option = next((o for o in field["options"] if o["name"].lower() == str(value).lower()), None)
            if option is None:
                raise SpecError(422, f"no option {value!r} on {field_name}")
            value = option["id"]
        try:
            self.set_item_value(project, item, field, value, actor or project["owner"])
        except ValueError as exc:
            raise SpecError(422, str(exc)) from None
        return item

    @staticmethod
    def _hook_value(field: dict, value):
        if value is None:
            return None
        if field["type"] == "SINGLE_SELECT":
            o = next((o for o in field["options"] if o["id"] == value), None)
            return {"id": o["id"], "name": o["name"], "color": o["color"].lower(), "description": o["description"]} \
                if o else None
        return value

    @staticmethod
    def project_node_id(project: dict) -> str:
        return node_id("PVT", project["id"])

    @staticmethod
    def item_node_id(project: dict, item: dict) -> str:
        return node_id("PVTI", project["id"], item["id"])

    @staticmethod
    def field_node_id(project: dict, field: dict) -> str:
        prefix = {"SINGLE_SELECT": "PVTSSF", "ITERATION": "PVTIF"}.get(field["type"], "PVTF")
        return node_id(prefix, project["id"], field["id"])

    def _item_hook(self, project: dict, item: dict, action: str, actor: str, **extra) -> None:
        owner = self.store.account(project["owner"])
        if owner["type"] != "Organization":
            return  # GitHub sends projects_v2_item for organization projects only
        content = self.item_content(item)
        if content is not None:
            repo, issue = content
            content_node = node_id("PR" if issue.get("pull") else "I", repo["id"], issue["number"])
        else:
            content_node = node_id("DI", item.get("draft_id", item["id"]))
        r = self.render()
        body = {"projects_v2_item": {
            "id": item["id"], "node_id": self.item_node_id(project, item),
            "project_node_id": self.project_node_id(project), "content_node_id": content_node,
            "content_type": {"ISSUE": "Issue", "PULL_REQUEST": "PullRequest"}.get(item["type"], "DraftIssue"),
            "creator": r.user(item["creator"]), "created_at": item["created_at"], "updated_at": item["updated_at"],
            "archived_at": item["archived_at"]}, **extra}
        self.hooks.emit("projects_v2_item", action, None, body, actor, owner=project["owner"])
