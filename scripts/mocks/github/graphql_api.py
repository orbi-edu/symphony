"""GraphQL on GitHub's published schema (`schema.docs.graphql`, graphql-core): documents are
parsed and validated against the real schema and executed with resolvers over the mock's
state. Objects are dicts keyed by GraphQL field names; fields with arguments are callables
(graphql-core's default resolver calls them with the field's arguments).

Errors follow GitHub's shape: a `type` (`NOT_FOUND`, `FORBIDDEN`, `UNPROCESSABLE`) next to
`message`, `path` and `locations`; a missing repository or pull request is `null` plus an error.
"""

from __future__ import annotations

import base64
from typing import Any

from graphql import GraphQLError, graphql_sync

from . import gitrepo, spec
from .spec import SpecError
from .state import LEVEL, Actor, node_id, parse_node_id

FORBIDDEN = "Resource not accessible by integration"
# Node-id prefixes whose parts are all database ids, and how many parts each has.
NUMERIC_IDS = {"R": 1, "U": 1, "BOT": 1, "O": 1, "IC": 1, "PVT": 1, "I": 2, "PR": 2, "LA": 2, "PVTI": 2,
               "PVTF": 2, "PVTSSF": 2}


class GqlError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _cursor(i: int) -> str:
    return base64.b64encode(f"cursor:v2:{i}".encode()).decode()


def _uncursor(c: str | None) -> int | None:
    if not c:
        return None
    try:
        return int(base64.b64decode(c).decode().rsplit(":", 1)[-1])
    except Exception:  # noqa: BLE001
        raise GqlError("INVALID_CURSOR_ARGUMENTS", f"`{c}` does not appear to be a valid cursor.") from None


def connection(info, items: list, first=None, last=None, after=None, before=None, **_) -> dict:
    """A Relay connection over `items`, paginated as GitHub does (`first`/`last` ≤ 100, one required)."""
    name = info.field_name
    if first is None and last is None:
        raise GqlError("MISSING_PAGINATION_BOUNDARIES",
                       f"You must provide a `first` or `last` value to properly paginate the `{name}` connection.")
    for arg, val in (("first", first), ("last", last)):
        if val is not None and not 0 <= val <= 100:
            raise GqlError("EXCESSIVE_PAGINATION",
                           f"Requesting {val} records on the `{name}` connection exceeds the `{arg}` limit of 100 records.")
    lo, hi = 0, len(items)
    a, b = _uncursor(after), _uncursor(before)
    if a is not None:
        lo = a + 1
    if b is not None:
        hi = min(hi, b)
    idx = list(range(lo, hi))
    if first is not None:
        idx = idx[:first]
    if last is not None:
        idx = idx[-last:] if last else []
    edges = [{"cursor": _cursor(i), "node": items[i]} for i in idx]
    return {
        "edges": edges, "nodes": [e["node"] for e in edges], "totalCount": len(items),
        "pageInfo": {"hasNextPage": (idx[-1] if idx else lo - 1) < hi - 1,
                     "hasPreviousPage": (idx[0] if idx else hi) > lo,
                     "startCursor": edges[0]["cursor"] if edges else None,
                     "endCursor": edges[-1]["cursor"] if edges else None},
    }


