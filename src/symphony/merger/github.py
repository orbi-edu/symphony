"""GitHub for the merger, as the GitHub App installation (`symphony.ghapp`).

`GitHub` names every call the merger makes; `RestGitHub` is the network edge (REST, plus
GraphQL where REST has no equivalent: the ready-for-review time, draft conversion and review
threads). Tests substitute an in-memory implementation of the same methods.

A PR is a dict: `number, state (open|closed|merged), draft, base, head_sha, head_ref, author,
labels, ready_at (ISO), title, body, url, mergeable (MERGEABLE|CONFLICTING|UNKNOWN)`.
"""

from __future__ import annotations

from collections.abc import Callable

from ..endpoints import github_api_url, github_graphql_url

TIMEOUT_S = 30.0

_PR_FIELDS = """
  number state isDraft baseRefName headRefOid headRefName title body url mergeable id
  author { login __typename } labels(first: 50) { nodes { name } } createdAt
  timelineItems(last: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) { nodes { ... on ReadyForReviewEvent { createdAt } } }
  reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } } } }
"""


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class GitHub:
    """The calls the merger makes. Every method raises `GitHubError` on failure."""

    def list_open(self) -> list[dict]: raise NotImplementedError
    def pr(self, n: int) -> dict: raise NotImplementedError
    def add_labels(self, n: int, labels: list[str]) -> None: raise NotImplementedError
    def remove_label(self, n: int, label: str) -> None: raise NotImplementedError
    def comment(self, n: int, body: str) -> str: raise NotImplementedError
    def edit_comment(self, url: str, body: str) -> None: raise NotImplementedError
    def discussion(self, n: int) -> list[dict]: raise NotImplementedError
    def set_draft(self, n: int, draft: bool) -> None: raise NotImplementedError
    def close(self, n: int) -> None: raise NotImplementedError
    def merge(self, n: int, sha: str, method: str) -> tuple[bool, str]: raise NotImplementedError
    def check_runs(self, sha: str) -> list[dict]: raise NotImplementedError
    def workflow_runs(self, *, sha: str | None = None, branch: str | None = None,
                      event: str | None = None, status: str | None = None) -> list[dict]:
        raise NotImplementedError
    def run(self, run_id: int) -> dict: raise NotImplementedError
    def run_jobs(self, run_id: int) -> list[dict]: raise NotImplementedError
    def job_log(self, job_id: int) -> str: raise NotImplementedError
    def rerun_failed(self, run_id: int) -> None: raise NotImplementedError
    def rerun(self, run_id: int) -> None: raise NotImplementedError
    def cancel(self, run_id: int) -> None: raise NotImplementedError
    def reviews(self, n: int) -> list[dict]: raise NotImplementedError
    def post_review(self, n: int, commit_id: str, body: str, comments: list[dict]) -> list[dict]:
        raise NotImplementedError
    def review_threads(self, n: int) -> list[dict]: raise NotImplementedError
    def resolve_thread(self, thread_id: str) -> None: raise NotImplementedError
    def artifact_download_url(self, run_id: int, name: str) -> str | None: raise NotImplementedError


def _pr_from_graphql(node: dict) -> dict:
    ready = (node.get("timelineItems") or {}).get("nodes") or []
    state = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}[node["state"]]
    return {
        "number": node["number"],
        "state": state,
        "draft": bool(node["isDraft"]),
        "base": node["baseRefName"],
        "head_sha": node["headRefOid"],
        "head_ref": node["headRefName"],
        "author": (node.get("author") or {}).get("login", "ghost"),
        "author_type": (node.get("author") or {}).get("__typename", "User"),
        "labels": [x["name"] for x in (node.get("labels") or {}).get("nodes", [])],
        "ready_at": (ready[0].get("createdAt") if ready and ready[0] else None) or node["createdAt"],
        "title": node.get("title") or "",
        "body": node.get("body") or "",
        "url": node.get("url") or "",
        "mergeable": node.get("mergeable") or "UNKNOWN",
        "node_id": node["id"],
        "requested_reviewers": [
            r["requestedReviewer"]["login"]
            for r in (node.get("reviewRequests") or {}).get("nodes") or []
            if (r or {}).get("requestedReviewer") and r["requestedReviewer"].get("login")
        ],
    }


def _run(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r.get("name") or "",
        "status": r.get("status") or "",
        "conclusion": r.get("conclusion") or "",
        "attempt": r.get("run_attempt") or 1,
        "sha": r.get("head_sha") or "",
        "branch": r.get("head_branch") or "",
        "event": r.get("event") or "",
        "url": r.get("html_url") or "",
        "title": r.get("display_title") or "",
    }


