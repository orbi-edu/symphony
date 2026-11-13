"""The mock's behavior, shared by REST, GraphQL, git and the control API: pull requests with real
git semantics (mergeability by trying the merge, merges that move the base ref), comments,
labels and reviews, and what every push sets off (webhooks, `synchronize`, CI)."""

from __future__ import annotations

import collections
import threading

from . import gitrepo, spec
from .ci import CI
from .hooks import Hooks
from .issues import IssuesMixin
from .objects import Render
from .spec import SpecError
from .state import Store, iso


def validation_failed(resource: str, message: str, docs: str = spec.DOCS, field: str | None = None,
                      code: str = "custom") -> SpecError:
    err = {"resource": resource, "code": code, "message": message}
    if field:
        err["field"] = field
    return SpecError(422, "Validation Failed", docs, errors=[err])


class MockGitHub(IssuesMixin):
    def __init__(self, state_dir: str, *, public_url: str, webhook_url: str | None = None,
                 webhook_secret: str | None = None, app_id: int = 1, app_slug: str = "symphony",
                 check_responses: bool = True) -> None:
        self.store = Store(state_dir, app_id, app_slug)
        self.spec = spec.rest()
        self.public_url = public_url.rstrip("/")
        self.check_responses = check_responses
        if webhook_url is not None or webhook_secret is not None:
            self.store.data["webhook"] = {"url": webhook_url, "secret": webhook_secret}
            self.store.save()
        self._pr_cache: dict[tuple, dict] = {}
        self.requests: collections.deque = collections.deque(maxlen=2000)
        self.violations: list[dict] = []
        self.served: set[tuple[str, str, int]] = set()
        self.hooks = Hooks(self)
        self.ci = CI(self)
        self._closed = threading.Event()

    @property
    def lock(self):
        return self.store.lock

    def render(self, base: str | None = None, api: str | None = None) -> Render:
        return Render(self, base or self.public_url, api)

    def close(self) -> None:
        self.ci.close()
        self.hooks.close()

    # -- repositories --------------------------------------------------------------------
    def create_repo(self, owner: str, name: str, *, seed: str | None = None, default_branch: str = "main",
                    private: bool = True, description: str = "", ci: dict | None = None) -> dict:
        with self.lock:
            if self.store.repo(owner, name):
                raise validation_failed("Repository", "name already exists on this account", field="name")
            repo = self.store.create_repo(owner, name, default_branch, private, description)
            path = self.store.repo_path(repo)
            gitrepo.init_bare(path, default_branch)
            gitrepo.seed(path, default_branch, seed, name)
            if ci:
                repo["ci"].update(ci)
            self.store.save()
            return repo

    def find_repo(self, owner: str, name: str) -> dict | None:
        return self.store.repo(owner, name.removesuffix(".git"))

    def repo_by_id(self, repo_id: int) -> dict | None:
        return next((r for r in self.store.data["repos"].values() if r["id"] == int(repo_id)), None)

    def path(self, repo: dict) -> str:
        return self.store.repo_path(repo)

    # -- pull requests -----------------------------------------------------------------------
    def pr_info(self, repo: dict, issue: dict) -> dict:
        """Head, base and the merge facts GitHub computes: mergeability (by trying the merge),
        commit and line counts, and the mergeable state."""
        pr = issue["pull"]
        path = self.path(repo)
        if issue["state"] == "open":
            head = gitrepo.branch_sha(path, pr["head_ref"]) or pr["head_sha"]
            base = gitrepo.branch_sha(path, pr["base_ref"]) or pr["base_sha"]
            pr["head_sha"], pr["base_sha"] = head, base
        else:
            head, base = pr["head_sha"], pr["base_sha"]
        key = (repo["id"], base, head)
        facts = self._pr_cache.get(key)
        if facts is None:
            mb = gitrepo.merge_base(path, base, head) or base
            stat = gitrepo.out(path, "diff", "--numstat", mb, head)
            adds = dels = files = 0
            for line in stat.splitlines():
                a, d, _ = (line.split("\t") + ["", ""])[:3]
                adds += int(a) if a.isdigit() else 0
                dels += int(d) if d.isdigit() else 0
                files += 1
            facts = {"mergeable": gitrepo.mergeable(path, base, head),
                     "commits": len(gitrepo.range_commits(path, base, head)),
                     "additions": adds, "deletions": dels, "changed_files": files}
            self._pr_cache[key] = facts
        out = {"head_sha": head, "base_sha": base, **facts}
        if issue["state"] != "open":
            out["mergeable"] = None
            out["mergeable_state"] = "unknown"
            return out
        if pr["draft"]:
            state = "draft"
        elif not facts["mergeable"]:
            state = "dirty"
        elif self.required_failing(repo, pr["base_ref"], head):
            state = "blocked"
        elif any(c.get("conclusion") in ("failure", "timed_out", "action_required")
                 for c in self.ci.latest_checks(repo, head)):
            state = "unstable"
        else:
            state = "clean"
        out["mergeable_state"] = state
        return out

    def required_failing(self, repo: dict, branch: str, sha: str) -> str | None:
        """The message GitHub gives when branch protection blocks a merge of `sha`, or None."""
        rule = repo["protection"].get(branch)
        if not rule:
            return None
        checks = {c["name"]: c for c in self.ci.latest_checks(repo, sha)}
        statuses = {s["context"]: s for s in self.ci.latest_statuses(repo, sha)}
        for name in (rule.get("required_status_checks") or {}).get("contexts") or []:
            check = checks.get(name)
            status = statuses.get(name)
            if check is None and status is None:
                return f"Required status check \"{name}\" is expected."
            ok = (check is not None and check["status"] == "completed"
                  and check.get("conclusion") in ("success", "neutral", "skipped")) or \
                 (status is not None and status["state"] == "success")
            if not ok:
                pending = (check is not None and check["status"] != "completed") or \
                          (status is not None and status["state"] == "pending")
                return f"Required status check \"{name}\" is {'expected' if pending else 'failing'}."
        return None

    def pulls(self, repo: dict) -> list[dict]:
        return [i for i in repo["issues"].values() if i.get("pull")]

    def open_pr(self, repo: dict, user: str, head: str, base: str, title: str, body: str | None,
                draft: bool, docs: str = spec.DOCS) -> dict:
        with self.lock:
            path = self.path(repo)
            if ":" in head:
                owner, head = head.split(":", 1)
                if owner.lower() != repo["owner"].lower():
                    raise validation_failed("PullRequest", "head repository forks are not modeled", docs, "head",
                                            "invalid")
            head_sha = gitrepo.branch_sha(path, head)
            base_sha = gitrepo.branch_sha(path, base)
            if head_sha is None:
                raise validation_failed("PullRequest", "", docs, "head", "invalid")
            if base_sha is None:
                raise validation_failed("PullRequest", "", docs, "base", "invalid")
            for other in self.pulls(repo):
                if other["state"] == "open" and other["pull"]["head_ref"] == head and other["pull"]["base_ref"] == base:
                    raise validation_failed("PullRequest", f"A pull request already exists for {repo['owner']}:{head}.",
                                            docs)
            if not gitrepo.range_commits(path, base_sha, head_sha):
                raise validation_failed("PullRequest", f"No commits between {base} and {head}", docs)
            issue = self._new_issue(repo, user, title, body)
            issue["pull"] = {"head_ref": head, "base_ref": base, "head_sha": head_sha, "base_sha": base_sha,
                             "draft": bool(draft), "merged": False, "merged_at": None, "merged_by": None,
                             "merge_commit_sha": None, "timeline": []}
            self.store.save()
            self._pr_event(repo, issue, "opened", user)
            self._pr_ci(repo, issue, user)
            return issue

    def _new_issue(self, repo: dict, user: str, title: str, body: str | None) -> dict:
        self.store.account(user)
        n = repo["next_number"]
        repo["next_number"] += 1
        now = iso()
        issue = {"id": self.store.next_id(), "number": n, "title": title, "body": body, "user": user,
                 "state": "open", "state_reason": None, "labels": [], "created_at": now, "updated_at": now,
                 "closed_at": None, "closed_by": None, "pull": None, "parent": None, "sub_issues": [],
                 "blocked_by": []}
        repo["issues"][str(n)] = issue
        return issue

    def issue(self, repo: dict, number: int) -> dict | None:
        return repo["issues"].get(str(number))

    def _pr_event(self, repo: dict, issue: dict, action: str, sender: str, **extra) -> None:
        r = self.render()
        self.hooks.emit("pull_request", action, repo,
                        {"number": issue["number"], "pull_request": r.pull(repo, issue), **extra}, sender)

    def _pr_ci(self, repo: dict, issue: dict, actor: str) -> None:
        info = self.pr_info(repo, issue)
        self.ci.trigger(repo, info["head_sha"], issue["pull"]["head_ref"], "pull_request", [issue["number"]],
                        actor, issue["title"], labels=issue["labels"], draft=issue["pull"]["draft"])

    def touch(self, issue: dict) -> None:
        issue["updated_at"] = iso()

    def update_pr(self, repo: dict, issue: dict, actor: str, *, title=None, body=None, state=None, base=None,
                  docs: str = spec.DOCS) -> None:
        with self.lock:
            pr = issue["pull"]
            if base is not None and base != pr["base_ref"]:
                if gitrepo.branch_sha(self.path(repo), base) is None:
                    raise validation_failed("PullRequest", "", docs, "base", "invalid")
                changes = {"base": {"ref": {"from": pr["base_ref"]}}}
                pr["base_ref"] = base
                self.touch(issue)
                self._pr_event(repo, issue, "edited", actor, changes=changes)
            if title is not None or body is not None:
                changes = {}
                if title is not None and title != issue["title"]:
                    changes["title"] = {"from": issue["title"]}
                    issue["title"] = title
                if body is not None and body != issue.get("body"):
                    changes["body"] = {"from": issue.get("body") or ""}
                    issue["body"] = body
                if changes:
                    self.touch(issue)
                    self._pr_event(repo, issue, "edited", actor, changes=changes)
            if state is not None and state != issue["state"] and not pr["merged"]:
                if state == "closed":
                    self.pr_info(repo, issue)
                    issue["state"], issue["closed_at"] = "closed", iso()
                    pr["timeline"].append({"type": "CLOSED_EVENT", "at": issue["closed_at"], "actor": actor})
                    self.touch(issue)
                    self._pr_event(repo, issue, "closed", actor)
                else:
                    if gitrepo.branch_sha(self.path(repo), pr["head_ref"]) is None:
                        raise validation_failed("PullRequest", "state cannot be changed. The head branch was deleted.",
                                                docs, "state")
                    issue["state"], issue["closed_at"] = "open", None
                    pr["timeline"].append({"type": "REOPENED_EVENT", "at": iso(), "actor": actor})
                    self.touch(issue)
                    self._pr_event(repo, issue, "reopened", actor)
                    self._pr_ci(repo, issue, actor)
            self.store.save()

    def set_draft(self, repo: dict, issue: dict, draft: bool, actor: str) -> None:
        with self.lock:
            pr = issue["pull"]
            if issue["state"] != "open" or pr["draft"] == draft:
                return
            pr["draft"] = draft
            now = iso()
            pr["timeline"].append({"type": "CONVERT_TO_DRAFT_EVENT" if draft else "READY_FOR_REVIEW_EVENT",
                                   "at": now, "actor": actor})
            self.touch(issue)
            self.store.save()
            self._pr_event(repo, issue, "converted_to_draft" if draft else "ready_for_review", actor)
            if not draft:
                self._pr_ci(repo, issue, actor)

    def merge_pr(self, repo: dict, issue: dict, actor: str, method: str, sha: str | None,
                 title: str | None, message: str | None, docs: str = spec.DOCS) -> str:
        """Merges like `PUT /pulls/{n}/merge`; raises GitHub's 405/409 when it cannot."""
        with self.lock:
            pr = issue["pull"]
            path = self.path(repo)
            if issue["state"] != "open":
                raise SpecError(405, "Pull Request is not mergeable", docs)
            info = self.pr_info(repo, issue)
            if pr["draft"]:
                raise SpecError(405, "Pull Request is still a draft", docs)
            if sha and sha != info["head_sha"]:
                raise SpecError(409, "Head branch was modified. Review and try the merge again.", docs)
            blocked = self.required_failing(repo, pr["base_ref"], info["head_sha"])
            if blocked:
                raise SpecError(405, blocked, docs)
            n = issue["number"]
            acc = self.store.account(actor)
            author = (acc["name"] or actor, f"{acc['id']}+{actor}@users.noreply.github.com")
            if method == "merge":
                head_title = title or f"Merge pull request #{n} from {repo['owner']}:{pr['head_ref']}"
                body = message if message is not None else issue["title"]
            elif method == "squash":
                head_title = title or f"{issue['title']} (#{n})"
                body = message if message is not None else "\n\n".join(
                    "* " + gitrepo.commit_info(path, c).message
                    for c in gitrepo.range_commits(path, info["base_sha"], info["head_sha"]))
            else:
                head_title, body = "", ""
            try:
                new = gitrepo.merge(path, info["base_sha"], info["head_sha"], method, head_title, body, author)
            except gitrepo.Conflict:
                raise SpecError(405, "This branch can't be rebased" if method == "rebase"
                                else "Pull Request is not mergeable", docs) from None
            before = info["base_sha"]
            try:
                gitrepo.update_ref(path, f"refs/heads/{pr['base_ref']}", new, before)
            except gitrepo.GitFailure:
                raise SpecError(409, "Base branch was modified. Review and try the merge again.", docs) from None
            now = iso()
            issue["state"], issue["closed_at"] = "closed", now
            pr.update(merged=True, merged_at=now, merged_by=actor, merge_commit_sha=new, base_sha=before)
            pr["timeline"].append({"type": "MERGED_EVENT", "at": now, "actor": actor})
            self.touch(issue)
            self.store.save()
            self._pr_event(repo, issue, "closed", actor)
            self.after_push(repo, [(f"refs/heads/{pr['base_ref']}", before, new)], actor)
            return new

    # -- labels and conversation ----------------------------------------------------------------
    def add_labels(self, repo: dict, issue: dict, names: list[str], actor: str) -> None:
        with self.lock:
            for name in names:
                lab = self.store.label(repo, name, create=True)
                if lab["name"] not in issue["labels"]:
                    issue["labels"].append(lab["name"])
                    self.touch(issue)
                    label = self.render().label(repo, lab)
                    if issue.get("pull"):
                        self._pr_event(repo, issue, "labeled", actor, label=label)
                    else:
                        self._issue_hook(repo, issue, "labeled", actor, label=label)
            self.store.save()

    def remove_label(self, repo: dict, issue: dict, name: str, actor: str) -> bool:
        with self.lock:
            match = next((x for x in issue["labels"] if x.lower() == name.lower()), None)
            if match is None:
                return False
            issue["labels"].remove(match)
            self.touch(issue)
            self.store.save()
            label = self.render().label(repo, self.store.label(repo, match))
            if issue.get("pull"):
                self._pr_event(repo, issue, "unlabeled", actor, label=label)
            else:
                self._issue_hook(repo, issue, "unlabeled", actor, label=label)
            return True

    def add_comment(self, repo: dict, issue: dict, user: str, body: str) -> dict:
        with self.lock:
            self.store.account(user)
            now = iso()
            c = {"id": self.store.next_id(), "issue": issue["number"], "body": body, "user": user,
                 "created_at": now, "updated_at": now}
            repo["comments"][str(c["id"])] = c
            self.touch(issue)
            self.store.save()
            r = self.render()
            self.hooks.emit("issue_comment", "created", repo,
                            {"issue": r.issue(repo, issue), "comment": r.comment(repo, c)}, user)
            return c

    def edit_comment(self, repo: dict, c: dict, body: str, actor: str) -> None:
        with self.lock:
            old = c["body"]
            c["body"], c["updated_at"] = body, iso()
            self.store.save()
            r = self.render()
            issue = repo["issues"][str(c["issue"])]
            self.hooks.emit("issue_comment", "edited", repo,
                            {"issue": r.issue(repo, issue), "comment": r.comment(repo, c),
                             "changes": {"body": {"from": old}}}, actor)

    def add_review_comment(self, repo: dict, issue: dict, user: str, body: str, *, commit_id: str | None,
                           path: str | None, line: int | None, side: str = "RIGHT",
                           start_line: int | None = None, in_reply_to: int | None = None,
                           subject_type: str = "line", review_id: int | None = None,
                           docs: str = spec.DOCS) -> dict:
        with self.lock:
            info = self.pr_info(repo, issue)
            if in_reply_to is not None:
                parent = repo["review_comments"].get(str(in_reply_to))
                if parent is None or parent["pr"] != issue["number"]:
                    raise SpecError(404, "Not Found", docs)
                commit_id, path, line, side = parent["commit_id"], parent["path"], parent.get("line"), parent["side"]
                hunk = parent["diff_hunk"]
            else:
                if gitrepo.object_type(self.path(repo), commit_id or "") != "commit":
                    raise validation_failed("PullRequestReviewComment", "commit_id is not part of the pull request",
                                            docs, "commit_id")
                changed = {f.filename: f for f in gitrepo.files(self.path(repo), info["base_sha"], commit_id)}
                if path not in changed:
                    raise validation_failed("PullRequestReviewComment", "Path could not be resolved", docs, "path")
                hunk = _hunk(changed[path].patch, line if subject_type == "line" else None, side)
                if hunk is None:
                    raise validation_failed("PullRequestReviewComment", "Line could not be resolved", docs, "line")
            self.store.account(user)
            now = iso()
            c = {"id": self.store.next_id(), "pr": issue["number"], "body": body, "user": user,
                 "commit_id": commit_id, "path": path, "line": line, "side": side, "start_line": start_line,
                 "start_side": side if start_line else None, "in_reply_to_id": in_reply_to, "diff_hunk": hunk,
                 "subject_type": subject_type, "created_at": now, "updated_at": now, "review_id": review_id}
            if review_id is None:
                review = self._review_record(repo, issue, user, "", "COMMENTED", commit_id)
                c["review_id"] = review["id"]
            repo["review_comments"][str(c["id"])] = c
            self.touch(issue)
            self.store.save()
            r = self.render()
            self.hooks.emit("pull_request_review_comment", "created", repo,
                            {"comment": r.review_comment(repo, c), "pull_request": r.pull(repo, issue, full=False)}, user)
            return c

    def _review_record(self, repo: dict, issue: dict, user: str, body: str, state: str, commit_id: str) -> dict:
        r = {"id": self.store.next_id(), "pr": issue["number"], "user": user, "body": body, "state": state,
             "commit_id": commit_id, "submitted_at": iso() if state != "PENDING" else None}
        repo["reviews"][str(r["id"])] = r
        return r

    def add_review(self, repo: dict, issue: dict, user: str, body: str | None, event: str | None,
                   commit_id: str | None, comments: list[dict], docs: str = spec.DOCS) -> dict:
        with self.lock:
            info = self.pr_info(repo, issue)
            state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED",
                     None: "PENDING"}[event]
            if state in ("CHANGES_REQUESTED", "COMMENTED") and not (body or comments):
                raise validation_failed("PullRequestReview", f"Unprocessable Entity: body is required for {event}",
                                        docs)
            if user.lower() == issue["user"].lower() and state in ("APPROVED", "CHANGES_REQUESTED"):
                verb = "approve" if state == "APPROVED" else "request changes on"
                raise validation_failed("PullRequestReview", f"Can not {verb} your own pull request", docs)
            self.store.account(user)
            review = self._review_record(repo, issue, user, body or "", state, commit_id or info["head_sha"])
            for c in comments:
                self.add_review_comment(repo, issue, user, c["body"], commit_id=review["commit_id"], path=c["path"],
                                        line=c.get("line") or c.get("position"), side=c.get("side", "RIGHT"),
                                        start_line=c.get("start_line"), review_id=review["id"], docs=docs)
            self.touch(issue)
            self.store.save()
            if state != "PENDING":
                r = self.render()
                self.hooks.emit("pull_request_review", "submitted", repo,
                                {"review": r.review(repo, review), "pull_request": r.pull(repo, issue, full=False)}, user)
            return review

    # -- refs and files -------------------------------------------------------------------------
    def write_file(self, repo: dict, branch: str, file: str, content: bytes | None, message: str, sha: str | None,
                   actor: str, author: dict | None = None, docs: str = spec.DOCS) -> tuple[str, str | None, bool]:
        with self.lock:
            path = self.path(repo)
            parent = gitrepo.branch_sha(path, branch)
            if parent is None:
                raise SpecError(404, f"Branch {branch} not found", docs)
            current = gitrepo.blob_sha(path, parent, file)
            if current is not None and gitrepo.object_type(path, current) != "blob":
                raise SpecError(422, f"Invalid request.\n\n{file} is a directory.", docs)
            if content is not None and current is not None and sha is None:
                raise SpecError(422, "Invalid request.\n\n\"sha\" wasn't supplied.", docs)
            if sha is not None and current != sha:
                raise SpecError(409, f"{file} does not match {sha}", docs)
            if content is None and current is None:
                raise SpecError(404, "Not Found", docs)
            acc = self.store.account(actor)
            who = (author or {}).get("name") or acc["name"], (author or {}).get("email") or \
                f"{acc['id']}+{actor}@users.noreply.github.com"
            new = gitrepo.write_file(path, parent, file, content, message, who, gitrepo.GITHUB)
            gitrepo.update_ref(path, f"refs/heads/{branch}", new, parent)
            self.after_push(repo, [(f"refs/heads/{branch}", parent, new)], actor)
            return new, gitrepo.blob_sha(path, new, file) if content is not None else None, current is None

    def after_push(self, repo: dict, updates: list[tuple[str, str, str]], pusher: str) -> None:
        """Everything a push sets off: `push`, `synchronize` on the pull requests whose head
        moved, and CI (pushes to the default branch; pull request heads)."""
        r = self.render()
        path = self.path(repo)
        with self.lock:
            repo["pushed_at"] = iso()
            self.store.save()
            for ref, before, after in updates:
                deleted = after == gitrepo.ZERO
                created = before == gitrepo.ZERO
                commits = []
                if not deleted:
                    rng = gitrepo.range_commits(path, before, after) if not created else [after]
                    for sha in rng[-20:]:
                        hc = r.head_commit(repo, sha)
                        commits.append({**hc, "url": f"{r.base}/{repo['full_name']}/commit/{sha}", "distinct": True,
                                        "added": [], "removed": [], "modified": []})
                forced = not created and not deleted and not gitrepo.is_ancestor(path, before, after)
                acc = self.store.account(pusher)
                self.hooks.emit("push", None, repo, {
                    "ref": ref, "before": before, "after": after, "created": created, "deleted": deleted,
                    "forced": forced, "base_ref": None,
                    "compare": f"{r.base}/{repo['full_name']}/compare/{before[:12]}...{after[:12]}",
                    "commits": commits, "head_commit": commits[-1] if commits else None,
                    "pusher": {"name": pusher, "email": f"{acc['id']}+{pusher}@users.noreply.github.com"},
                }, pusher)
                if not ref.startswith("refs/heads/") or deleted:
                    continue
                branch = ref.removeprefix("refs/heads/")
                if branch == repo["default_branch"]:
                    self.ci.trigger(repo, after, branch, "push", [], pusher, gitrepo.commit_info(path, after).message
                                    .split("\n", 1)[0])
                for issue in self.pulls(repo):
                    if issue["state"] != "open" or issue["pull"]["head_ref"] != branch:
                        continue
                    issue["pull"]["head_sha"] = after
                    if forced:
                        issue["pull"]["timeline"].append({"type": "HEAD_REF_FORCE_PUSHED_EVENT", "at": iso(),
                                                          "actor": pusher})
                    self.touch(issue)
                    self.store.save()
                    self._pr_event(repo, issue, "synchronize", pusher, before=before, after=after)
                    self._pr_ci(repo, issue, pusher)


def _hunk(patch: str, line: int | None, side: str) -> str | None:
    """The diff hunk up to `line` (GitHub's `diff_hunk`), or None when the diff does not show it."""
    import re

    if line is None:
        return patch.split("\n", 1)[0] if patch else ""
    lines = patch.split("\n")
    start = None
    old = new = 0
    for i, text in enumerate(lines):
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", text)
        if m:
            start, old, new = i, int(m.group(1)), int(m.group(2))
            continue
        if start is None:
            continue
        if text.startswith("-"):
            here, old = old, old + 1
            if side == "LEFT" and here == line:
                return "\n".join(lines[start:i + 1])
        elif text.startswith("+"):
            here, new = new, new + 1
            if side == "RIGHT" and here == line:
                return "\n".join(lines[start:i + 1])
        else:
            if (side == "RIGHT" and new == line) or (side == "LEFT" and old == line):
                return "\n".join(lines[start:i + 1])
            old, new = old + 1, new + 1
    return None
