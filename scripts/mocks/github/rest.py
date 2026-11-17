"""The REST API: each served operation of GitHub's OpenAPI description, keyed by its path
template. The router (`dispatch`) matches the path against the WHOLE description, authenticates
(App JWT or installation/user token), enforces the token's repositories and permissions, checks
query parameters and the body against the operation's schemas, runs the handler and completes
the response to the documented schema."""

from __future__ import annotations

import base64
import hashlib
import hmac
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from . import gitrepo
from .core import validation_failed
from .objects import Render
from .spec import Operation, SpecError
from .state import LEVEL, Actor, iso

FORBIDDEN = "Resource not accessible by integration"

# Permission alternatives per operation: any one dict satisfied (every entry at that level) is enough.
META = ({"metadata": "read"},)
CONTENTS_R = ({"contents": "read"},)
CONTENTS_W = ({"contents": "write"},)
PR_R = ({"pull_requests": "read"},)
PR_W = ({"pull_requests": "write"},)
ISSUE_R = ({"issues": "read"}, {"pull_requests": "read"})
ISSUE_W = ({"issues": "write"}, {"pull_requests": "write"})
MERGE = ({"pull_requests": "write", "contents": "write"},)
CHECKS_R = ({"checks": "read"},)
STATUS_R = ({"statuses": "read"},)
STATUS_W = ({"statuses": "write"},)
ACTIONS_R = ({"actions": "read"},)
ACTIONS_W = ({"actions": "write"},)
ADMIN_R = ({"administration": "read"},)
ADMIN_W = ({"administration": "write"},)
ISSUES_R = ({"issues": "read"},)
ISSUES_W = ({"issues": "write"},)


@dataclass
class Req:
    m: Any
    op: Operation
    params: dict
    query: dict
    body: Any
    actor: Actor | None
    render: Render
    headers: dict
    repo: dict | None = None
    path: str = ""
    raw_query: dict = field(default_factory=dict)

    @property
    def login(self) -> str:
        return self.actor.login if self.actor else self.m.store.bot_login()

    def fail(self, status: int, message: str, **extra) -> SpecError:
        return SpecError(status, message, self.op.docs, **extra)

    def not_found(self) -> SpecError:
        return self.fail(404, "Not Found")


@dataclass
class Route:
    fn: Callable
    perms: tuple | None
    auth: str = "token"  # token | jwt


@dataclass
class Resp:
    status: int
    body: Any = None
    headers: dict = field(default_factory=dict)


ROUTES: dict[tuple[str, str], Route] = {}


def route(method: str, template: str, perms: tuple | None = None, auth: str = "token"):
    def deco(fn):
        ROUTES[(method, template)] = Route(fn, perms, auth)
        return fn
    return deco


# -- the router --------------------------------------------------------------------------
def dispatch(m, method: str, path: str, query: dict[str, str], headers: dict[str, str], raw: bytes,
             render: Render) -> Resp:
    spec = m.spec
    op = None
    try:
        op = spec.match(method, path)
        rt = ROUTES.get((op.method, op.template))
        if rt is None:
            raise SpecError(501, f"{op.method} {op.template} is in GitHub's description but not served by the mock",
                            op.docs)
        actor = None
        if rt.auth == "jwt":
            _verify_jwt(m, headers.get("authorization", ""), op)
        else:
            actor = _actor(m, headers.get("authorization", ""), op)
        req = Req(m, op, op.params, {}, None, actor, render, headers, path=path, raw_query=query)
        if "owner" in op.params and "repo" in op.params:
            repo = m.find_repo(op.params["owner"], op.params["repo"])
            if repo is None or (actor is not None and not m.store.can_see(actor, repo["full_name"])):
                raise req.not_found()
            req.repo = repo
            if actor is not None and rt.perms and not _allowed(actor, rt.perms):
                raise req.fail(403, FORBIDDEN)
        req.query = spec.query(op, query)
        req.body = spec.body(op, raw, headers.get("content-type", ""))
        with m.lock:
            out = rt.fn(req)
        resp = out if isinstance(out, Resp) else Resp(*out) if isinstance(out, tuple) else Resp(200, out)
    except SpecError as exc:
        resp = Resp(exc.status, exc.body)
    if op is not None:
        _finish(m, op, resp, render)
    return resp


def _finish(m, op: Operation, resp: Resp, render: Render) -> None:
    """Completes a JSON body to the documented response schema and, with `check_responses`,
    records any response that breaks it — or a success status the operation does not document."""
    m.served.add((op.method, op.template, resp.status))
    if m.check_responses and resp.status < 400 and str(resp.status) not in (op.op.get("responses") or {}):
        m.violations.append({"operation": f"{op.method} {op.template}", "status": resp.status,
                             "errors": ["status not documented for this operation"]})
    if not isinstance(resp.body, (dict, list)):
        return
    ptr = m.spec.response_pointer(op, resp.status)
    if ptr is None:
        return
    resp.body = m.spec.conform(ptr, resp.body, render.base)
    if m.check_responses:
        problems = m.spec.errors(ptr, resp.body)
        if problems:
            m.violations.append({"operation": f"{op.method} {op.template}", "status": resp.status,
                                 "errors": problems[:10]})


def _allowed(actor: Actor, alternatives: tuple) -> bool:
    return any(all(actor.level(p) >= LEVEL[lv] for p, lv in alt.items()) for alt in alternatives)


def _bearer(header: str) -> str:
    kind, _, value = header.partition(" ")
    return value.strip() if kind.lower() in ("bearer", "token") else ""


def _actor(m, header: str, op: Operation) -> Actor:
    if not header:
        raise SpecError(401, "Requires authentication", op.docs)
    if header.lower().startswith("basic "):
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            password = ""
        token = password
    else:
        token = _bearer(header)
    actor = m.store.actor(token) if token else None
    if actor is None:
        raise SpecError(401, "Bad credentials", op.docs)
    return actor


