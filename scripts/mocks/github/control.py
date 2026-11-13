"""`/_mock/*`: the certification script's handle on the mock — create accounts and repositories
(seeded from a local directory), install the App with chosen repositories and permissions,
comment as a person, set CI results, configure branch protection and the webhook target, read
state and deliveries, wait for CI to settle, reset; and for the issue tracker: create a project
(Status single-select, optional Priority), open an issue as a person (optionally adding it to a
project with a status), and move a project item as a person."""

from __future__ import annotations

import json

from . import gitrepo
from .spec import SpecError


def _json(raw: bytes) -> dict:
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SpecError(400, "the control API takes a JSON object")
    return value


def _repo(m, full: str | None) -> dict:
    owner, _, name = (full or "").partition("/")
    repo = m.find_repo(owner, name) if name else None
    if repo is None:
        raise SpecError(404, f"no repository {full!r}")
    return repo


def _summary(server, repo: dict) -> dict:
    m = server.mock
    path = m.path(repo)
    out = {"full_name": repo["full_name"], "id": repo["id"], "default_branch": repo["default_branch"],
           "clone_url": f"{m.public_url}/{repo['full_name']}.git",
           "branches": {k.removeprefix("refs/heads/"): v for k, v in gitrepo.refs(path).items()
                        if k.startswith("refs/heads/")},
           "pulls": [], "ci": repo["ci"], "ci_results": repo["ci_results"], "protection": repo["protection"],
           "workflow_runs": [], "comments": list(repo["comments"].values()),
           "review_comments": list(repo["review_comments"].values()), "reviews": list(repo["reviews"].values()),
           "labels": sorted(x["name"] for x in repo["labels"].values()), "statuses": repo["statuses"]}
    for issue in sorted(repo["issues"].values(), key=lambda i: i["number"]):
        if issue.get("pull"):
            info = m.pr_info(repo, issue)
            out["pulls"].append({"number": issue["number"], "title": issue["title"], "state": issue["state"],
                                 "user": issue["user"], "labels": issue["labels"], **issue["pull"],
                                 "head_sha": info["head_sha"], "base_sha": info["base_sha"],
                                 "mergeable": info["mergeable"], "mergeable_state": info["mergeable_state"]})
    for run in sorted(repo["workflow_runs"].values(), key=lambda r: r["id"]):
        jobs = [repo["jobs"][str(j)] for j in run["jobs"]]
        out["workflow_runs"].append({k: run[k] for k in ("id", "name", "event", "status", "conclusion", "head_sha",
                                                         "head_branch", "run_attempt", "pull_requests")}
                                    | {"jobs": [{"id": j["id"], "name": j["name"], "conclusion": j["conclusion"]}
                                                for j in jobs]})
    return out


def handle(server, method: str, path: str, query: dict, raw: bytes) -> tuple[int, object]:
    m = server.mock
    try:
        body = _json(raw) if method in ("POST", "PUT", "PATCH") else {}
        return _route(server, m, method, path, query, body)
    except SpecError as exc:
        return exc.status, exc.body
    except (KeyError, ValueError, TypeError) as exc:
        return 400, {"message": f"bad control request: {type(exc).__name__}: {exc}"}