class RestGitHub(GitHub):
    def __init__(self, repo: str, token: Callable[[], str], http=None) -> None:
        import httpx

        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self._token = token
        self._http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)

    def _req(self, method: str, path: str, *, json=None, params=None, ok=(200, 201, 202, 204)):
        url = path if path.startswith("http") else f"{github_api_url()}{path}"
        resp = self._http.request(
            method, url, json=json, params=params,
            headers={"Authorization": f"Bearer {self._token()}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        if resp.status_code not in ok:
            raise GitHubError(f"{method} {path}: {resp.status_code} {resp.text[:500]}", resp.status_code)
        return resp

    def _json(self, method: str, path: str, **kw):
        resp = self._req(method, path, **kw)
        return resp.json() if resp.content else {}

    def _repo(self, path: str) -> str:
        return f"/repos/{self.repo}{path}"

    def _graphql(self, query: str, variables: dict) -> dict:
        data = self._json("POST", github_graphql_url(), json={"query": query, "variables": variables})
        if data.get("errors"):
            # GraphQL answers 200 with a NOT_FOUND error: the same 404 the REST calls raise.
            missing = all(e.get("type") == "NOT_FOUND" for e in data["errors"])
            raise GitHubError(f"graphql: {data['errors']}", 404 if missing else 0)
        return data["data"]

    def _pages(self, path: str, key: str | None = None, params: dict | None = None) -> list:
        out: list = []
        page = 1
        while True:
            data = self._json("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            items = data.get(key, []) if key else data
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    # -- PRs -----------------------------------------------------------------------
    def list_open(self) -> list[dict]:
        query = f"""query($owner: String!, $name: String!, $after: String) {{
          repository(owner: $owner, name: $name) {{
            pullRequests(states: OPEN, first: 50, after: $after) {{
              nodes {{ {_PR_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }} }} }}"""
        out, after = [], None
        while True:
            data = self._graphql(query, {"owner": self.owner, "name": self.name, "after": after})
            conn = data["repository"]["pullRequests"]
            out.extend(_pr_from_graphql(n) for n in conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                return out
            after = conn["pageInfo"]["endCursor"]

    def pr(self, n: int) -> dict:
        query = f"""query($owner: String!, $name: String!, $n: Int!) {{
          repository(owner: $owner, name: $name) {{ pullRequest(number: $n) {{ {_PR_FIELDS} }} }} }}"""
        data = self._graphql(query, {"owner": self.owner, "name": self.name, "n": n})
        node = data["repository"]["pullRequest"]
        if node is None:
            raise GitHubError(f"PR #{n} not found", 404)
        return _pr_from_graphql(node)

    def add_labels(self, n: int, labels: list[str]) -> None:
        if labels:
            self._req("POST", self._repo(f"/issues/{n}/labels"), json={"labels": labels})

    def remove_label(self, n: int, label: str) -> None:
        self._req("DELETE", self._repo(f"/issues/{n}/labels/{label}"), ok=(200, 204, 404))

    def comment(self, n: int, body: str) -> str:
        data = self._json("POST", self._repo(f"/issues/{n}/comments"), json={"body": body[:65536]})
        return data["html_url"]

    def edit_comment(self, url: str, body: str) -> None:
        cid = url.rsplit("#issuecomment-", 1)[-1]
        if not cid.isdigit():
            raise GitHubError(f"{url or '(no URL)'} names no comment to edit")
        self._req("PATCH", self._repo(f"/issues/comments/{cid}"), json={"body": body[:65536]})

    def discussion(self, n: int) -> list[dict]:
        out = [
            {"at": c["created_at"], "who": c["user"]["login"], "where": "PR", "body": c.get("body") or ""}
            for c in self._pages(self._repo(f"/issues/{n}/comments"))
        ]
        for c in self._pages(self._repo(f"/pulls/{n}/comments")):
            line = c.get("line") or c.get("original_line")
            out.append({"at": c["created_at"], "who": c["user"]["login"],
                        "where": f"on {c['path']}{f':{line}' if line else ''}", "body": c.get("body") or ""})
        for r in self._pages(self._repo(f"/pulls/{n}/reviews")):
            if (r.get("body") or "").strip():
                out.append({"at": r.get("submitted_at") or "", "who": r["user"]["login"],
                            "where": f"review ({r['state'].lower()})", "body": r["body"]})
        return sorted(out, key=lambda e: e["at"])

    def set_draft(self, n: int, draft: bool) -> None:
        node = self.pr(n)["node_id"]
        mutation = (
            "mutation($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { clientMutationId } }"
            if draft else
            "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { clientMutationId } }"
        )
        self._graphql(mutation, {"id": node})

    def close(self, n: int) -> None:
        """`PATCH pulls/N` with `state: closed`: closed, not merged; a person can reopen it."""
        self._req("PATCH", self._repo(f"/pulls/{n}"), json={"state": "closed"})

    def merge(self, n: int, sha: str, method: str) -> tuple[bool, str]:
        """`PUT pulls/N/merge` with `sha`: GitHub merges only that exact head
        (`--match-head-commit`)."""
        resp = self._req("PUT", self._repo(f"/pulls/{n}/merge"),
                         json={"sha": sha, "merge_method": method}, ok=(200, 405, 409, 422))
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("merged"):
            return True, ""
        return False, data.get("message") or f"merge answered {resp.status_code}"

    # -- CI ------------------------------------------------------------------------
    def check_runs(self, sha: str) -> list[dict]:
        runs = self._pages(self._repo(f"/commits/{sha}/check-runs"), "check_runs")
        return [
            {"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion") or "",
             "started_at": c.get("started_at") or "", "url": c.get("html_url") or ""}
            for c in runs
        ]

    def workflow_runs(self, *, sha=None, branch=None, event=None, status=None) -> list[dict]:
        params = {k: v for k, v in (("head_sha", sha), ("branch", branch), ("event", event),
                                     ("status", status)) if v}
        data = self._json("GET", self._repo("/actions/runs"), params={**params, "per_page": 50})
        return [_run(r) for r in data.get("workflow_runs", [])]

    def run(self, run_id: int) -> dict:
        return _run(self._json("GET", self._repo(f"/actions/runs/{run_id}")))

    def run_jobs(self, run_id: int) -> list[dict]:
        jobs = self._pages(self._repo(f"/actions/runs/{run_id}/jobs"), "jobs")
        return [
            {"id": j["id"], "name": j["name"], "conclusion": j.get("conclusion") or "",
             "url": j.get("html_url") or "",
             "steps": [{"name": s["name"], "conclusion": s.get("conclusion") or ""}
                       for s in j.get("steps") or []]}
            for j in jobs
        ]

    def job_log(self, job_id: int) -> str:
        return self._req("GET", self._repo(f"/actions/jobs/{job_id}/logs")).text

    def rerun_failed(self, run_id: int) -> None:
        self._req("POST", self._repo(f"/actions/runs/{run_id}/rerun-failed-jobs"))

    def rerun(self, run_id: int) -> None:
        self._req("POST", self._repo(f"/actions/runs/{run_id}/rerun"))

    def cancel(self, run_id: int) -> None:
        self._req("POST", self._repo(f"/actions/runs/{run_id}/cancel"), ok=(202, 409))

    def reviews(self, n: int) -> list[dict]:
        """The human reviews on a PR: each has `user`, `state` (APPROVED, CHANGES_REQUESTED,
        COMMENTED, DISMISSED, PENDING) and `submitted_at`. Reviews by bots and apps (a review
        service, Symphony itself) are left out: they are not the human approval."""
        raw = self._pages(self._repo(f"/pulls/{n}/reviews"))
        return [
            {"user": (r.get("user") or {}).get("login", "ghost"),
             "state": r.get("state", ""),
             "submitted_at": r.get("submitted_at") or ""}
            for r in raw
            if (r.get("user") or {}).get("type") != "Bot"
            and not (r.get("user") or {}).get("login", "").endswith("[bot]")
        ]

    def post_review(self, n: int, commit_id: str, body: str, comments: list[dict]) -> list[dict]:
        """One `COMMENT` review on `commit_id` with inline comments (`{path, line, side, body}`,
        plus `start_line, start_side` for a range); never an approval or a change request, so
        the approval gate never counts it. Returns the review's comments, `{id, body}`. A line
        outside the PR's diff answers 422."""
        review = self._json("POST", self._repo(f"/pulls/{n}/reviews"), json={
            "commit_id": commit_id, "event": "COMMENT", "body": body[:65536], "comments": comments})
        return [{"id": c["id"], "body": c.get("body") or ""}
                for c in self._pages(self._repo(f"/pulls/{n}/reviews/{review['id']}/comments"))]

    def review_threads(self, n: int) -> list[dict]:
        """The PR's review threads: `{id (node id), resolved, comment}`, `comment` being the REST
        id of the thread's first comment."""
        query = """query($owner: String!, $name: String!, $n: Int!, $after: String) {
          repository(owner: $owner, name: $name) { pullRequest(number: $n) {
            reviewThreads(first: 100, after: $after) {
              nodes { id isResolved comments(first: 1) { nodes { databaseId } } }
              pageInfo { hasNextPage endCursor } } } } }"""
        out, after = [], None
        while True:
            data = self._graphql(query, {"owner": self.owner, "name": self.name, "n": n, "after": after})
            pr = data["repository"]["pullRequest"]
            if pr is None:
                raise GitHubError(f"PR #{n} not found", 404)
            conn = pr["reviewThreads"]
            for t in conn["nodes"]:
                first = (t.get("comments") or {}).get("nodes") or []
                out.append({"id": t["id"], "resolved": bool(t["isResolved"]),
                            "comment": first[0].get("databaseId") if first and first[0] else None})
            if not conn["pageInfo"]["hasNextPage"]:
                return out
            after = conn["pageInfo"]["endCursor"]

    def resolve_thread(self, thread_id: str) -> None:
        self._graphql("mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }",
                      {"id": thread_id})

    def artifact_download_url(self, run_id: int, name: str) -> str | None:
        """The download URL for a named artifact of a workflow run, or None."""
        data = self._json("GET", self._repo(f"/actions/runs/{run_id}/artifacts"))
        for a in data.get("artifacts", []):
            if a.get("name") == name and not a.get("expired"):
                return self._repo(f"/actions/artifacts/{a['id']}/zip")
        return None