def _verify_jwt(m, header: str, op: Operation) -> dict:
    """GitHub's App authentication: an RS256 JWT signed with the App's private key, `iss` the
    App id (or client id), `iat` not in the future, `exp` at most 10 minutes after `iat` and
    after now."""
    import jwt

    token = _bearer(header)
    if not token or token.count(".") != 2:
        raise SpecError(401, "A JSON web token could not be decoded", op.docs)
    app = m.store.app
    try:
        claims = jwt.decode(token, app["public_key"], algorithms=["RS256"],
                            options={"verify_exp": False, "verify_iat": False, "verify_nbf": False})
    except jwt.InvalidTokenError:
        raise SpecError(401, "A JSON web token could not be decoded", op.docs) from None
    now = time.time()
    iat, exp, iss = claims.get("iat"), claims.get("exp"), claims.get("iss")
    if not isinstance(iat, int) or iat > now + 60:
        raise SpecError(401, "'Issued at' claim ('iat') must be an Integer representing the time that the "
                             "assertion was issued.", op.docs)
    if not isinstance(exp, int) or exp <= now:
        raise SpecError(401, "'Expiration time' claim ('exp') must be a numeric value representing the future "
                             "time at which the assertion expires.", op.docs)
    if exp - iat > 600 or exp > now + 600 + 1:
        raise SpecError(401, "'Expiration time' claim ('exp') is too far in the future", op.docs)
    if str(iss) not in (str(app["id"]), app["client_id"]):
        raise SpecError(401, "'Issuer' claim ('iss') must be an Integer", op.docs)
    return claims