def _route(server, m, method: str, path: str, query: dict, body: dict) -> tuple[int, object]:
    store = m.store
    key = (method, path.rstrip("/"))
    if key == ("GET", "/_mock/health"):
        return 200, {"ok": True, "url": server.url, "https_url": server.https_url, "ca": server.ca_path}
    if key == ("GET", "/_mock/app"):
        app = store.app
        return 200, {**app, "installations": sorted(store.data["installations"].values(), key=lambda i: i["id"]),
                     "env": {"GITHUB_APP_ID": str(app["id"]), "GITHUB_APP_PRIVATE_KEY": app.get("private_key"),
                             "GITHUB_APP_INSTALLATION_ID": min((i for i in store.data["installations"]), key=int,
                                                               default=None),
                             "GITHUB_API_URL": m.public_url, "GITHUB_GRAPHQL_URL": f"{m.public_url}/graphql",
                             "GITHUB_SERVER_URL": m.public_url}}
    if key == ("POST", "/_mock/app"):
        store.register_app_key(body.get("public_key"), body.get("private_key"))
        return 200, {"id": store.app["id"], "public_key": store.app["public_key"]}
    if key == ("POST", "/_mock/repos"):
        repo = m.create_repo(body["owner"], body["name"], seed=body.get("seed"),
                             default_branch=body.get("default_branch", "main"), private=body.get("private", True),
                             description=body.get("description", ""), ci=body.get("ci"))
        for label in body.get("labels") or []:
            store.label(repo, label, create=True)
        store.save()
        return 201, _summary(server, repo)
    if key == ("POST", "/_mock/installations"):
        repos = body.get("repositories")
        if repos is not None and repos != "all":
            repos = [r if "/" in r else f"{body['account']}/{r}" for r in repos]
        inst = store.install(body["account"], None if repos in (None, "all") else repos, body.get("permissions"))
        return 201, inst
    if key == ("POST", "/_mock/users"):
        acc = store.account(body["login"], body.get("type", "User"))
        if body.get("name"):
            acc["name"] = body["name"]
        return 201, {**acc, "token": store.user_token(acc["login"])}
    if key == ("POST", "/_mock/projects"):
        p = m.create_project(body["owner"], body.get("number"), title=body.get("title", "Symphony"),
                             priority=body.get("priority", False), creator=body.get("user"))
        return 201, _project(m, p)
    if key == ("POST", "/_mock/issues"):
        return _issue(m, body)
    if key == ("POST", "/_mock/project-items"):
        return _item(m, body)
    if key == ("POST", "/_mock/project-items/move"):
        return _move(m, body)
    if key == ("POST", "/_mock/comment"):
        return _comment(m, body)
    if key == ("POST", "/_mock/ci"):
        return _ci(m, body)
    if key == ("POST", "/_mock/ci/config"):
        repo = _repo(m, body.pop("repo"))
        allowed = {"workflow", "path", "jobs", "mode", "command", "delay_s", "skip_drafts"}
        unknown = set(body) - allowed
        if unknown:
            raise SpecError(400, f"unknown CI settings: {sorted(unknown)}")
        repo["ci"].update(body)
        store.save()
        return 200, repo["ci"]
    if key == ("POST", "/_mock/ci/drain"):
        idle = m.ci.drain(float(body.get("timeout_s", 60)))
        flushed = m.hooks.flush(float(body.get("timeout_s", 30)))
        return 200, {"idle": idle, "webhooks_flushed": flushed}
    if key == ("POST", "/_mock/protection"):
        repo = _repo(m, body["repo"])
        checks = body.get("required_checks")
        if checks is None:
            repo["protection"].pop(body["branch"], None)
        else:
            repo["protection"][body["branch"]] = {"required_status_checks": {"strict": False, "contexts": checks},
                                                  "enforce_admins": True, "required_pull_request_reviews": None,
                                                  "restrictions": None}
        store.save()
        return 200, repo["protection"]
    if key == ("POST", "/_mock/webhook"):
        store.data["webhook"] = {"url": body.get("url"), "secret": body.get("secret")}
        store.save()
        return 200, {"url": body.get("url"), "secret_set": bool(body.get("secret"))}
    if key == ("GET", "/_mock/state"):
        if query.get("repo"):
            return 200, _summary(server, _repo(m, query["repo"]))
        snap = store.snapshot()
        return 200, {"app": {k: v for k, v in snap["app"].items() if k != "private_key"},
                     "accounts": snap["accounts"], "installations": snap["installations"],
                     "webhook": {"url": snap["webhook"].get("url")},
                     "repos": [_summary(server, r) for r in store.data["repos"].values()]}
    if key == ("GET", "/_mock/deliveries"):
        items = list(m.hooks.deliveries)
        if query.get("event"):
            items = [d for d in items if d["event"] == query["event"]]
        if query.get("since"):
            items = [d for d in items if d["id"] > int(query["since"])]
        if query.get("brief"):
            items = [{k: v for k, v in d.items() if k != "payload"} for d in items]
        return 200, items
    if key == ("GET", "/_mock/requests"):
        return 200, list(m.requests)
    if key == ("GET", "/_mock/violations"):
        return 200, {"violations": m.violations, "served": sorted(f"{a} {b} {c}" for a, b, c in m.served)}
    if key == ("POST", "/_mock/reset"):
        m.ci.drain(10)
        store.reset()
        m.hooks.deliveries.clear()
        m.requests.clear()
        m.violations.clear()
        return 200, {"reset": True}
    raise SpecError(404, f"no control route {method} {path}")


def _project(m, p: dict) -> dict:
    return {"id": m.project_node_id(p), "database_id": p["id"], "owner": p["owner"], "number": p["number"],
            "title": p["title"],
            "fields": [{"id": m.field_node_id(p, f), "name": f["name"], "type": f["type"],
                        **({"options": f["options"]} if "options" in f else {})} for f in p["fields"]],
            "items": [{"id": m.item_node_id(p, i), "database_id": i["id"], "type": i["type"],
                       "repo_id": i.get("repo_id"), "number": i.get("number"), "values": i["values"]}
                      for i in p["items"]]}


