"""REST objects as GitHub renders them, from the mock's state. Only the fields that carry
meaning are built here; `RestSpec.conform` completes the documented shape on the way out."""

from __future__ import annotations

from . import gitrepo
from .state import node_id


class Render:
    def __init__(self, mock, base: str, api: str | None = None) -> None:
        self.m = mock
        self.store = mock.store
        self.base = base.rstrip("/")
        self.api = (api or base).rstrip("/")

    # -- accounts / app ---------------------------------------------------------------
    def user(self, login: str | None) -> dict | None:
        if not login:
            return None
        acc = self.store.account(login)
        kind = acc["type"]
        prefix = {"Organization": "O", "Bot": "BOT"}.get(kind, "U")
        api_login = login.replace("[bot]", "%5Bbot%5D")
        return {
            "login": acc["login"], "id": acc["id"], "node_id": node_id(prefix, acc["id"]),
            "avatar_url": f"{self.base}/avatars/{acc['id']}", "gravatar_id": "",
            "url": f"{self.api}/users/{api_login}", "html_url": f"{self.base}/{api_login}",
            "type": kind, "site_admin": False, "name": acc.get("name"),
            "repos_url": f"{self.api}/users/{api_login}/repos",
        }

    def app(self) -> dict:
        app = self.store.app
        return {
            "id": app["id"], "slug": app["slug"], "node_id": node_id("A", app["id"]),
            "client_id": app["client_id"], "owner": self.user(app["owner"]), "name": app["name"],
            "description": "Symphony's GitHub App, as served by the certification mock",
            "external_url": f"{self.base}/apps/{app['slug']}", "html_url": f"{self.base}/apps/{app['slug']}",
            "created_at": app["created_at"], "updated_at": app["created_at"],
            "permissions": {"contents": "write", "pull_requests": "write", "checks": "write",
                            "actions": "write", "issues": "write", "metadata": "read", "statuses": "write"},
            "events": ["pull_request", "pull_request_review", "pull_request_review_comment",
                       "issue_comment", "check_suite", "check_run", "workflow_run", "push"],
        }

    def actions_app(self) -> dict:
        return {
            "id": 15368, "slug": "github-actions", "node_id": node_id("A", 15368),
            "owner": self.user("github"), "name": "GitHub Actions", "description": "Automate your workflow",
            "external_url": "https://help.github.com/en/actions", "html_url": f"{self.base}/apps/github-actions",
            "created_at": "2018-07-30T09:30:17Z", "updated_at": "2024-04-10T20:33:16Z",
            "permissions": {"actions": "write", "checks": "write", "contents": "write", "metadata": "read"},
            "events": ["check_run", "check_suite", "push", "pull_request", "workflow_run"],
        }

    def installation(self, inst: dict) -> dict:
        acc = self.store.account(inst["account"])
        return {
            "id": inst["id"], "app_id": self.store.app["id"], "app_slug": self.store.app["slug"],
            "client_id": self.store.app["client_id"],
            "target_id": acc["id"], "target_type": acc["type"] if acc["type"] != "Bot" else "User",
            "account": self.user(inst["account"]), "repository_selection": inst["repository_selection"],
            "access_tokens_url": f"{self.api}/app/installations/{inst['id']}/access_tokens",
            "repositories_url": f"{self.api}/installation/repositories",
            "html_url": f"{self.base}/organizations/{inst['account']}/settings/installations/{inst['id']}",
            "permissions": inst["permissions"], "events": self.app()["events"],
            "created_at": inst["created_at"], "updated_at": inst.get("updated_at", inst["created_at"]),
            "single_file_name": None, "suspended_by": None, "suspended_at": None,
        }

    # -- repositories ---------------------------------------------------------------------
    def repo(self, repo: dict, actor=None) -> dict:
        full = repo["full_name"]
        api = f"{self.api}/repos/{full}"
        out = {
            "id": repo["id"], "node_id": node_id("R", repo["id"]), "name": repo["name"], "full_name": full,
            "owner": self.user(repo["owner"]), "private": repo["private"],
            "visibility": "private" if repo["private"] else "public",
            "html_url": f"{self.base}/{full}", "description": repo.get("description") or None,
            "fork": False, "url": api, "default_branch": repo["default_branch"],
            "clone_url": f"{self.base}/{full}.git", "git_url": f"{self.base}/{full}.git",
            "ssh_url": f"git@mock.invalid:{full}.git", "svn_url": f"{self.base}/{full}",
            "mirror_url": None, "homepage": None, "language": None,
            "created_at": repo["created_at"], "updated_at": repo.get("pushed_at") or repo["created_at"],
            "pushed_at": repo.get("pushed_at"), "archived": False, "disabled": False,
            "has_issues": True, "has_projects": False, "has_wiki": False, "has_pages": False,
            "has_downloads": True, "has_discussions": False, "is_template": False,
            "allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": True,
            "delete_branch_on_merge": False, "topics": [], "license": None,
            "pulls_url": f"{api}/pulls{{/number}}", "issues_url": f"{api}/issues{{/number}}",
            "contents_url": f"{api}/contents/{{+path}}", "labels_url": f"{api}/labels{{/name}}",
            "statuses_url": f"{api}/statuses/{{sha}}", "commits_url": f"{api}/commits{{/sha}}",
            "git_refs_url": f"{api}/git/refs{{/sha}}", "compare_url": f"{api}/compare/{{base}}...{{head}}",
            "merges_url": f"{api}/merges", "hooks_url": f"{api}/hooks",
        }
        if actor is not None:
            out["permissions"] = self.repo_permissions(actor)
        return out

    def repo_permissions(self, actor) -> dict:
        contents = actor.level("contents")
        return {"admin": actor.level("administration") >= 2, "maintain": actor.level("administration") >= 2,
                "push": contents >= 2, "triage": actor.level("issues") >= 2 or actor.level("pull_requests") >= 2,
                "pull": contents >= 1 or actor.kind == "user"}

    def label(self, repo: dict, lab: dict) -> dict:
        return {"id": lab["id"], "node_id": node_id("LA", repo["id"], lab["id"]),
                "url": f"{self.api}/repos/{repo['full_name']}/labels/{lab['name']}", "name": lab["name"],
                # "" rather than null: `pull-request-simple` documents a label description as a string
                "color": lab["color"], "default": lab["default"], "description": lab.get("description") or "",
                "archived_at": None, "archived_by": None}

    def labels(self, repo: dict, names: list[str]) -> list[dict]:
        return [self.label(repo, self.store.label(repo, n, create=True)) for n in names]

    # -- issues and pull requests ----------------------------------------------------------------
    def _issue_base(self, repo: dict, issue: dict) -> dict:
        full = repo["full_name"]
        n = issue["number"]
        kind = "pull" if issue.get("pull") else "issues"
        return {
            "id": issue["id"], "node_id": node_id("PR" if issue.get("pull") else "I", repo["id"], n),
            "url": f"{self.api}/repos/{full}/issues/{n}", "repository_url": f"{self.api}/repos/{full}",
            "labels_url": f"{self.api}/repos/{full}/issues/{n}/labels{{/name}}",
            "comments_url": f"{self.api}/repos/{full}/issues/{n}/comments",
            "events_url": f"{self.api}/repos/{full}/issues/{n}/events",
            "html_url": f"{self.base}/{full}/{kind}/{n}", "number": n, "state": issue["state"],
            "state_reason": issue.get("state_reason"), "title": issue["title"], "body": issue.get("body"),
            "user": self.user(issue["user"]), "labels": self.labels(repo, issue["labels"]),
            "assignee": None, "assignees": [], "milestone": None, "locked": False, "active_lock_reason": None,
            "comments": sum(1 for c in repo["comments"].values() if c["issue"] == n),
            "created_at": issue["created_at"], "updated_at": issue["updated_at"], "closed_at": issue.get("closed_at"),
            "author_association": self.association(repo, issue["user"]),
            "closed_by": self.user(issue.get("closed_by")),
            **self._issue_links(issue),
        }

    def _issue_links(self, issue: dict) -> dict:
        """Sub-issues and dependency summaries and the parent's URL (issues only)."""
        if issue.get("pull"):
            return {}
        m = self.m
        subs = [m.issue_by_id(i) for i in issue.get("sub_issues") or []]
        done = sum(1 for s in subs if s and s[1]["state"] == "closed")
        blocked_by = [m.issue_by_id(i) for i in issue.get("blocked_by") or []]
        blocking = m.blocking_of(issue)
        out = {"sub_issues_summary": {"total": len(subs), "completed": done,
                                      "percent_completed": done * 100 // len(subs) if subs else 0},
               "issue_dependencies_summary": {
                   "blocked_by": sum(1 for b in blocked_by if b and b[1]["state"] == "open"),
                   "blocking": sum(1 for _, b in blocking if b["state"] == "open"),
                   "total_blocked_by": len(blocked_by), "total_blocking": len(blocking)}}
        parent = m.issue_by_id(issue["parent"]) if issue.get("parent") else None
        if parent:
            out["parent_issue_url"] = f"{self.api}/repos/{parent[0]['full_name']}/issues/{parent[1]['number']}"
        return out

    def association(self, repo: dict, login: str) -> str:
        acc = self.store.account(login)
        if acc and acc["type"] == "Bot":
            return "NONE"
        return "MEMBER" if login.lower() != repo["owner"].lower() else "OWNER"

    def issue(self, repo: dict, issue: dict) -> dict:
        out = self._issue_base(repo, issue)
        if issue.get("pull"):
            full, n = repo["full_name"], issue["number"]
            out["pull_request"] = {"url": f"{self.api}/repos/{full}/pulls/{n}",
                                   "html_url": f"{self.base}/{full}/pull/{n}",
                                   "diff_url": f"{self.base}/{full}/pull/{n}.diff",
                                   "patch_url": f"{self.base}/{full}/pull/{n}.patch",
                                   "merged_at": issue["pull"].get("merged_at")}
            out["draft"] = issue["pull"]["draft"]
        return out

    def _branch(self, repo: dict, ref: str, sha: str) -> dict:
        return {"label": f"{repo['owner']}:{ref}", "ref": ref, "sha": sha, "user": self.user(repo["owner"]),
                "repo": self.repo(repo)}

    def pull(self, repo: dict, issue: dict, full: bool = True) -> dict:
        info = self.m.pr_info(repo, issue)
        pr = issue["pull"]
        fn, n = repo["full_name"], issue["number"]
        api = f"{self.api}/repos/{fn}"
        out = {
            "url": f"{api}/pulls/{n}", "id": issue["id"], "node_id": node_id("PR", repo["id"], n),
            "html_url": f"{self.base}/{fn}/pull/{n}", "diff_url": f"{self.base}/{fn}/pull/{n}.diff",
            "patch_url": f"{self.base}/{fn}/pull/{n}.patch", "issue_url": f"{api}/issues/{n}",
            "commits_url": f"{api}/pulls/{n}/commits", "review_comments_url": f"{api}/pulls/{n}/comments",
            "review_comment_url": f"{api}/pulls/comments{{/number}}", "comments_url": f"{api}/issues/{n}/comments",
            "statuses_url": f"{api}/statuses/{info['head_sha']}",
            "number": n, "state": issue["state"], "locked": False, "title": issue["title"],
            "user": self.user(issue["user"]), "body": issue.get("body"), "labels": self.labels(repo, issue["labels"]),
            "milestone": None, "active_lock_reason": None, "created_at": issue["created_at"],
            "updated_at": issue["updated_at"], "closed_at": issue.get("closed_at"), "merged_at": pr.get("merged_at"),
            "merge_commit_sha": pr.get("merge_commit_sha") or info.get("test_merge_sha"),
            "assignee": None, "assignees": [], "requested_reviewers": [], "requested_teams": [],
            "head": self._branch(repo, pr["head_ref"], info["head_sha"]),
            "base": self._branch(repo, pr["base_ref"], info["base_sha"]),
            "_links": {k: {"href": v} for k, v in {
                "self": f"{api}/pulls/{n}", "html": f"{self.base}/{fn}/pull/{n}", "issue": f"{api}/issues/{n}",
                "comments": f"{api}/issues/{n}/comments", "review_comments": f"{api}/pulls/{n}/comments",
                "review_comment": f"{api}/pulls/comments{{/number}}", "commits": f"{api}/pulls/{n}/commits",
                "statuses": f"{api}/statuses/{info['head_sha']}"}.items()},
            "author_association": self.association(repo, issue["user"]), "auto_merge": None,
            "draft": pr["draft"],
        }
        if full:
            out.update({
                "merged": bool(pr.get("merged")), "mergeable": info["mergeable"],
                "rebaseable": info["mergeable"], "mergeable_state": info["mergeable_state"],
                "merged_by": self.user(pr.get("merged_by")),
                "comments": sum(1 for c in repo["comments"].values() if c["issue"] == n),
                "review_comments": sum(1 for c in repo["review_comments"].values() if c["pr"] == n),
                "maintainer_can_modify": False, "commits": info["commits"], "additions": info["additions"],
                "deletions": info["deletions"], "changed_files": info["changed_files"],
            })
        return out

    def comment(self, repo: dict, c: dict) -> dict:
        fn = repo["full_name"]
        issue = repo["issues"][str(c["issue"])]
        kind = "pull" if issue.get("pull") else "issues"
        return {
            "id": c["id"], "node_id": node_id("IC", c["id"]), "url": f"{self.api}/repos/{fn}/issues/comments/{c['id']}",
            "html_url": f"{self.base}/{fn}/{kind}/{c['issue']}#issuecomment-{c['id']}", "body": c["body"],
            "user": self.user(c["user"]), "created_at": c["created_at"], "updated_at": c["updated_at"],
            "issue_url": f"{self.api}/repos/{fn}/issues/{c['issue']}",
            "author_association": self.association(repo, c["user"]),
        }

    def review_comment(self, repo: dict, c: dict) -> dict:
        fn, n = repo["full_name"], c["pr"]
        api = f"{self.api}/repos/{fn}"
        html = f"{self.base}/{fn}/pull/{n}#discussion_r{c['id']}"
        out = {
            "url": f"{api}/pulls/comments/{c['id']}", "id": c["id"], "node_id": node_id("PRRC", c["id"]),
            "pull_request_review_id": c.get("review_id"), "diff_hunk": c["diff_hunk"], "path": c["path"],
            "position": c.get("line"), "original_position": c.get("line"), "commit_id": c["commit_id"],
            "original_commit_id": c["commit_id"], "user": self.user(c["user"]), "body": c["body"],
            "created_at": c["created_at"], "updated_at": c["updated_at"], "html_url": html,
            "pull_request_url": f"{api}/pulls/{n}", "author_association": self.association(repo, c["user"]),
            "_links": {"self": {"href": f"{api}/pulls/comments/{c['id']}"}, "html": {"href": html},
                       "pull_request": {"href": f"{api}/pulls/{n}"}},
            "line": c.get("line"), "original_line": c.get("line"), "side": c.get("side", "RIGHT"),
            "start_line": c.get("start_line"), "original_start_line": c.get("start_line"),
            "start_side": c.get("start_side"), "subject_type": c.get("subject_type", "line"),
        }
        if c.get("in_reply_to_id"):
            out["in_reply_to_id"] = c["in_reply_to_id"]
        return out

    def review(self, repo: dict, r: dict) -> dict:
        fn, n = repo["full_name"], r["pr"]
        html = f"{self.base}/{fn}/pull/{n}#pullrequestreview-{r['id']}"
        return {
            "id": r["id"], "node_id": node_id("PRR", r["id"]), "user": self.user(r["user"]), "body": r["body"],
            "state": r["state"], "html_url": html, "pull_request_url": f"{self.api}/repos/{fn}/pulls/{n}",
            "_links": {"html": {"href": html}, "pull_request": {"href": f"{self.api}/repos/{fn}/pulls/{n}"}},
            "submitted_at": r.get("submitted_at"), "commit_id": r["commit_id"],
            "author_association": self.association(repo, r["user"]),
        }

    # -- git objects ------------------------------------------------------------------------------
    def git_ref(self, repo: dict, ref: str, sha: str) -> dict:
        fn = repo["full_name"]
        kind = gitrepo.object_type(self.store.repo_path(repo), sha) or "commit"
        return {"ref": ref, "node_id": node_id("REF", repo["id"], ref), "url": f"{self.api}/repos/{fn}/git/{ref}",
                "object": {"type": kind, "sha": sha, "url": f"{self.api}/repos/{fn}/git/{kind}s/{sha}"}}

    def commit(self, repo: dict, sha: str) -> dict:
        path = self.store.repo_path(repo)
        info = gitrepo.commit_info(path, sha)
        fn = repo["full_name"]
        return {
            "url": f"{self.api}/repos/{fn}/commits/{sha}", "sha": sha, "node_id": node_id("C", repo["id"], sha),
            "html_url": f"{self.base}/{fn}/commit/{sha}", "comments_url": f"{self.api}/repos/{fn}/commits/{sha}/comments",
            "commit": {
                "url": f"{self.api}/repos/{fn}/git/commits/{sha}",
                "author": {"name": info.author[0], "email": info.author[1], "date": _z(info.author[2])},
                "committer": {"name": info.committer[0], "email": info.committer[1], "date": _z(info.committer[2])},
                "message": info.message, "comment_count": 0,
                "tree": {"sha": info.tree, "url": f"{self.api}/repos/{fn}/git/trees/{info.tree}"},
                "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None,
                                 "verified_at": None},
            },
            "author": None, "committer": None,
            "parents": [{"sha": p, "url": f"{self.api}/repos/{fn}/commits/{p}", "html_url": f"{self.base}/{fn}/commit/{p}"}
                        for p in info.parents],
        }

    def head_commit(self, repo: dict, sha: str) -> dict:
        info = gitrepo.commit_info(self.store.repo_path(repo), sha)
        return {"id": sha, "tree_id": info.tree, "message": info.message, "timestamp": _z(info.committer[2]),
                "author": {"name": info.author[0], "email": info.author[1]},
                "committer": {"name": info.committer[0], "email": info.committer[1]}}

    def diff_entry(self, repo: dict, head: str, f: gitrepo.FileChange) -> dict:
        fn = repo["full_name"]
        out = {"sha": f.sha, "filename": f.filename, "status": f.status, "additions": f.additions,
               "deletions": f.deletions, "changes": f.additions + f.deletions,
               "blob_url": f"{self.base}/{fn}/blob/{head}/{f.filename}",
               "raw_url": f"{self.base}/{fn}/raw/{head}/{f.filename}",
               "contents_url": f"{self.api}/repos/{fn}/contents/{f.filename}?ref={head}"}
        if f.patch:
            out["patch"] = f.patch
        if f.previous_filename:
            out["previous_filename"] = f.previous_filename
        return out

    def content(self, repo: dict, ref: str, mode: str, typ: str, sha: str, path: str, with_content: bool) -> dict:
        import base64

        fn = repo["full_name"]
        name = path.rsplit("/", 1)[-1]
        kind = {"blob": "file", "tree": "dir", "commit": "submodule"}.get(typ, "file")
        if mode == "120000":
            kind = "symlink"
        git_url = f"{self.api}/repos/{fn}/git/{'trees' if typ == 'tree' else 'blobs'}/{sha}"
        html = f"{self.base}/{fn}/{'tree' if typ == 'tree' else 'blob'}/{ref}/{path}"
        out = {"type": kind, "name": name, "path": path, "sha": sha, "size": 0,
               "url": f"{self.api}/repos/{fn}/contents/{path}?ref={ref}", "git_url": git_url, "html_url": html,
               "download_url": f"{self.base}/{fn}/raw/{ref}/{path}" if kind == "file" else None,
               "_links": {"self": f"{self.api}/repos/{fn}/contents/{path}?ref={ref}", "git": git_url, "html": html}}
        if typ == "blob":
            data = gitrepo.read_blob(self.store.repo_path(repo), sha)
            out["size"] = len(data)
            if with_content:
                out["content"] = base64.encodebytes(data).decode()
                out["encoding"] = "base64"
        return out

    # -- CI ------------------------------------------------------------------------------------
    def check_suite(self, repo: dict, s: dict) -> dict:
        fn = repo["full_name"]
        return {
            "id": s["id"], "node_id": node_id("CS", s["id"]), "head_branch": s["head_branch"], "head_sha": s["head_sha"],
            "status": s["status"], "conclusion": s.get("conclusion"), "url": f"{self.api}/repos/{fn}/check-suites/{s['id']}",
            "before": s.get("before"), "after": s["head_sha"], "pull_requests": self.pr_refs(repo, s.get("pull_requests", [])),
            "app": self.actions_app(), "repository": self.repo(repo), "created_at": s["created_at"],
            "updated_at": s["updated_at"], "head_commit": self.head_commit(repo, s["head_sha"]),
            "latest_check_runs_count": len(s.get("check_runs", [])),
            "check_runs_url": f"{self.api}/repos/{fn}/check-suites/{s['id']}/check-runs",
            "rerequestable": True, "runs_rerequestable": True,
        }

    def check_run(self, repo: dict, c: dict) -> dict:
        fn = repo["full_name"]
        job = repo["jobs"].get(str(c.get("job"))) if c.get("job") else None
        html = f"{self.base}/{fn}/actions/runs/{job['run_id']}/job/{job['id']}" if job else f"{self.base}/{fn}/runs/{c['id']}"
        return {
            "id": c["id"], "node_id": node_id("CR", c["id"]), "head_sha": c["head_sha"], "name": c["name"],
            "external_id": str(c.get("job") or c["id"]), "url": f"{self.api}/repos/{fn}/check-runs/{c['id']}",
            "html_url": html, "details_url": html, "status": c["status"], "conclusion": c.get("conclusion"),
            "started_at": c.get("started_at"), "completed_at": c.get("completed_at"),
            "output": {"title": c.get("title"), "summary": c.get("summary"), "text": None, "annotations_count": 0,
                       "annotations_url": f"{self.api}/repos/{fn}/check-runs/{c['id']}/annotations"},
            "check_suite": {"id": c["suite"]}, "app": self.actions_app(),
            "pull_requests": self.pr_refs(repo, c.get("pull_requests", [])),
        }

    def pr_refs(self, repo: dict, numbers: list[int]) -> list[dict]:
        out = []
        for n in numbers:
            issue = repo["issues"].get(str(n))
            if not issue or not issue.get("pull"):
                continue
            info = self.m.pr_info(repo, issue)
            url = f"{self.api}/repos/{repo['full_name']}/pulls/{n}"
            ref = {"id": repo["id"], "url": f"{self.api}/repos/{repo['full_name']}", "name": repo["name"]}
            out.append({"id": issue["id"], "number": n, "url": url,
                        "head": {"ref": issue["pull"]["head_ref"], "sha": info["head_sha"], "repo": ref},
                        "base": {"ref": issue["pull"]["base_ref"], "sha": info["base_sha"], "repo": ref}})
        return out

    def workflow_run(self, repo: dict, r: dict) -> dict:
        fn = repo["full_name"]
        api = f"{self.api}/repos/{fn}/actions/runs/{r['id']}"
        return {
            "id": r["id"], "name": r["name"], "node_id": node_id("WFR", r["id"]), "check_suite_id": r["suite"],
            "check_suite_node_id": node_id("CS", r["suite"]), "head_branch": r["head_branch"], "head_sha": r["head_sha"],
            "path": r["path"], "run_number": r["run_number"], "run_attempt": r["run_attempt"], "event": r["event"],
            "status": r["status"], "conclusion": r.get("conclusion"), "workflow_id": r["workflow_id"],
            "display_title": r["display_title"], "url": api, "html_url": f"{self.base}/{fn}/actions/runs/{r['id']}",
            "pull_requests": self.pr_refs(repo, r.get("pull_requests", [])), "created_at": r["created_at"],
            "updated_at": r["updated_at"], "run_started_at": r.get("run_started_at"),
            "actor": self.user(r["actor"]), "triggering_actor": self.user(r.get("triggering_actor") or r["actor"]),
            "jobs_url": f"{api}/jobs", "logs_url": f"{api}/logs", "check_suite_url": f"{self.api}/repos/{fn}/check-suites/{r['suite']}",
            "artifacts_url": f"{api}/artifacts", "cancel_url": f"{api}/cancel", "rerun_url": f"{api}/rerun",
            "previous_attempt_url": f"{api}/attempts/{r['run_attempt'] - 1}" if r["run_attempt"] > 1 else None,
            "workflow_url": f"{self.api}/repos/{fn}/actions/workflows/{r['workflow_id']}",
            "head_commit": self.head_commit(repo, r["head_sha"]), "repository": self.repo(repo),
            "head_repository": self.repo(repo), "referenced_workflows": [],
        }

    def job(self, repo: dict, j: dict) -> dict:
        fn = repo["full_name"]
        run = repo["workflow_runs"][str(j["run_id"])]
        return {
            "id": j["id"], "run_id": j["run_id"], "run_attempt": j["run_attempt"], "node_id": node_id("CR", j["check_run"]),
            "run_url": f"{self.api}/repos/{fn}/actions/runs/{j['run_id']}", "head_sha": run["head_sha"],
            "head_branch": run["head_branch"], "workflow_name": run["name"], "name": j["name"],
            "url": f"{self.api}/repos/{fn}/actions/jobs/{j['id']}",
            "html_url": f"{self.base}/{fn}/actions/runs/{j['run_id']}/job/{j['id']}",
            "status": j["status"], "conclusion": j.get("conclusion"), "created_at": j["created_at"],
            "started_at": j.get("started_at") or j["created_at"], "completed_at": j.get("completed_at"),
            "check_run_url": f"{self.api}/repos/{fn}/check-runs/{j['check_run']}",
            "steps": [{"name": s["name"], "status": s["status"], "conclusion": s.get("conclusion"),
                       "number": i + 1, "started_at": s.get("started_at"), "completed_at": s.get("completed_at")}
                      for i, s in enumerate(j["steps"])],
            "labels": ["ubuntu-latest"], "runner_id": 1 if j.get("started_at") else None,
            "runner_name": "mock-runner" if j.get("started_at") else None,
            "runner_group_id": 1 if j.get("started_at") else None,
            "runner_group_name": "Default" if j.get("started_at") else None,
        }

    def status(self, repo: dict, s: dict) -> dict:
        return {"url": f"{self.api}/repos/{repo['full_name']}/statuses/{s['sha']}", "avatar_url": None,
                "id": s["id"], "node_id": node_id("SC", s["id"]), "state": s["state"], "description": s.get("description"),
                "target_url": s.get("target_url"), "context": s["context"], "created_at": s["created_at"],
                "updated_at": s["updated_at"], "creator": self.user(s["creator"])}


def _z(stamp: str) -> str:
    """git's strict ISO date → GitHub's UTC `Z` form."""
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(stamp).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return stamp