def paginate(req: Req, items: list, key: str | None = None, extra: dict | None = None) -> Resp:
    per_page = max(1, min(100, int(req.query.get("per_page", 30))))
    page = max(1, int(req.query.get("page", 1)))
    total = len(items)
    chunk = items[(page - 1) * per_page: page * per_page]
    headers = {}
    last = max(1, -(-total // per_page))
    links = []
    base = f"{req.render.api}{req.path}"
    params = {k: v for k, v in req.raw_query.items() if k != "page"}
    if page < last:
        links.append(f'<{base}?{urlencode({**params, "page": page + 1})}>; rel="next"')
        links.append(f'<{base}?{urlencode({**params, "page": last})}>; rel="last"')
    if page > 1:
        links.append(f'<{base}?{urlencode({**params, "page": 1})}>; rel="first"')
        links.append(f'<{base}?{urlencode({**params, "page": page - 1})}>; rel="prev"')
    if links:
        headers["Link"] = ", ".join(links)
    if key is None:
        return Resp(200, chunk, headers)
    return Resp(200, {"total_count": total, key: chunk, **(extra or {})}, headers)


def _issue(req: Req, number_param: str = "issue_number") -> dict:
    issue = req.m.issue(req.repo, req.params[number_param])
    if issue is None:
        raise req.not_found()
    _may_touch(req, issue)
    return issue


def _may_touch(req: Req, issue: dict) -> None:
    """Issue routes take `issues` or `pull_requests`; on an issue (not a pull request) only `issues` counts."""
    if req.actor is None or issue.get("pull"):
        return
    level = "read" if req.op.method == "GET" else "write"
    if not _allowed(req.actor, ({"issues": level},)):
        raise req.fail(403, FORBIDDEN)


def _pull(req: Req) -> dict:
    issue = req.m.issue(req.repo, req.params["pull_number"])
    if issue is None or not issue.get("pull"):
        raise req.not_found()
    return issue


def _by_created(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda c: (c["created_at"], c["id"]))


# -- the App ------------------------------------------------------------------------------------
@route("GET", "/app", auth="jwt")
def get_app(req: Req):
    return req.render.app()


@route("GET", "/app/installations", auth="jwt")
def list_installations(req: Req):
    insts = sorted(req.m.store.data["installations"].values(), key=lambda i: i["id"])
    return paginate(req, [req.render.installation(i) for i in insts])


@route("GET", "/app/installations/{installation_id}", auth="jwt")
def get_installation(req: Req):
    inst = req.m.store.installation(req.params["installation_id"])
    if inst is None:
        raise req.not_found()
    return req.render.installation(inst)


@route("GET", "/repos/{owner}/{repo}/installation", auth="jwt")
def repo_installation(req: Req):
    inst = req.m.store.installation_for(req.repo["full_name"])
    if inst is None:
        raise req.not_found()
    return req.render.installation(inst)


@route("POST", "/app/installations/{installation_id}/access_tokens", auth="jwt")
def access_token(req: Req):
    store = req.m.store
    inst = store.installation(req.params["installation_id"])
    if inst is None:
        raise req.not_found()
    body = req.body or {}
    covered = store.installation_repos(inst)
    repos: list[str] | None = None
    if body.get("repositories") is not None or body.get("repository_ids") is not None:
        repos = []
        by_id = {store.data["repos"][k]["id"]: k for k in covered}
        for name in body.get("repositories") or []:
            full = f"{inst['account']}/{name}".lower()
            if full not in covered:
                raise req.fail(422, "There is at least one repository that does not exist or is not accessible "
                                    "to the parent installation.")
            repos.append(full)
        for rid in body.get("repository_ids") or []:
            if rid not in by_id:
                raise req.fail(422, "There is at least one repository that does not exist or is not accessible "
                                    "to the parent installation.")
            repos.append(by_id[rid])
        repos = sorted(set(repos))
    granted = inst["permissions"]
    wanted = body.get("permissions")
    if wanted is not None:
        for perm, level in wanted.items():
            if LEVEL.get(level, 0) > LEVEL.get(granted.get(perm, "none"), 0):
                raise req.fail(422, "The permissions requested are not granted to this installation.")
        permissions = {**wanted, "metadata": wanted.get("metadata", "read")}
    else:
        permissions = dict(granted)
    rec = store.mint(inst, repos, permissions)
    out = {"token": rec["token"], "expires_at": iso(rec["expires_at"]), "permissions": permissions,
           "repository_selection": "selected" if repos is not None else inst["repository_selection"]}
    if repos is not None or inst["repository_selection"] == "selected":
        out["repositories"] = [req.render.repo(store.data["repos"][k]) for k in (repos if repos is not None else covered)]
    return Resp(201, out)


@route("GET", "/installation/repositories")
def installation_repositories(req: Req):
    store = req.m.store
    actor = req.actor
    if actor.kind != "installation":
        raise req.fail(403, "You must authenticate with an installation access token in order to list repositories "
                            "for an installation.")
    inst = store.installation(actor.installation_id)
    names = [k for k in store.installation_repos(inst) if actor.repos is None or k in actor.repos]
    repos = [req.render.repo(store.data["repos"][k], actor) for k in names]
    selection = "selected" if actor.repos is not None else inst["repository_selection"]
    return paginate(req, repos, "repositories", {"repository_selection": selection})


# -- repositories, refs, contents --------------------------------------------------------------------
@route("GET", "/repos/{owner}/{repo}", META)
def get_repo(req: Req):
    return req.render.repo(req.repo, req.actor)


@route("GET", "/repos/{owner}/{repo}/contents/{path}", CONTENTS_R)
def get_contents(req: Req):
    repo = req.repo
    ref = req.query.get("ref") or repo["default_branch"]
    path = req.m.path(repo)
    sha = gitrepo.resolve(path, ref)
    if sha is None:
        raise req.fail(404, f"No commit found for the ref {ref}")
    file = req.params["path"].strip("/")
    entries = gitrepo.ls(path, sha, file)
    if entries is None:
        raise req.not_found()
    accept = req.headers.get("accept", "")
    is_dir = gitrepo.object_type(path, f"{sha}:{file}") == "tree" if file else True
    if is_dir:
        return [req.render.content(repo, ref, mode, typ, osha, name, False) for mode, typ, osha, name in entries]
    mode, typ, osha, name = entries[0]
    if "application/vnd.github.raw" in accept or "application/vnd.github.v3.raw" in accept:
        return Resp(200, gitrepo.read_blob(path, osha), {"Content-Type": "application/vnd.github.raw"})
    return req.render.content(repo, ref, mode, typ, osha, name, True)


@route("PUT", "/repos/{owner}/{repo}/contents/{path}", CONTENTS_W)
def put_contents(req: Req):
    body = req.body
    repo = req.repo
    branch = body.get("branch") or repo["default_branch"]
    try:
        content = base64.b64decode(body["content"], validate=False)
    except ValueError:
        raise req.fail(422, "Invalid request.\n\ncontent is not valid Base64") from None
    file = req.params["path"].strip("/")
    new, blob, created = req.m.write_file(repo, branch, file, content, body["message"], body.get("sha"), req.login,
                                          body.get("author") or body.get("committer"), req.op.docs)
    return Resp(201 if created else 200, _file_commit(req, branch, file, new, blob))


def _file_commit(req: Req, branch: str, file: str, sha: str, blob: str | None) -> dict:
    r, repo = req.render, req.repo
    commit = r.commit(repo, sha)
    content = None
    if blob:
        content = r.content(repo, branch, "100644", "blob", blob, file, False)
    info = commit["commit"]
    return {"content": content, "commit": {
        "sha": sha, "node_id": commit["node_id"], "url": info["url"], "html_url": commit["html_url"],
        "author": info["author"], "committer": info["committer"], "message": info["message"],
        "tree": info["tree"], "parents": [{"sha": p["sha"], "url": p["url"], "html_url": p["html_url"]}
                                          for p in commit["parents"]],
        "verification": {**info["verification"]}}}


@route("DELETE", "/repos/{owner}/{repo}/contents/{path}", CONTENTS_W)
def delete_contents(req: Req):
    body = req.body
    branch = body.get("branch") or req.repo["default_branch"]
    file = req.params["path"].strip("/")
    new, _, _ = req.m.write_file(req.repo, branch, file, None, body["message"], body["sha"], req.login,
                                 body.get("author") or body.get("committer"), req.op.docs)
    return _file_commit(req, branch, file, new, None)


@route("GET", "/repos/{owner}/{repo}/git/ref/{ref}", CONTENTS_R)
def get_ref(req: Req):
    ref = "refs/" + req.params["ref"].strip("/")
    sha = gitrepo.refs(req.m.path(req.repo)).get(ref)
    if sha is None:
        raise req.not_found()
    return req.render.git_ref(req.repo, ref, sha)


@route("POST", "/repos/{owner}/{repo}/git/refs", CONTENTS_W)
def create_ref(req: Req):
    ref, sha = req.body["ref"], req.body["sha"]
    path = req.m.path(req.repo)
    if not ref.startswith("refs/") or ref.count("/") < 2:
        raise req.fail(422, "Reference name must start with 'refs/' and have at least two slashes.")
    if ref in gitrepo.refs(path):
        raise req.fail(422, "Reference already exists")
    if gitrepo.object_type(path, sha) is None:
        raise req.fail(422, "Object does not exist")
    gitrepo.update_ref(path, ref, sha, gitrepo.ZERO)
    req.m.after_push(req.repo, [(ref, gitrepo.ZERO, sha)], req.login)
    return Resp(201, req.render.git_ref(req.repo, ref, sha))


@route("PATCH", "/repos/{owner}/{repo}/git/refs/{ref}", CONTENTS_W)
def update_ref(req: Req):
    ref = "refs/" + req.params["ref"].strip("/")
    path = req.m.path(req.repo)
    old = gitrepo.refs(path).get(ref)
    if old is None:
        raise req.fail(422, "Reference does not exist")
    sha = req.body["sha"]
    if gitrepo.object_type(path, sha) is None:
        raise req.fail(422, "Object does not exist")
    if not req.body.get("force") and not gitrepo.is_ancestor(path, old, sha):
        raise req.fail(422, "Update is not a fast forward")
    gitrepo.update_ref(path, ref, sha, old)
    req.m.after_push(req.repo, [(ref, old, sha)], req.login)
    return req.render.git_ref(req.repo, ref, sha)


@route("DELETE", "/repos/{owner}/{repo}/git/refs/{ref}", CONTENTS_W)
def delete_ref(req: Req):
    ref = "refs/" + req.params["ref"].strip("/")
    path = req.m.path(req.repo)
    old = gitrepo.refs(path).get(ref)
    if old is None:
        raise req.fail(422, "Reference does not exist")
    gitrepo.delete_ref(path, ref)
    req.m.after_push(req.repo, [(ref, old, gitrepo.ZERO)], req.login)
    return Resp(204)


@route("GET", "/repos/{owner}/{repo}/compare/{basehead}", CONTENTS_R)
def compare(req: Req):
    spec_ = req.params["basehead"]
    if "..." in spec_:
        base, head = spec_.split("...", 1)
    elif ".." in spec_:
        base, head = spec_.split("..", 1)
    else:
        raise req.not_found()
    repo, r = req.repo, req.render
    path = req.m.path(repo)
    b, h = gitrepo.resolve(path, base), gitrepo.resolve(path, head)
    if b is None or h is None:
        raise req.not_found()
    mb = gitrepo.merge_base(path, b, h)
    ahead = len(gitrepo.range_commits(path, b, h))
    behind = len(gitrepo.range_commits(path, h, b))
    status = "identical" if b == h else "ahead" if behind == 0 else "behind" if ahead == 0 else "diverged"
    fn = repo["full_name"]
    commits = [r.commit(repo, s) for s in gitrepo.range_commits(path, b, h)]
    return {"url": f"{r.api}/repos/{fn}/compare/{spec_}", "html_url": f"{r.base}/{fn}/compare/{spec_}",
            "permalink_url": f"{r.base}/{fn}/compare/{b[:7]}...{h[:7]}",
            "diff_url": f"{r.base}/{fn}/compare/{spec_}.diff", "patch_url": f"{r.base}/{fn}/compare/{spec_}.patch",
            "base_commit": r.commit(repo, b), "merge_base_commit": r.commit(repo, mb or b), "status": status,
            "ahead_by": ahead, "behind_by": behind, "total_commits": ahead, "commits": commits,
            "files": [r.diff_entry(repo, h, f) for f in gitrepo.files(path, b, h)]}


# -- labels ------------------------------------------------------------------------------------
@route("GET", "/repos/{owner}/{repo}/labels", ISSUE_R)
def list_labels(req: Req):
    labels = sorted(req.repo["labels"].values(), key=lambda x: x["name"].lower())
    return paginate(req, [req.render.label(req.repo, x) for x in labels])


@route("POST", "/repos/{owner}/{repo}/labels", ISSUE_W)
def create_label(req: Req):
    body = req.body
    if req.m.store.label(req.repo, body["name"]) is not None:
        raise validation_failed("Label", "already_exists", req.op.docs, "name", "already_exists")
    lab = req.m.store.label(req.repo, body["name"], create=True)
    lab["color"] = (body.get("color") or "ededed").lstrip("#")
    lab["description"] = body.get("description")
    req.m.store.save()
    return Resp(201, req.render.label(req.repo, lab))


@route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/labels", ISSUE_R)
def issue_labels(req: Req):
    issue = _issue(req)
    return paginate(req, req.render.labels(req.repo, issue["labels"]))


@route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/labels", ISSUE_W)
def add_labels(req: Req):
    issue = _issue(req)
    body = req.body
    raw = body.get("labels", []) if isinstance(body, dict) else body or []
    names = [x["name"] if isinstance(x, dict) else x for x in raw]
    req.m.add_labels(req.repo, issue, names, req.login)
    return req.render.labels(req.repo, issue["labels"])


@route("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels/{name}", ISSUE_W)
def remove_label(req: Req):
    issue = _issue(req)
    if not req.m.remove_label(req.repo, issue, req.params["name"], req.login):
        raise req.fail(404, "Label does not exist")
    return req.render.labels(req.repo, issue["labels"])


# -- issues and comments ------------------------------------------------------------------------
@route("GET", "/repos/{owner}/{repo}/issues", ISSUE_R)
def list_issues(req: Req):
    q = req.query
    state = q.get("state", "open")
    sees_issues = _allowed(req.actor, ISSUES_R)
    sees_pulls = _allowed(req.actor, PR_R)
    items = [i for i in req.repo["issues"].values() if (state == "all" or i["state"] == state)
             and (sees_pulls if i.get("pull") else sees_issues)]
    if q.get("labels"):
        wanted = [x.strip().lower() for x in q["labels"].split(",") if x.strip()]
        items = [i for i in items if all(w in [x.lower() for x in i["labels"]] for w in wanted)]
    if q.get("creator"):
        items = [i for i in items if i["user"].lower() == q["creator"].lower()]
    if q.get("since"):
        items = [i for i in items if i["updated_at"] >= q["since"]]
    key = {"updated": "updated_at", "comments": "created_at"}.get(q.get("sort", "created"), "created_at")
    items.sort(key=lambda i: (i[key], i["number"]), reverse=q.get("direction", "desc") == "desc")
    return paginate(req, [req.render.issue(req.repo, i) for i in items])


@route("POST", "/repos/{owner}/{repo}/issues", ISSUES_W)
def create_issue(req: Req):
    b = req.body
    labels = [x["name"] if isinstance(x, dict) else str(x) for x in b.get("labels") or []]
    issue = req.m.create_issue(req.repo, req.login, str(b["title"]), b.get("body"), labels)
    if b.get("parent_issue_id"):
        found = req.m.issue_by_id(b["parent_issue_id"])
        if found is None:
            raise req.not_found()
        req.m.add_sub_issue(found[0], found[1], issue["id"], False, req.login)
    return Resp(201, req.render.issue(req.repo, issue),
                {"Location": f"{req.render.api}/repos/{req.repo['full_name']}/issues/{issue['number']}"})


@route("GET", "/repos/{owner}/{repo}/issues/{issue_number}", ISSUE_R)
def get_issue(req: Req):
    return req.render.issue(req.repo, _issue(req))


@route("PATCH", "/repos/{owner}/{repo}/issues/{issue_number}", ISSUE_W)
def update_issue(req: Req):
    issue = _issue(req)
    b = req.body or {}
    labels = None
    if b.get("labels") is not None:
        labels = [x["name"] if isinstance(x, dict) else str(x) for x in b["labels"]]
    title = str(b["title"]) if b.get("title") is not None else None
    if issue.get("pull"):
        req.m.update_pr(req.repo, issue, req.login, title=title, body=b.get("body"), state=b.get("state"),
                        docs=req.op.docs)
        if labels is not None:
            req.m.update_issue(req.repo, issue, req.login, labels=labels)
    else:
        req.m.update_issue(req.repo, issue, req.login, title=title, body=b.get("body"), state=b.get("state"),
                           state_reason=b.get("state_reason"), labels=labels)
    return req.render.issue(req.repo, issue)


@route("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/labels", ISSUE_W)
def set_labels(req: Req):
    issue = _issue(req)
    body = req.body
    raw = body.get("labels", []) if isinstance(body, dict) else body or []
    req.m.update_issue(req.repo, issue, req.login, labels=[x["name"] if isinstance(x, dict) else x for x in raw])
    return req.render.labels(req.repo, issue["labels"])


# -- sub-issues and dependencies ---------------------------------------------------------------------
def _plain_issue(req: Req) -> dict:
    issue = _issue(req)
    if issue.get("pull"):
        raise req.not_found()
    return issue


def _issues_by_id(req: Req, ids: list[int]) -> list[dict]:
    out = []
    for i in ids:
        found = req.m.issue_by_id(i)
        if found is not None and (req.actor is None or req.m.store.can_see(req.actor, found[0]["full_name"])):
            out.append(req.render.issue(*found))
    return out


@route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issues", ISSUES_R)
def list_sub_issues(req: Req):
    return paginate(req, _issues_by_id(req, _plain_issue(req).get("sub_issues") or []))


@route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issues", ISSUES_W)
def add_sub_issue(req: Req):
    parent = _plain_issue(req)
    req.m.add_sub_issue(req.repo, parent, req.body["sub_issue_id"], bool(req.body.get("replace_parent")), req.login)
    return Resp(201, req.render.issue(req.repo, parent),
                {"Location": f"{req.render.api}/repos/{req.repo['full_name']}/issues/{parent['number']}/sub_issues"})


@route("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issue", ISSUES_W)
def remove_sub_issue(req: Req):
    parent = _plain_issue(req)
    if not req.m.remove_sub_issue(req.repo, parent, req.body["sub_issue_id"], req.login):
        raise req.not_found()
    return Resp(200, req.render.issue(req.repo, parent),
                {"Location": f"{req.render.api}/repos/{req.repo['full_name']}/issues/{parent['number']}/sub_issue"})


@route("PATCH", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issues/priority", ISSUES_W)
def reprioritize_sub_issue(req: Req):
    parent = _plain_issue(req)
    b = req.body
    req.m.reprioritize_sub_issue(parent, b["sub_issue_id"], b.get("after_id"), b.get("before_id"))
    return req.render.issue(req.repo, parent)


@route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by", ISSUES_R)
def list_blocked_by(req: Req):
    return paginate(req, _issues_by_id(req, _plain_issue(req).get("blocked_by") or []))


@route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by", ISSUES_W)
def add_blocked_by(req: Req):
    issue = _plain_issue(req)
    req.m.add_blocked_by(req.repo, issue, req.body["issue_id"], req.login)
    return Resp(201, req.render.issue(req.repo, issue), {
        "Location": f"{req.render.api}/repos/{req.repo['full_name']}/issues/{issue['number']}/dependencies/blocked_by"})


@route("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by/{issue_id}", ISSUES_W)
def remove_blocked_by(req: Req):
    issue = _plain_issue(req)
    if not req.m.remove_blocked_by(req.repo, issue, req.params["issue_id"], req.login):
        raise req.not_found()
    return req.render.issue(req.repo, issue)


@route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocking", ISSUES_R)
def list_blocking(req: Req):
    issue = _plain_issue(req)
    return paginate(req, [req.render.issue(r, i) for r, i in req.m.blocking_of(issue)
                          if req.actor is None or req.m.store.can_see(req.actor, r["full_name"])])


@route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/comments", ISSUE_R)
def list_issue_comments(req: Req):
    issue = _issue(req)
    since = req.query.get("since")
    items = [c for c in req.repo["comments"].values() if c["issue"] == issue["number"]
             and (not since or c["updated_at"] >= since)]
    return paginate(req, [req.render.comment(req.repo, c) for c in _by_created(items)])


@route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments", ISSUE_W)
def create_issue_comment(req: Req):
    issue = _issue(req)
    c = req.m.add_comment(req.repo, issue, req.login, req.body["body"])
    return Resp(201, req.render.comment(req.repo, c),
                {"Location": f"{req.render.api}/repos/{req.repo['full_name']}/issues/comments/{c['id']}"})


def _comment(req: Req) -> dict:
    c = req.repo["comments"].get(str(req.params["comment_id"]))
    if c is None:
        raise req.not_found()
    _may_touch(req, req.repo["issues"][str(c["issue"])])
    return c


@route("GET", "/repos/{owner}/{repo}/issues/comments/{comment_id}", ISSUE_R)
def get_issue_comment(req: Req):
    return req.render.comment(req.repo, _comment(req))


@route("PATCH", "/repos/{owner}/{repo}/issues/comments/{comment_id}", ISSUE_W)
def edit_issue_comment(req: Req):
    c = _comment(req)
    if req.actor.kind == "installation" and c["user"].lower() != req.login.lower() and \
            not _allowed(req.actor, ({"issues": "write"},)):
        raise req.fail(403, FORBIDDEN)
    req.m.edit_comment(req.repo, c, req.body["body"], req.login)
    return req.render.comment(req.repo, c)


# -- pull requests ---------------------------------------------------------------------------------
@route("GET", "/repos/{owner}/{repo}/pulls", PR_R)
def list_pulls(req: Req):
    q = req.query
    state = q.get("state", "open")
    items = [i for i in req.m.pulls(req.repo) if state == "all" or i["state"] == state]
    if q.get("head"):
        owner, _, branch = q["head"].rpartition(":")
        items = [i for i in items if i["pull"]["head_ref"] == branch
                 and (not owner or owner.lower() == req.repo["owner"].lower())]
    if q.get("base"):
        items = [i for i in items if i["pull"]["base_ref"] == q["base"]]
    sort = q.get("sort", "created")
    key = "updated_at" if sort in ("updated", "long-running") else "created_at"
    desc = q.get("direction", "desc" if sort == "created" or "sort" not in q else "asc")
    items.sort(key=lambda i: (i[key], i["number"]), reverse=desc == "desc")
    return paginate(req, [req.render.pull(req.repo, i, full=False) for i in items])


@route("POST", "/repos/{owner}/{repo}/pulls", PR_W)
def create_pull(req: Req):
    body = req.body
    if not body.get("title") and not body.get("issue"):
        raise validation_failed("PullRequest", "title is required", req.op.docs, "title", "missing_field")
    issue = req.m.open_pr(req.repo, req.login, body["head"], body["base"], body.get("title") or "", body.get("body"),
                          bool(body.get("draft")), req.op.docs)
    return Resp(201, req.render.pull(req.repo, issue),
                {"Location": f"{req.render.api}/repos/{req.repo['full_name']}/pulls/{issue['number']}"})


@route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}", PR_R)
def get_pull(req: Req):
    issue = _pull(req)
    accept = req.headers.get("accept", "")
    if "diff" in accept or "patch" in accept:
        info = req.m.pr_info(req.repo, issue)
        path = req.m.path(req.repo)
        if "patch" in accept:
            text = gitrepo.format_patch(path, info["base_sha"], info["head_sha"])
            media = "application/vnd.github.patch"
        else:
            text = gitrepo.diff(path, info["base_sha"], info["head_sha"])
            media = "application/vnd.github.diff"
        return Resp(200, text.encode(), {"Content-Type": f"{media}; charset=utf-8"})
    return req.render.pull(req.repo, issue)


@route("PATCH", "/repos/{owner}/{repo}/pulls/{pull_number}", PR_W)
def update_pull(req: Req):
    issue = _pull(req)
    b = req.body or {}
    req.m.update_pr(req.repo, issue, req.login, title=b.get("title"), body=b.get("body"), state=b.get("state"),
                    base=b.get("base"), docs=req.op.docs)
    return req.render.pull(req.repo, issue)


@route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/commits", PR_R)
def pull_commits(req: Req):
    issue = _pull(req)
    info = req.m.pr_info(req.repo, issue)
    shas = gitrepo.range_commits(req.m.path(req.repo), info["base_sha"], info["head_sha"])
    return paginate(req, [req.render.commit(req.repo, s) for s in shas])


@route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/files", PR_R)
def pull_files(req: Req):
    issue = _pull(req)
    info = req.m.pr_info(req.repo, issue)
    changes = gitrepo.files(req.m.path(req.repo), info["base_sha"], info["head_sha"])
    return paginate(req, [req.render.diff_entry(req.repo, info["head_sha"], f) for f in changes])


@route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/merge", PR_R)
def is_merged(req: Req):
    issue = _pull(req)
    if not issue["pull"]["merged"]:
        raise req.not_found()
    return Resp(204)


@route("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge", MERGE)
def merge_pull(req: Req):
    issue = _pull(req)
    b = req.body or {}
    method = b.get("merge_method", "merge")
    sha = req.m.merge_pr(req.repo, issue, req.login, method, b.get("sha"), b.get("commit_title"),
                         b.get("commit_message"), req.op.docs)
    return {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}


@route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/comments", PR_R)
def list_review_comments(req: Req):
    issue = _pull(req)
    items = [c for c in req.repo["review_comments"].values() if c["pr"] == issue["number"]]
    reverse = req.query.get("direction") == "desc"
    items = sorted(_by_created(items), key=lambda c: (c["created_at"], c["id"]), reverse=reverse)
    return paginate(req, [req.render.review_comment(req.repo, c) for c in items])


@route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/comments", PR_W)
def create_review_comment(req: Req):
    issue = _pull(req)
    b = req.body
    if b.get("in_reply_to"):
        c = req.m.add_review_comment(req.repo, issue, req.login, b["body"], commit_id=None, path=None, line=None,
                                     in_reply_to=b["in_reply_to"], docs=req.op.docs)
    else:
        line = b.get("line") or b.get("position")
        c = req.m.add_review_comment(req.repo, issue, req.login, b["body"], commit_id=b["commit_id"], path=b["path"],
                                     line=line, side=b.get("side", "RIGHT"), start_line=b.get("start_line"),
                                     subject_type=b.get("subject_type", "line"), docs=req.op.docs)
    return Resp(201, req.render.review_comment(req.repo, c))


@route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies", PR_W)
def reply_review_comment(req: Req):
    issue = _pull(req)
    c = req.m.add_review_comment(req.repo, issue, req.login, req.body["body"], commit_id=None, path=None, line=None,
                                 in_reply_to=req.params["comment_id"], docs=req.op.docs)
    return Resp(201, req.render.review_comment(req.repo, c))


@route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews", PR_R)
def list_reviews(req: Req):
    issue = _pull(req)
    items = sorted((r for r in req.repo["reviews"].values() if r["pr"] == issue["number"]), key=lambda r: r["id"])
    return paginate(req, [req.render.review(req.repo, r) for r in items])


@route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews", PR_W)
def create_review(req: Req):
    issue = _pull(req)
    b = req.body or {}
    review = req.m.add_review(req.repo, issue, req.login, b.get("body"), b.get("event"), b.get("commit_id"),
                              b.get("comments") or [], req.op.docs)
    return req.render.review(req.repo, review)


# -- search ------------------------------------------------------------------------------------------
@route("GET", "/search/issues")
def search_issues(req: Req):
    m, actor = req.m, req.actor
    try:
        terms = shlex.split(req.query["q"])
    except ValueError:
        terms = req.query["q"].split()
    quals: dict[str, list[str]] = {}
    words = []
    for t in terms:
        k, sep, v = t.partition(":")
        if sep and k in ("repo", "is", "type", "state", "label", "head", "base", "author", "in", "org", "user"):
            quals.setdefault(k, []).append(v)
        else:
            words.append(t.lower())
    if not quals.get("repo") and not quals.get("org") and not quals.get("user") and not words:
        raise validation_failed("Search", "The search is longer than 256 characters." if len(req.query["q"]) > 256
                                else "Must include at least one user, organization, or repository", req.op.docs,
                                "q", "invalid")
    kinds = set(quals.get("is", []) + quals.get("type", []))
    items = []
    for repo in m.store.data["repos"].values():
        if not m.store.can_see(actor, repo["full_name"]):
            continue
        if quals.get("repo") and repo["full_name"].lower() not in [r.lower() for r in quals["repo"]]:
            continue
        owners = [o.lower() for o in quals.get("org", []) + quals.get("user", [])]
        if owners and repo["owner"].lower() not in owners:
            continue
        for issue in repo["issues"].values():
            pr = issue.get("pull")
            if ("pr" in kinds or "pull-request" in kinds) and not pr:
                continue
            if "issue" in kinds and pr:
                continue
            if "open" in kinds or "open" in quals.get("state", []):
                if issue["state"] != "open":
                    continue
            if "closed" in kinds or "closed" in quals.get("state", []):
                if issue["state"] != "closed":
                    continue
            if "merged" in kinds and not (pr and pr["merged"]):
                continue
            if any(lab.lower() not in [x.lower() for x in issue["labels"]] for lab in quals.get("label", [])):
                continue
            if quals.get("head") and (not pr or pr["head_ref"] not in quals["head"]):
                continue
            if quals.get("base") and (not pr or pr["base_ref"] not in quals["base"]):
                continue
            if quals.get("author") and issue["user"].lower() not in [a.lower() for a in quals["author"]]:
                continue
            text = " ".join([issue["title"], issue.get("body") or "", pr["head_ref"] if pr else ""]).lower()
            if any(w not in text for w in words):
                continue
            items.append((repo, issue))
    items.sort(key=lambda ri: (ri[1]["created_at"], ri[1]["number"]), reverse=True)
    rendered = [{**req.render.issue(repo, issue), "score": 1.0} for repo, issue in items]
    return paginate(req, rendered, "items", {"incomplete_results": False})


# -- checks and statuses ---------------------------------------------------------------------------------
def _sha(req: Req, param: str = "ref") -> str:
    sha = gitrepo.resolve(req.m.path(req.repo), req.params[param])
    if sha is None:
        raise req.fail(422, f"No commit found for SHA: {req.params[param]}")
    return sha


@route("GET", "/repos/{owner}/{repo}/commits/{ref}/check-runs", CHECKS_R)
def commit_check_runs(req: Req):
    sha = _sha(req)
    runs = [c for c in req.repo["check_runs"].values() if c["head_sha"] == sha]
    if req.query.get("check_name"):
        runs = [c for c in runs if c["name"] == req.query["check_name"]]
    if req.query.get("status"):
        runs = [c for c in runs if c["status"] == req.query["status"]]
    if req.query.get("filter", "latest") == "latest":
        latest: dict[str, dict] = {}
        for c in runs:
            if c["name"] not in latest or c["id"] > latest[c["name"]]["id"]:
                latest[c["name"]] = c
        runs = list(latest.values())
    runs.sort(key=lambda c: c["id"], reverse=True)
    return paginate(req, [req.render.check_run(req.repo, c) for c in runs], "check_runs")


@route("GET", "/repos/{owner}/{repo}/commits/{ref}/check-suites", CHECKS_R)
def commit_check_suites(req: Req):
    sha = _sha(req)
    suites = sorted((s for s in req.repo["check_suites"].values() if s["head_sha"] == sha), key=lambda s: s["id"])
    return paginate(req, [req.render.check_suite(req.repo, s) for s in suites], "check_suites")


@route("GET", "/repos/{owner}/{repo}/check-runs/{check_run_id}", CHECKS_R)
def get_check_run(req: Req):
    c = req.repo["check_runs"].get(str(req.params["check_run_id"]))
    if c is None:
        raise req.not_found()
    return req.render.check_run(req.repo, c)


@route("GET", "/repos/{owner}/{repo}/check-suites/{check_suite_id}", CHECKS_R)
def get_check_suite(req: Req):
    s = req.repo["check_suites"].get(str(req.params["check_suite_id"]))
    if s is None:
        raise req.not_found()
    return req.render.check_suite(req.repo, s)


@route("POST", "/repos/{owner}/{repo}/statuses/{sha}", STATUS_W)
def create_status(req: Req):
    path = req.m.path(req.repo)
    sha = req.params["sha"]
    if gitrepo.object_type(path, sha) != "commit":
        raise req.fail(422, f"No commit found for SHA: {sha}")
    b = req.body
    now = iso()
    s = {"id": req.m.store.next_id(), "sha": gitrepo.resolve(path, sha), "state": b["state"],
         "context": b.get("context") or "default", "description": b.get("description"),
         "target_url": b.get("target_url"), "creator": req.login, "created_at": now, "updated_at": now}
    req.repo["statuses"].append(s)
    req.m.store.save()
    return Resp(201, req.render.status(req.repo, s))


@route("GET", "/repos/{owner}/{repo}/commits/{ref}/statuses", STATUS_R)
def commit_statuses(req: Req):
    sha = _sha(req)
    items = sorted((s for s in req.repo["statuses"] if s["sha"] == sha), key=lambda s: s["id"], reverse=True)
    return paginate(req, [req.render.status(req.repo, s) for s in items])


@route("GET", "/repos/{owner}/{repo}/commits/{ref}/status", STATUS_R)
def combined_status(req: Req):
    sha = _sha(req)
    latest = req.m.ci.latest_statuses(req.repo, sha)
    states = {s["state"] for s in latest}
    state = "failure" if states & {"failure", "error"} else "pending" if "pending" in states or not latest else "success"
    r, fn = req.render, req.repo["full_name"]
    return {"state": state, "sha": sha, "total_count": len(latest), "statuses": [r.status(req.repo, s) for s in latest],
            "repository": r.repo(req.repo), "commit_url": f"{r.api}/repos/{fn}/commits/{sha}",
            "url": f"{r.api}/repos/{fn}/commits/{sha}/status"}


# -- Actions -------------------------------------------------------------------------------------------
def _run(req: Req) -> dict:
    run = req.repo["workflow_runs"].get(str(req.params["run_id"]))
    if run is None:
        raise req.not_found()
    return run


@route("GET", "/repos/{owner}/{repo}/actions/runs", ACTIONS_R)
def list_runs(req: Req):
    q = req.query
    runs = list(req.repo["workflow_runs"].values())
    if q.get("branch"):
        runs = [r for r in runs if r["head_branch"] == q["branch"]]
    if q.get("event"):
        runs = [r for r in runs if r["event"] == q["event"]]
    if q.get("head_sha"):
        runs = [r for r in runs if r["head_sha"] == q["head_sha"]]
    if q.get("actor"):
        runs = [r for r in runs if r["actor"].lower() == q["actor"].lower()]
    if q.get("check_suite_id"):
        runs = [r for r in runs if r["suite"] == q["check_suite_id"]]
    status = q.get("status")
    if status:
        runs = [r for r in runs if r["status"] == status or r.get("conclusion") == status]
    runs.sort(key=lambda r: r["id"], reverse=True)
    return paginate(req, [req.render.workflow_run(req.repo, r) for r in runs], "workflow_runs")


@route("GET", "/repos/{owner}/{repo}/actions/runs/{run_id}", ACTIONS_R)
def get_run(req: Req):
    return req.render.workflow_run(req.repo, _run(req))


@route("GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", ACTIONS_R)
def run_jobs(req: Req):
    run = _run(req)
    if req.query.get("filter") == "all":
        jobs = sorted((j for j in req.repo["jobs"].values() if j["run_id"] == run["id"]), key=lambda j: j["id"])
    else:
        jobs = [req.repo["jobs"][str(j)] for j in run["jobs"]]
    return paginate(req, [req.render.job(req.repo, j) for j in jobs], "jobs")


def _job(req: Req) -> dict:
    job = req.repo["jobs"].get(str(req.params["job_id"]))
    if job is None:
        raise req.not_found()
    return job


@route("GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}", ACTIONS_R)
def get_job(req: Req):
    return req.render.job(req.repo, _job(req))


def log_signature(m, full: str, job_id: int, expires: int) -> str:
    key = (m.store.app.get("public_key") or "mock").encode()
    return hmac.new(key, f"{full.lower()}:{job_id}:{expires}".encode(), hashlib.sha256).hexdigest()


@route("GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", ACTIONS_R)
def job_logs(req: Req):
    job = _job(req)
    expires = int(time.time()) + 60
    full = req.repo["full_name"]
    sig = log_signature(req.m, full, job["id"], expires)
    url = f"{req.render.base}/_mock/job-logs/{full}/{job['id']}?{urlencode({'expires': expires, 'sig': sig})}"
    return Resp(302, None, {"Location": url})


@route("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun", ACTIONS_W)
def rerun(req: Req):
    run = _run(req)
    if not req.m.ci.rerun(req.repo, run, req.login, failed_only=False):
        raise req.fail(403, "This workflow is already running")
    return Resp(201, {})


@route("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs", ACTIONS_W)
def rerun_failed(req: Req):
    run = _run(req)
    if not req.m.ci.rerun(req.repo, run, req.login, failed_only=True):
        raise req.fail(403, "This workflow is already running")
    return Resp(201, {})


@route("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/cancel", ACTIONS_W)
def cancel(req: Req):
    run = _run(req)
    if not req.m.ci.cancel(req.repo, run, req.login):
        raise req.fail(409, "Cannot cancel a workflow run that is completed.")
    return Resp(202, {})


# -- branch protection ---------------------------------------------------------------------------------
def _protection(req: Req, rule: dict) -> dict:
    r, fn, branch = req.render, req.repo["full_name"], req.params["branch"]
    url = f"{r.api}/repos/{fn}/branches/{branch}/protection"
    out: dict = {"url": url}
    checks = rule.get("required_status_checks")
    if checks is not None:
        out["required_status_checks"] = {"url": f"{url}/required_status_checks", "strict": bool(checks.get("strict")),
                                         "contexts": list(checks.get("contexts") or []),
                                         "contexts_url": f"{url}/required_status_checks/contexts",
                                         "checks": [{"context": c, "app_id": None} for c in checks.get("contexts") or []]}
    out["enforce_admins"] = {"url": f"{url}/enforce_admins", "enabled": bool(rule.get("enforce_admins"))}
    if rule.get("required_pull_request_reviews") is not None:
        rv = rule["required_pull_request_reviews"]
        out["required_pull_request_reviews"] = {
            "url": f"{url}/required_pull_request_reviews", "dismiss_stale_reviews": bool(rv.get("dismiss_stale_reviews")),
            "require_code_owner_reviews": bool(rv.get("require_code_owner_reviews")),
            "required_approving_review_count": int(rv.get("required_approving_review_count") or 0)}
    return out


@route("GET", "/repos/{owner}/{repo}/branches/{branch}/protection", ADMIN_R)
def get_protection(req: Req):
    rule = req.repo["protection"].get(req.params["branch"])
    if rule is None:
        raise req.fail(404, "Branch not protected")
    return _protection(req, rule)


@route("PUT", "/repos/{owner}/{repo}/branches/{branch}/protection", ADMIN_W)
def put_protection(req: Req):
    branch = req.params["branch"]
    if gitrepo.branch_sha(req.m.path(req.repo), branch) is None:
        raise req.fail(404, "Branch not found")
    b = req.body
    checks = b.get("required_status_checks")
    if checks is not None and checks.get("checks") and not checks.get("contexts"):
        checks = {**checks, "contexts": [c["context"] for c in checks["checks"]]}
    rule = {"required_status_checks": checks, "enforce_admins": bool(b.get("enforce_admins")),
            "required_pull_request_reviews": b.get("required_pull_request_reviews"),
            "restrictions": b.get("restrictions")}
    req.repo["protection"][branch] = rule
    req.m.store.save()
    return _protection(req, rule)


@route("DELETE", "/repos/{owner}/{repo}/branches/{branch}/protection", ADMIN_W)
def delete_protection(req: Req):
    if req.repo["protection"].pop(req.params["branch"], None) is None:
        raise req.fail(404, "Branch not protected")
    req.m.store.save()
    return Resp(204)