def _issue(m, body: dict) -> tuple[int, object]:
    """`{repo, user, title, body?, labels?, project?: number, status?}`: a person opens an issue; with
    `project` it is added to the repository owner's project `number` (and set to `status`)."""
    repo = _repo(m, body["repo"])
    user = body["user"]
    m.store.account(user, "User")
    issue = m.create_issue(repo, user, body["title"], body.get("body"), body.get("labels") or [])
    out = {"issue": m.render().issue(repo, issue)}
    if body.get("project") is not None:
        item = m.add_project_item(repo["owner"], body["project"], repo=repo, issue_number=issue["number"], actor=user)
        if body.get("status"):
            m.set_item_field(repo["owner"], body["project"], item, "Status", body["status"], user)
        out["item_id"] = item
    return 201, out


def _item(m, body: dict) -> tuple[int, object]:
    """`{owner, number, repo?, issue_number?, draft_title?, user?}`: adds an issue, pull request or draft."""
    repo = _repo(m, body["repo"]) if body.get("repo") else None
    item = m.add_project_item(body["owner"], body["number"], repo=repo, issue_number=body.get("issue_number"),
                              draft_title=body.get("draft_title"), actor=body.get("user"))
    return 201, {"item_id": item}


def _move(m, body: dict) -> tuple[int, object]:
    """`{owner, number, item_id | repo + issue_number, user, status? | field + value}`: a person sets
    a field (Status by default) of a project item."""
    project = m.project(body["owner"], body["number"])
    if project is None:
        raise SpecError(404, f"no project {body['owner']}#{body['number']}")
    item_id = body.get("item_id")
    if item_id is None:
        repo = _repo(m, body["repo"])
        item = next((i for i in project["items"] if i.get("repo_id") == repo["id"]
                     and i.get("number") == int(body["issue_number"])), None)
        if item is None:
            raise SpecError(404, f"{body['repo']}#{body['issue_number']} is not in the project")
        item_id = item["id"]
    user = body.get("user")
    if user:
        m.store.account(user, "User")
    item = m.set_item_field(body["owner"], body["number"], item_id, body.get("field", "Status"),
                            body["value"] if "value" in body else body["status"], user)
    return 200, {"item_id": m.item_node_id(project, item), "values": item["values"]}


def _comment(m, body: dict) -> tuple[int, object]:
    """A person's comment: `kind` issue (the conversation), review_comment (inline, on `path`/`line`
    of the head, or `in_reply_to`), or review (`event` COMMENT|APPROVE|REQUEST_CHANGES)."""
    repo = _repo(m, body["repo"])
    issue = m.issue(repo, int(body["number"]))
    if issue is None:
        raise SpecError(404, f"no issue or pull request #{body['number']}")
    user = body["user"]
    m.store.account(user, "User")
    kind = body.get("kind", "issue")
    r = m.render()
    if kind == "issue":
        return 201, r.comment(repo, m.add_comment(repo, issue, user, body["body"]))
    if not issue.get("pull"):
        raise SpecError(422, f"#{issue['number']} is not a pull request")
    if kind == "review_comment":
        info = m.pr_info(repo, issue)
        c = m.add_review_comment(repo, issue, user, body["body"], commit_id=body.get("commit_id") or info["head_sha"],
                                 path=body.get("path"), line=body.get("line"), side=body.get("side", "RIGHT"),
                                 in_reply_to=body.get("in_reply_to"))
        return 201, r.review_comment(repo, c)
    if kind == "review":
        review = m.add_review(repo, issue, user, body.get("body"), body.get("event", "COMMENT"), None,
                              body.get("comments") or [])
        return 201, r.review(repo, review)
    raise SpecError(400, f"unknown comment kind {kind!r}")


def _ci(m, body: dict) -> tuple[int, object]:
    """Sets (or with `clear`, removes) the CI result for a head `sha`, a pull request `pr`, a `branch`
    or every run (`default: true`): `{conclusion, jobs?: {name: conclusion}, log?, delay_s?, hold?,
    interrupted?}`. `rerun: true` starts a new attempt of the completed runs it now covers."""
    repo = _repo(m, body["repo"])
    if body.get("sha"):
        sha = gitrepo.resolve(m.path(repo), body["sha"])
        if sha is None:
            raise SpecError(404, f"no commit {body['sha']}")
        key = sha
    elif body.get("pr"):
        key = f"pr:{int(body['pr'])}"
    elif body.get("branch"):
        key = f"branch:{body['branch']}"
    elif body.get("default"):
        key = "*"
    else:
        raise SpecError(400, "give sha, pr, branch or default")
    result = None
    if not body.get("clear"):
        result = {k: body[k] for k in ("conclusion", "jobs", "log", "delay_s", "hold", "interrupted") if k in body}
        result.setdefault("conclusion", "success")
    m.ci.set_result(repo, key, result)
    rerun = []
    if body.get("rerun"):
        with m.lock:
            for run in list(repo["workflow_runs"].values()):
                if run["status"] == "completed" and m.ci.override(repo, run) is result and result is not None:
                    if m.ci.rerun(repo, run, "mock-ci", failed_only=False):
                        rerun.append(run["id"])
    return 200, {"key": key, "result": result, "rerun": rerun}