class Graph:
    """Resolvers for one request: the mock, the authenticated actor and the URLs to render."""

    def __init__(self, mock, actor: Actor, base: str) -> None:
        self.m = mock
        self.actor = actor
        self.base = base.rstrip("/")

    # -- access ---------------------------------------------------------------------------
    def _visible(self, repo: dict | None) -> bool:
        return repo is not None and self.m.store.can_see(self.actor, repo["full_name"])

    def _need(self, *alternatives: dict) -> None:
        if not any(all(self.actor.level(p) >= LEVEL[lv] for p, lv in alt.items()) for alt in alternatives):
            raise GqlError("FORBIDDEN", FORBIDDEN)

    def _node(self, value: str, *prefixes: str) -> tuple[str, list[str]]:
        parsed = parse_node_id(value or "")
        if parsed is not None and parsed[0] in NUMERIC_IDS:
            parts = parsed[1]
            if len(parts) != NUMERIC_IDS[parsed[0]] or not all(p.isdigit() for p in parts):
                parsed = None
        if parsed is None or (prefixes and parsed[0] not in prefixes):
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{value}'")
        return parsed

    def _pr_by_id(self, value: str) -> tuple[dict, dict]:
        _, (repo_id, number) = self._node(value, "PR")
        repo = self.m.repo_by_id(int(repo_id))
        issue = self.m.issue(repo, int(number)) if self._visible(repo) else None
        if issue is None or not issue.get("pull"):
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{value}'")
        return repo, issue

    def _labelable(self, value: str) -> tuple[dict, dict]:
        prefix, parts = self._node(value, "PR", "I")
        repo = self.m.repo_by_id(int(parts[0]))
        issue = self.m.issue(repo, int(parts[1])) if self._visible(repo) else None
        if issue is None:
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{value}'")
        return repo, issue

    # -- root ----------------------------------------------------------------------------------
    def root(self) -> dict:
        return {
            "repository": self.repository, "viewer": lambda info: self.actor_obj(self.actor.login),
            "node": lambda info, id: self.node(id), "nodes": lambda info, ids: [self._node_or_error(i) for i in ids],
            "rateLimit": lambda info, dryRun=False: {"cost": 1, "limit": 5000, "remaining": 4999, "used": 1,
                                                     "nodeCount": 1, "resetAt": spec.EPOCH},
            "createPullRequest": self.create_pull_request,
            "convertPullRequestToDraft": lambda info, input: self._draft(input, True),
            "markPullRequestReadyForReview": lambda info, input: self._draft(input, False),
            "mergePullRequest": self.merge_pull_request,
            "closePullRequest": lambda info, input: self._state(input, "closed"),
            "reopenPullRequest": lambda info, input: self._state(input, "open"),
            "updatePullRequest": self.update_pull_request,
            "addComment": self.add_comment,
            "updateIssueComment": self.update_issue_comment,
            "addLabelsToLabelable": lambda info, input: self._labels(input, True),
            "removeLabelsFromLabelable": lambda info, input: self._labels(input, False),
            "organization": lambda info, login: self._owner(login, "Organization"),
            "user": lambda info, login: self._owner(login, "User"),
            "repositoryOwner": lambda info, login: self._owner(login, None),
            "updateProjectV2ItemFieldValue": self.update_item_field_value,
            "addProjectV2ItemById": self.add_project_item,
            "deleteProjectV2Item": self.delete_project_item,
        }

    def repository(self, info, owner: str, name: str, followRenames: bool = True):
        repo = self.m.find_repo(owner, name)
        if not self._visible(repo):
            raise GqlError("NOT_FOUND", f"Could not resolve to a Repository with the name '{owner}/{name}'.")
        return self.repo_obj(repo)

    def _node_or_error(self, value: str):
        """One `nodes` entry: an id that does not resolve is a null entry plus a NOT_FOUND error at its
        index, as GitHub answers (graphql-core turns a returned exception into that element's error)."""
        try:
            found = self.node(value)
        except GqlError as exc:
            return exc
        return found if found is not None else GqlError(
            "NOT_FOUND", f"Could not resolve to a node with the global id of '{value}'")

    def node(self, value: str):
        prefix, parts = self._node(value)
        if prefix == "R":
            repo = self.m.repo_by_id(int(parts[0]))
            if self._visible(repo):
                return self.repo_obj(repo)
        elif prefix in ("PR", "I"):
            repo, issue = self._labelable(value)
            return self.pr_obj(repo, issue) if issue.get("pull") else self.issue_obj(repo, issue)
        elif prefix in ("U", "BOT", "O"):
            acc = next((a for a in self.m.store.data["accounts"].values() if a["id"] == int(parts[0])), None)
            if acc:
                return self.owner_obj(acc["login"]) if acc["type"] != "Bot" else self.actor_obj(acc["login"])
        elif prefix == "PVT":
            project = self.m.project_by_id(int(parts[0]))
            if project is not None and self._project_ok(project):
                return self.project_obj(project)
        elif prefix == "PVTI":
            project = self.m.project_by_id(int(parts[0]))
            item = self.m.project_item(project, int(parts[1])) if project else None
            if item is not None and self._project_ok(project):
                return self.item_obj(project, item)
        elif prefix in ("PVTF", "PVTSSF"):
            project = self.m.project_by_id(int(parts[0]))
            field = next((f for f in project["fields"] if f["id"] == int(parts[1])), None) if project else None
            if field is not None and self._project_ok(project):
                return self.field_obj(project, field)
        return None

    # -- objects ------------------------------------------------------------------------------------
    def actor_obj(self, login: str | None) -> dict | None:
        if not login:
            return None
        acc = self.m.store.account(login)
        kind = {"Bot": "Bot", "Organization": "Organization"}.get(acc["type"], "User")
        prefix = {"Bot": "BOT", "Organization": "O"}.get(kind, "U")
        shown = acc["login"].removesuffix("[bot]") if kind == "Bot" else acc["login"]
        return {"__typename": kind, "id": node_id(prefix, acc["id"]), "databaseId": acc["id"], "login": shown,
                "name": acc.get("name"), "url": f"{self.base}/{acc['login']}", "resourcePath": f"/{acc['login']}",
                "avatarUrl": lambda info, size=None: f"{self.base}/avatars/{acc['id']}",
                "createdAt": spec.EPOCH, "updatedAt": spec.EPOCH}

    def repo_obj(self, repo: dict) -> dict:
        path = self.m.path(repo)
        fn = repo["full_name"]
        level = "ADMIN" if self.actor.kind == "user" else (
            "WRITE" if self.actor.level("contents") >= 2 else "READ")
        return {
            "__typename": "Repository", "id": node_id("R", repo["id"]), "databaseId": repo["id"], "name": repo["name"],
            "nameWithOwner": fn, "owner": self.actor_obj(repo["owner"]), "url": f"{self.base}/{fn}",
            "resourcePath": f"/{fn}", "isPrivate": repo["private"], "visibility": "PRIVATE" if repo["private"] else "PUBLIC",
            "description": repo.get("description") or None, "createdAt": repo["created_at"],
            "updatedAt": repo.get("pushed_at") or repo["created_at"], "pushedAt": repo.get("pushed_at"),
            "isArchived": False, "isDisabled": False, "isFork": False, "isEmpty": False, "isLocked": False,
            "isTemplate": False, "isMirror": False, "hasIssuesEnabled": True, "hasWikiEnabled": False,
            "hasProjectsEnabled": False, "mergeCommitAllowed": True, "squashMergeAllowed": True,
            "rebaseMergeAllowed": True, "deleteBranchOnMerge": False, "autoMergeAllowed": False,
            "viewerPermission": level, "sshUrl": f"git@mock.invalid:{fn}.git",
            "defaultBranchRef": lambda info: self.ref_obj(repo, repo["default_branch"]),
            "ref": lambda info, qualifiedName: self.ref_obj(repo, qualifiedName.removeprefix("refs/heads/")),
            "pullRequest": lambda info, number: self._pull(repo, number),
            "issueOrPullRequest": lambda info, number: self._issue_or_pull(repo, number),
            "pullRequests": lambda info, **a: self._pulls(info, repo, **a),
            "labels": lambda info, query=None, **a: connection(info, [
                self.label_obj(repo, x) for x in sorted(repo["labels"].values(), key=lambda x: x["name"].lower())
                if not query or query.lower() in x["name"].lower()], **a),
            "label": lambda info, name: (lambda x: self.label_obj(repo, x) if x else None)(self.m.store.label(repo, name)),
            "issue": lambda info, number: self._plain_issue(repo, number),
            "issues": lambda info, states=None, labels=None, orderBy=None, filterBy=None, **a: connection(info, [
                self.issue_obj(repo, i) for i in sorted(repo["issues"].values(), key=lambda i: i["number"])
                if not i.get("pull") and (not states or i["state"].upper() in states)
                and (not labels or set(labels) & set(i["labels"]))], **a),
            "object": lambda info, oid=None, expression=None: self.commit_obj(repo, gitrepo.resolve(path, oid or expression))
            if gitrepo.resolve(path, oid or expression or "") else None,
        }

    def ref_obj(self, repo: dict, branch: str) -> dict | None:
        sha = gitrepo.branch_sha(self.m.path(repo), branch)
        if sha is None:
            return None
        return {"__typename": "Ref", "id": node_id("REF", repo["id"], f"refs/heads/{branch}"), "name": branch,
                "prefix": "refs/heads/", "target": self.commit_obj(repo, sha), "repository": self.repo_obj(repo)}

    def commit_obj(self, repo: dict, sha: str) -> dict:
        info = gitrepo.commit_info(self.m.path(repo), sha)
        fn = repo["full_name"]
        headline, _, body = info.message.partition("\n")
        return {
            "__typename": "Commit", "id": node_id("C", repo["id"], sha), "oid": sha, "abbreviatedOid": sha[:7],
            "message": info.message, "messageHeadline": headline, "messageBody": body.strip(),
            "committedDate": info.committer[2], "authoredDate": info.author[2], "url": f"{self.base}/{fn}/commit/{sha}",
            "commitUrl": f"{self.base}/{fn}/commit/{sha}", "resourcePath": f"/{fn}/commit/{sha}",
            "author": {"name": info.author[0], "email": info.author[1], "date": info.author[2], "user": None},
            "committer": {"name": info.committer[0], "email": info.committer[1], "date": info.committer[2], "user": None},
            "statusCheckRollup": lambda info_: self.rollup(repo, sha),
            "repository": lambda info_: self.repo_obj(repo),
        }

    def rollup(self, repo: dict, sha: str) -> dict | None:
        checks = self.m.ci.latest_checks(repo, sha)
        statuses = self.m.ci.latest_statuses(repo, sha)
        if not checks and not statuses:
            return None
        bad = {"failure", "timed_out", "action_required", "startup_failure", "cancelled"}
        if any(c.get("conclusion") in bad for c in checks) or any(s["state"] in ("failure", "error") for s in statuses):
            state = "FAILURE"
        elif any(c["status"] != "completed" for c in checks) or any(s["state"] == "pending" for s in statuses):
            state = "PENDING"
        else:
            state = "SUCCESS"
        r = self.m.render(self.base)
        items = [{"__typename": "CheckRun", "id": node_id("CR", c["id"]), "databaseId": c["id"], "name": c["name"],
                  "status": c["status"].upper(), "conclusion": (c.get("conclusion") or "").upper() or None,
                  "startedAt": c.get("started_at"), "completedAt": c.get("completed_at"),
                  "detailsUrl": r.check_run(repo, c)["html_url"], "url": r.check_run(repo, c)["html_url"]}
                 for c in checks]
        items += [{"__typename": "StatusContext", "id": node_id("SC", s["id"]), "context": s["context"],
                   "state": s["state"].upper(), "targetUrl": s.get("target_url"), "description": s.get("description"),
                   "createdAt": s["created_at"]} for s in statuses]
        return {"__typename": "StatusCheckRollup", "id": node_id("SCR", repo["id"], sha), "state": state,
                "contexts": lambda info, **a: connection(info, items, **a)}

    def label_obj(self, repo: dict, lab: dict) -> dict:
        return {"__typename": "Label", "id": node_id("LA", repo["id"], lab["id"]), "name": lab["name"],
                "color": lab["color"], "description": lab.get("description"), "isDefault": lab["default"],
                "url": f"{self.base}/{repo['full_name']}/labels/{lab['name']}", "createdAt": repo["created_at"]}

    def _pull(self, repo: dict, number: int):
        issue = self.m.issue(repo, number)
        if issue is None or not issue.get("pull"):
            raise GqlError("NOT_FOUND", f"Could not resolve to a PullRequest with the number of {number}.")
        return self.pr_obj(repo, issue)

    def _issue_or_pull(self, repo: dict, number: int):
        issue = self.m.issue(repo, number)
        if issue is None:
            raise GqlError("NOT_FOUND", f"Could not resolve to an issue or pull request with the number of {number}.")
        return self.pr_obj(repo, issue) if issue.get("pull") else self.issue_obj(repo, issue)

    def _pulls(self, info, repo: dict, states=None, labels=None, headRefName=None, baseRefName=None, orderBy=None, **a):
        items = self.m.pulls(repo)
        if states:
            want = {s.lower() for s in states}
            items = [i for i in items if ("merged" if i["pull"]["merged"] else i["state"]) in want]
        if labels:
            items = [i for i in items if set(labels) & set(i["labels"])]
        if headRefName:
            items = [i for i in items if i["pull"]["head_ref"] == headRefName]
        if baseRefName:
            items = [i for i in items if i["pull"]["base_ref"] == baseRefName]
        field = {"UPDATED_AT": "updated_at"}.get((orderBy or {}).get("field"), "created_at")
        items.sort(key=lambda i: (i[field], i["number"]), reverse=(orderBy or {}).get("direction") == "DESC")
        return connection(info, [self.pr_obj(repo, i) for i in items], **a)

    def _plain_issue(self, repo: dict, number: int):
        issue = self.m.issue(repo, number)
        if issue is None or issue.get("pull"):
            raise GqlError("NOT_FOUND", f"Could not resolve to an Issue with the number of {number}.")
        self._need({"issues": "read"})
        return self.issue_obj(repo, issue)

    def _issues_by_id(self, ids) -> list[dict]:
        out = []
        for i in ids or []:
            found = self.m.issue_by_id(i)
            if found is not None and self._visible(found[0]):
                out.append(self.issue_obj(*found))
        return out

    def issue_obj(self, repo: dict, issue: dict) -> dict:
        fn, n = repo["full_name"], issue["number"]
        subs = self._issues_by_id(issue.get("sub_issues"))
        done = sum(1 for s in subs if s["closed"])
        parent = self.m.issue_by_id(issue["parent"]) if issue.get("parent") else None
        return {"__typename": "Issue", "id": node_id("I", repo["id"], n), "databaseId": issue["id"],
                "fullDatabaseId": str(issue["id"]), "number": n,
                "title": issue["title"], "body": issue.get("body") or "", "bodyText": issue.get("body") or "",
                "state": issue["state"].upper(), "closedAt": issue.get("closed_at"),
                "stateReason": lambda info, enableDuplicate=False: (issue.get("state_reason") or "").upper() or None,
                "url": f"{self.base}/{fn}/issues/{n}", "resourcePath": f"/{fn}/issues/{n}",
                "createdAt": issue["created_at"], "updatedAt": issue["updated_at"],
                "author": self.actor_obj(issue["user"]), "closed": issue["state"] == "closed",
                "repository": lambda info: self.repo_obj(repo),
                "labels": lambda info, **a: connection(info, self._labels_of(repo, issue), **a),
                "assignees": lambda info, **a: connection(info, [], **a),
                "comments": lambda info, orderBy=None, **a: connection(info, [
                    self.comment_obj(repo, c) for c in sorted(repo["comments"].values(), key=lambda c: c["id"])
                    if c["issue"] == n], **a),
                "parent": lambda info: self.issue_obj(*parent) if parent and self._visible(parent[0]) else None,
                "subIssues": lambda info, **a: connection(info, subs, **a),
                "subIssuesSummary": {"total": len(subs), "completed": done,
                                     "percentCompleted": done * 100 // len(subs) if subs else 0},
                "blockedBy": lambda info, orderBy=None, **a: connection(
                    info, self._issues_by_id(issue.get("blocked_by")), **a),
                "blocking": lambda info, orderBy=None, **a: connection(info, [
                    self.issue_obj(r, i) for r, i in self.m.blocking_of(issue) if self._visible(r)], **a),
                "projectItems": lambda info, includeArchived=True, **a: connection(
                    info, self._items_of(repo, issue), **a)}

    def _labels_of(self, repo: dict, issue: dict) -> list[dict]:
        return [self.label_obj(repo, self.m.store.label(repo, n, create=True)) for n in issue["labels"]]

    def pr_obj(self, repo: dict, issue: dict) -> dict:
        m = self.m
        pr = issue["pull"]
        info = m.pr_info(repo, issue)
        fn, n = repo["full_name"], issue["number"]
        state = "MERGED" if pr["merged"] else issue["state"].upper()
        mergeable = "UNKNOWN" if info["mergeable"] is None else "MERGEABLE" if info["mergeable"] else "CONFLICTING"
        merge_state = {"clean": "CLEAN", "dirty": "DIRTY", "blocked": "BLOCKED", "unstable": "UNSTABLE",
                       "draft": "DRAFT"}.get(info["mergeable_state"], "UNKNOWN")
        path = m.path(repo)
        obj: dict[str, Any] = {
            "__typename": "PullRequest", "id": node_id("PR", repo["id"], n), "databaseId": issue["id"], "fullDatabaseId": str(issue["id"]),
            "number": n, "title": issue["title"], "body": issue.get("body") or "", "bodyText": issue.get("body") or "",
            "url": f"{self.base}/{fn}/pull/{n}", "permalink": f"{self.base}/{fn}/pull/{n}", "resourcePath": f"/{fn}/pull/{n}",
            "state": state, "isDraft": pr["draft"], "closed": issue["state"] == "closed", "merged": bool(pr["merged"]),
            "mergedAt": pr.get("merged_at"), "closedAt": issue.get("closed_at"), "createdAt": issue["created_at"],
            "updatedAt": issue["updated_at"], "locked": False, "baseRefName": pr["base_ref"], "baseRefOid": info["base_sha"],
            "headRefName": pr["head_ref"], "headRefOid": info["head_sha"], "isCrossRepository": False,
            "maintainerCanModify": False, "mergeable": mergeable, "mergeStateStatus": merge_state,
            "canBeRebased": bool(info["mergeable"]), "additions": info["additions"], "deletions": info["deletions"],
            "changedFiles": info["changed_files"], "author": self.actor_obj(issue["user"]),
            "mergedBy": self.actor_obj(pr.get("merged_by")), "reviewDecision": None, "isInMergeQueue": False,
            "authorAssociation": "MEMBER", "viewerDidAuthor": issue["user"].lower() == self.actor.login.lower(),
            "repository": lambda info_: self.repo_obj(repo), "baseRepository": lambda info_: self.repo_obj(repo),
            "headRepository": lambda info_: self.repo_obj(repo),
            "headRepositoryOwner": lambda info_: self.actor_obj(repo["owner"]),
            "baseRef": lambda info_: self.ref_obj(repo, pr["base_ref"]),
            "headRef": lambda info_: self.ref_obj(repo, pr["head_ref"]),
            "mergeCommit": lambda info_: self.commit_obj(repo, pr["merge_commit_sha"]) if pr.get("merge_commit_sha") else None,
            "labels": lambda info_, **a: connection(info_, self._labels_of(repo, issue), **a),
            "timelineItems": lambda info_, itemTypes=None, since=None, skip=None, **a: connection(
                info_, self._timeline(repo, issue, itemTypes), **a),
            "commits": lambda info_, **a: connection(info_, [
                {"__typename": "PullRequestCommit", "id": node_id("PURC", repo["id"], n, s), "commit": self.commit_obj(repo, s),
                 "url": f"{self.base}/{fn}/pull/{n}/commits/{s}"}
                for s in gitrepo.range_commits(path, info["base_sha"], info["head_sha"])], **a),
            "comments": lambda info_, orderBy=None, **a: connection(info_, [
                self.comment_obj(repo, c) for c in sorted(repo["comments"].values(), key=lambda c: c["id"])
                if c["issue"] == n], **a),
            "reviews": lambda info_, states=None, author=None, **a: connection(info_, [
                self.review_obj(repo, r) for r in sorted(repo["reviews"].values(), key=lambda r: r["id"])
                if r["pr"] == n and (not states or r["state"] in states)], **a),
            "files": lambda info_, **a: connection(info_, [
                {"__typename": "PullRequestChangedFile", "path": f.filename, "additions": f.additions,
                 "deletions": f.deletions, "changeType": {"added": "ADDED", "removed": "DELETED", "renamed": "RENAMED",
                                                          "copied": "COPIED"}.get(f.status, "MODIFIED"),
                 "viewerViewedState": "UNVIEWED"}
                for f in gitrepo.files(path, info["base_sha"], info["head_sha"])], **a),
        }
        return obj

    def _timeline(self, repo: dict, issue: dict, types) -> list[dict]:
        names = {"READY_FOR_REVIEW_EVENT": "ReadyForReviewEvent", "CONVERT_TO_DRAFT_EVENT": "ConvertToDraftEvent",
                 "MERGED_EVENT": "MergedEvent", "CLOSED_EVENT": "ClosedEvent", "REOPENED_EVENT": "ReopenedEvent",
                 "HEAD_REF_FORCE_PUSHED_EVENT": "HeadRefForcePushedEvent", "ISSUE_COMMENT": "IssueComment"}
        out = []
        for i, ev in enumerate(issue["pull"]["timeline"]):
            if types and ev["type"] not in types:
                continue
            out.append({"__typename": names[ev["type"]], "id": node_id("TE", repo["id"], issue["number"], i),
                        "createdAt": ev["at"], "actor": self.actor_obj(ev.get("actor")),
                        "pullRequest": lambda info_: self.pr_obj(repo, issue),
                        "url": f"{self.base}/{repo['full_name']}/pull/{issue['number']}"})
        if types and "ISSUE_COMMENT" in types:
            out += [self.comment_obj(repo, c) for c in repo["comments"].values() if c["issue"] == issue["number"]]
            out.sort(key=lambda e: e["createdAt"])
        return out

    def comment_obj(self, repo: dict, c: dict) -> dict:
        issue = repo["issues"][str(c["issue"])]
        kind = "pull" if issue.get("pull") else "issues"
        return {"__typename": "IssueComment", "id": node_id("IC", c["id"]), "databaseId": c["id"], "body": c["body"],
                "bodyText": c["body"], "author": self.actor_obj(c["user"]), "createdAt": c["created_at"],
                "updatedAt": c["updated_at"], "url": f"{self.base}/{repo['full_name']}/{kind}/{c['issue']}#issuecomment-{c['id']}",
                "isMinimized": False, "authorAssociation": "MEMBER"}

    def review_obj(self, repo: dict, r: dict) -> dict:
        return {"__typename": "PullRequestReview", "id": node_id("PRR", r["id"]), "databaseId": r["id"],
                "body": r["body"], "state": r["state"], "author": self.actor_obj(r["user"]),
                "submittedAt": r.get("submitted_at"), "createdAt": r.get("submitted_at") or spec.EPOCH,
                "url": f"{self.base}/{repo['full_name']}/pull/{r['pr']}#pullrequestreview-{r['id']}",
                "authorAssociation": "MEMBER"}

    # -- Projects (v2) -----------------------------------------------------------------------------------
    def _project_level(self, project: dict) -> str:
        """The App permission a project needs: organization projects or (user-owned) repository projects."""
        owner = self.m.store.account(project["owner"], create=False)
        return "organization_projects" if owner and owner["type"] == "Organization" else "repository_projects"

    def _project_ok(self, project: dict, level: str = "read") -> bool:
        if self.actor.kind == "user":
            return True
        inst = self.m.store.installation(self.actor.installation_id)
        return inst is not None and inst["account"].lower() == project["owner"].lower() \
            and self.actor.level(self._project_level(project)) >= LEVEL[level]

    def _project_need(self, project: dict, level: str) -> None:
        if self.actor.kind == "user":
            return
        inst = self.m.store.installation(self.actor.installation_id)
        if inst is None or inst["account"].lower() != project["owner"].lower():
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of "
                                        f"'{self.m.project_node_id(project)}'")
        self._need({self._project_level(project): level})

    def _owner(self, login: str, kind: str | None):
        acc = self.m.store.account(login, create=False)
        if acc is None or acc["type"] == "Bot" or (kind and acc["type"] != kind):
            if kind is None:
                return None
            what = "an Organization" if kind == "Organization" else "a User"
            raise GqlError("NOT_FOUND", f"Could not resolve to {what} with the login of '{login}'.")
        return self.owner_obj(acc["login"])

    def owner_obj(self, login: str) -> dict:
        obj = self.actor_obj(login)
        mine = sorted((p for p in self.m.projects().values() if p["owner"].lower() == login.lower()),
                      key=lambda p: p["number"])

        def project_v2(info, number):
            project = self.m.project(login, number)
            if project is None:
                raise GqlError("NOT_FOUND", f"Could not resolve to a ProjectV2 with the number {number}.")
            self._project_need(project, "read")
            return self.project_obj(project)

        def repository(info, name, followRenames=True):
            repo = self.m.find_repo(login, name)
            return self.repo_obj(repo) if self._visible(repo) else None

        obj.update({
            "projectV2": project_v2, "repository": repository,
            "projectsV2": lambda info, query=None, orderBy=None, minPermissionLevel=None, **a: connection(
                info, [self.project_obj(p) for p in mine if self._project_ok(p)
                       and (not query or query.lower() in p["title"].lower())], **a)})
        return obj

    def project_obj(self, project: dict) -> dict:
        m = self.m
        kind = "orgs" if self._project_level(project) == "organization_projects" else "users"
        path = f"/{kind}/{project['owner']}/projects/{project['number']}"
        return {
            "__typename": "ProjectV2", "id": m.project_node_id(project), "databaseId": project["id"],
            "fullDatabaseId": str(project["id"]), "number": project["number"], "title": project["title"],
            "closed": False, "closedAt": None, "public": False, "readme": None, "shortDescription": None,
            "createdAt": project["created_at"], "updatedAt": project["updated_at"], "url": f"{self.base}{path}",
            "resourcePath": path, "creator": lambda info: self.actor_obj(project["creator"]),
            "owner": lambda info: self.owner_obj(project["owner"]),
            "viewerCanUpdate": self._project_ok(project, "write"),
            "field": lambda info, name: (lambda f: self.field_obj(project, f) if f else None)(
                m.project_field(project, name)),
            "fields": lambda info, orderBy=None, **a: connection(
                info, [self.field_obj(project, f) for f in project["fields"]], **a),
            "items": lambda info, orderBy=None, query=None, archivedStates=None, **a: connection(
                info, [self.item_obj(project, i) for i in project["items"]], **a),
        }

    def field_obj(self, project: dict, field: dict) -> dict:
        single = field["type"] == "SINGLE_SELECT"
        obj = {"__typename": "ProjectV2SingleSelectField" if single else "ProjectV2Field",
               "id": self.m.field_node_id(project, field), "databaseId": field["id"], "name": field["name"],
               "dataType": field["type"], "isIssueField": False, "issueField": None,
               "createdAt": field["created_at"], "updatedAt": field["created_at"],
               "project": lambda info: self.project_obj(project)}
        if single:
            obj["options"] = lambda info, names=None: [
                {"id": o["id"], "name": o["name"], "nameHTML": o["name"], "color": o["color"],
                 "description": o["description"], "descriptionHTML": o["description"]}
                for o in field["options"] if names is None or o["name"] in names]
        return obj

    def _content_visible(self, repo: dict, issue: dict) -> bool:
        if not self._visible(repo):
            return False
        return self.actor.level("pull_requests" if issue.get("pull") else "issues") >= LEVEL["read"]

    def item_obj(self, project: dict, item: dict) -> dict:
        m = self.m
        content = m.item_content(item)
        kind = item["type"]
        if content is not None and not self._content_visible(*content):
            kind, content = "REDACTED", None
        if content is not None:
            repo, issue = content
            content_obj = self.pr_obj(repo, issue) if issue.get("pull") else self.issue_obj(repo, issue)
        elif kind == "DRAFT_ISSUE":
            content_obj = {"__typename": "DraftIssue", "id": node_id("DI", item["draft_id"]), "title": item["title"],
                           "body": item["body"], "bodyText": item["body"], "createdAt": item["created_at"],
                           "updatedAt": item["updated_at"], "creator": self.actor_obj(item["creator"]),
                           "assignees": lambda info, **a: connection(info, [], **a)}
        else:
            content_obj = None
        title = content[1]["title"] if content else item.get("title", "")

        def value(field: dict):
            if field["type"] == "TITLE":
                raw = title
            else:
                raw = item["values"].get(str(field["id"]))
            return self.value_obj(project, item, field, raw)

        return {
            "__typename": "ProjectV2Item", "id": m.item_node_id(project, item), "databaseId": item["id"],
            "fullDatabaseId": str(item["id"]), "type": kind, "isArchived": item["archived_at"] is not None,
            "createdAt": item["created_at"], "updatedAt": item["updated_at"],
            "creator": lambda info: self.actor_obj(item["creator"]), "project": lambda info: self.project_obj(project),
            "content": content_obj,
            "fieldValueByName": lambda info, name: (lambda f: value(f) if f else None)(m.project_field(project, name)),
            "fieldValues": lambda info, orderBy=None, **a: connection(
                info, [v for v in (value(f) for f in project["fields"]) if v is not None], **a),
        }

    def value_obj(self, project: dict, item: dict, field: dict, raw) -> dict | None:
        if raw is None:
            return None
        base = {"id": node_id("PVTFV", project["id"], item["id"], field["id"]), "databaseId": field["id"],
                "createdAt": item["created_at"], "updatedAt": item["updated_at"],
                "creator": lambda info: self.actor_obj(item["creator"]),
                "field": lambda info: self.field_obj(project, field), "item": lambda info: self.item_obj(project, item)}
        if field["type"] == "SINGLE_SELECT":
            o = next((o for o in field["options"] if o["id"] == raw), None)
            if o is None:
                return None
            return {**base, "__typename": "ProjectV2ItemFieldSingleSelectValue", "name": o["name"],
                    "nameHTML": o["name"], "optionId": o["id"], "color": o["color"], "description": o["description"],
                    "descriptionHTML": o["description"]}
        if field["type"] == "NUMBER":
            return {**base, "__typename": "ProjectV2ItemFieldNumberValue", "number": float(raw)}
        return {**base, "__typename": "ProjectV2ItemFieldTextValue", "text": str(raw)}

    def _items_of(self, repo: dict, issue: dict) -> list[dict]:
        return [self.item_obj(p, i) for p in self.m.projects().values() if self._project_ok(p)
                for i in p["items"] if i["type"] != "DRAFT_ISSUE" and i["repo_id"] == repo["id"]
                and i["number"] == issue["number"]]

    def _project_input(self, input: dict) -> dict:
        _, parts = self._node(input["projectId"], "PVT")
        project = self.m.project_by_id(int(parts[0]))
        if project is None:
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{input['projectId']}'")
        self._project_need(project, "write")
        return project

    def _item_input(self, project: dict, value: str) -> dict:
        _, parts = self._node(value, "PVTI")
        item = self.m.project_item(project, int(parts[1])) if int(parts[0]) == project["id"] else None
        if item is None:
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{value}'")
        return item

    def update_item_field_value(self, info, input: dict):
        project = self._project_input(input)
        item = self._item_input(project, input["itemId"])
        _, parts = self._node(input["fieldId"], "PVTF", "PVTSSF")
        field = next((f for f in project["fields"] if f["id"] == int(parts[1])), None) \
            if int(parts[0]) == project["id"] else None
        if field is None:
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{input['fieldId']}'")
        given = {k: v for k, v in input["value"].items() if v is not None}
        if len(given) != 1:
            raise GqlError("UNPROCESSABLE", "Exactly one value must be given")
        (key, value), = given.items()
        wanted = {"SINGLE_SELECT": "singleSelectOptionId", "NUMBER": "number", "TEXT": "text"}.get(field["type"])
        if key != wanted:
            raise GqlError("UNPROCESSABLE", f"The field of type {field['type']} does not accept a {key} value")
        try:
            self.m.set_item_value(project, item, field, value, self.actor.login)
        except ValueError as exc:
            raise GqlError("UNPROCESSABLE", str(exc)) from None
        return {"clientMutationId": input.get("clientMutationId"), "projectV2Item": self.item_obj(project, item)}

    def add_project_item(self, info, input: dict):
        project = self._project_input(input)
        repo, issue = self._labelable(input["contentId"])
        if not self._content_visible(repo, issue):
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{input['contentId']}'")
        item = self.m.add_item(project, self.actor.login, repo=repo, issue=issue)
        return {"clientMutationId": input.get("clientMutationId"), "item": self.item_obj(project, item)}

    def delete_project_item(self, info, input: dict):
        project = self._project_input(input)
        item = self._item_input(project, input["itemId"])
        self.m.delete_project_item(project, item, self.actor.login)
        return {"clientMutationId": input.get("clientMutationId"), "deletedItemId": input["itemId"]}

    # -- mutations -------------------------------------------------------------------------------------
    def _raise(self, exc: SpecError):
        message = exc.body.get("message", "")
        errors = exc.body.get("errors") or []
        detail = "; ".join(e.get("message", "") for e in errors if e.get("message"))
        raise GqlError("UNPROCESSABLE", f"{message} ({detail})" if detail else message) from None

    def create_pull_request(self, info, input: dict):
        self._need({"pull_requests": "write"})
        _, parts = self._node(input["repositoryId"], "R")
        repo = self.m.repo_by_id(int(parts[0]))
        if not self._visible(repo):
            raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{input['repositoryId']}'")
        try:
            issue = self.m.open_pr(repo, self.actor.login, input["headRefName"], input["baseRefName"], input["title"],
                                   input.get("body"), bool(input.get("draft")))
        except SpecError as exc:
            self._raise(exc)
        return {"clientMutationId": input.get("clientMutationId"), "pullRequest": self.pr_obj(repo, issue)}

    def _draft(self, input: dict, draft: bool):
        self._need({"pull_requests": "write"})
        repo, issue = self._pr_by_id(input["pullRequestId"])
        self.m.set_draft(repo, issue, draft, self.actor.login)
        return {"clientMutationId": input.get("clientMutationId"), "pullRequest": self.pr_obj(repo, issue)}

    def _state(self, input: dict, state: str):
        self._need({"pull_requests": "write"})
        repo, issue = self._pr_by_id(input["pullRequestId"])
        try:
            self.m.update_pr(repo, issue, self.actor.login, state=state)
        except SpecError as exc:
            self._raise(exc)
        return {"clientMutationId": input.get("clientMutationId"), "pullRequest": self.pr_obj(repo, issue)}

    def update_pull_request(self, info, input: dict):
        self._need({"pull_requests": "write"})
        repo, issue = self._pr_by_id(input["pullRequestId"])
        state = input.get("state")
        try:
            self.m.update_pr(repo, issue, self.actor.login, title=input.get("title"), body=input.get("body"),
                             base=input.get("baseRefName"), state=state.lower() if state else None)
        except SpecError as exc:
            self._raise(exc)
        return {"clientMutationId": input.get("clientMutationId"), "pullRequest": self.pr_obj(repo, issue)}

    def merge_pull_request(self, info, input: dict):
        self._need({"pull_requests": "write", "contents": "write"})
        repo, issue = self._pr_by_id(input["pullRequestId"])
        method = (input.get("mergeMethod") or "MERGE").lower()
        try:
            self.m.merge_pr(repo, issue, self.actor.login, method, input.get("expectedHeadOid"),
                            input.get("commitHeadline"), input.get("commitBody"))
        except SpecError as exc:
            self._raise(exc)
        return {"clientMutationId": input.get("clientMutationId"), "pullRequest": self.pr_obj(repo, issue),
                "actor": self.actor_obj(self.actor.login)}

    def add_comment(self, info, input: dict):
        self._need({"issues": "write"}, {"pull_requests": "write"})
        repo, issue = self._labelable(input["subjectId"])
        c = self.m.add_comment(repo, issue, self.actor.login, input["body"])
        node = self.comment_obj(repo, c)
        subject = self.pr_obj(repo, issue) if issue.get("pull") else self.issue_obj(repo, issue)
        return {"clientMutationId": input.get("clientMutationId"), "subject": subject,
                "commentEdge": {"cursor": _cursor(0), "node": node}, "timelineEdge": {"cursor": _cursor(0), "node": node}}

    def update_issue_comment(self, info, input: dict):
        self._need({"issues": "write"}, {"pull_requests": "write"})
        _, parts = self._node(input["id"], "IC")
        for repo in self.m.store.data["repos"].values():
            c = repo["comments"].get(parts[0])
            if c is not None and self._visible(repo):
                self.m.edit_comment(repo, c, input["body"], self.actor.login)
                return {"clientMutationId": input.get("clientMutationId"), "issueComment": self.comment_obj(repo, c)}
        raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{input['id']}'")

    def _labels(self, input: dict, add: bool):
        self._need({"issues": "write"}, {"pull_requests": "write"})
        repo, issue = self._labelable(input["labelableId"])
        names = []
        for lid in input["labelIds"]:
            _, parts = self._node(lid, "LA")
            lab = next((x for x in repo["labels"].values() if str(x["id"]) == parts[-1]), None)
            if lab is None:
                raise GqlError("NOT_FOUND", f"Could not resolve to a node with the global id of '{lid}'")
            names.append(lab["name"])
        if add:
            self.m.add_labels(repo, issue, names, self.actor.login)
        else:
            for name in names:
                self.m.remove_label(repo, issue, name, self.actor.login)
        target = self.pr_obj(repo, issue) if issue.get("pull") else self.issue_obj(repo, issue)
        return {"clientMutationId": input.get("clientMutationId"), "labelable": target}


def execute(mock, actor: Actor, body: Any, base: str) -> dict:
    """One GraphQL HTTP request body → GitHub's response document."""
    if not isinstance(body, dict) or not isinstance(body.get("query"), str):
        return {"errors": [{"message": "A query attribute must be specified and must be a string."}]}
    variables = body.get("variables") or {}
    if not isinstance(variables, dict):
        return {"errors": [{"message": "Variables are invalid JSON."}]}
    graph = Graph(mock, actor, base)
    with mock.lock:
        result = graphql_sync(spec.graphql_schema(), body["query"], root_value=graph.root(),
                              variable_values=variables, operation_name=body.get("operationName"))
    out: dict = {}
    if result.errors:
        out["errors"] = [_error(e) for e in result.errors]
    if result.data is not None or not result.errors or any(e.path for e in result.errors):
        out["data"] = result.data
    return out


def _error(e: GraphQLError) -> dict:
    d = e.formatted
    original = e.original_error
    if isinstance(original, GqlError):
        d = {"type": original.kind, **d}
    elif original is not None:
        d = {"type": "INTERNAL", **d, "message": f"{type(original).__name__}: {original}"}
    d.pop("extensions", None)
    return d
