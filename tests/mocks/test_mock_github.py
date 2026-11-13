"""GitHub mock (scripts/mocks/github): Symphony's real GitHub clients against it, end to end.

App tokens are minted by `ghapp.InstallationCredential` through the real HTTP exchange, git runs
through the real credential helper, and the review/train calls go through `merger.github.RestGitHub`.
The mock checks every request against GitHub's REST description and GraphQL schema and every
response against the documented response schema; the last test asserts no violation was seen
and that every served route was exercised.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt
import pytest
from scripts.mocks.github import rest
from scripts.mocks.github.server import MockServer

from symphony import codehost, ghapp
from symphony.api import webhooks
from symphony.merger.github import RestGitHub
from symphony.reflector import HttpxGitHub

SECRET = "mock-webhook-secret"
OWNER = "acme"


class _Receiver(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).got.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def receiver():
    _Receiver.got = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _seed(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return str(root)


@pytest.fixture(scope="module")
def gh(tmp_path_factory, receiver):
    tmp = tmp_path_factory.mktemp("gh")
    srv = MockServer(str(tmp / "state"), port=0, https_port=0,
                     webhook_url=f"http://127.0.0.1:{receiver.server_address[1]}/v1/webhooks/github",
                     webhook_secret=SECRET).start()
    m = srv.mock
    m.create_repo(OWNER, "widgets", seed=_seed(tmp / "seed-w", {
        "README.md": "# widgets\n", "app.py": "def answer():\n    return 41\n", "notes.txt": "one\ntwo\nthree\n"}))
    m.create_repo(OWNER, "gadgets")
    m.create_repo(OWNER, "secret")
    m.create_repo("other", "elsewhere")
    m.create_repo(OWNER, "tested", seed=_seed(tmp / "seed-t", {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"}))
    m.store.install(OWNER, [f"{OWNER}/widgets", f"{OWNER}/gadgets", f"{OWNER}/tested"], None)
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def app(gh):
    data = httpx.get(f"{gh.url}/_mock/app").json()
    return {"id": str(data["id"]), "key": data["private_key"], "installation": data["env"]["GITHUB_APP_INSTALLATION_ID"]}


@pytest.fixture(autouse=True)
def endpoints(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", gh.url)
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"{gh.url}/graphql")
    monkeypatch.setenv("GITHUB_SERVER_URL", gh.url)


def _cred(app, repos=(), permissions=None) -> ghapp.InstallationCredential:
    return ghapp.InstallationCredential(app["id"], app["key"], app["installation"],
                                        now_ms=lambda: int(time.time() * 1000), exchange=ghapp._http_exchange,
                                        target_repos=repos, permissions=permissions)


def _person(gh, login: str) -> str:
    return httpx.post(f"{gh.url}/_mock/users", json={"login": login}).json()["token"]


def _api(gh, token: str, method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(method, f"{gh.url}{path}", headers={"Authorization": f"Bearer {token}",
                                                              "Accept": "application/vnd.github+json"}, **kw)


def _drain(gh) -> None:
    assert httpx.post(f"{gh.url}/_mock/ci/drain", json={"timeout_s": 60}, timeout=90).json()["idle"]


def _git(*args: str, cwd: str | None = None, env: dict | None = None, check: bool = True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          # isolated from the host's git config: a keychain helper there would answer
                          # (and store) credentials before Symphony's helper is asked
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Agent",
                               "GIT_AUTHOR_EMAIL": "agent@example.com", "GIT_COMMITTER_NAME": "Agent",
                               "GIT_COMMITTER_EMAIL": "agent@example.com", **(env or {})})
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stdout}{proc.stderr}")
    return proc


def _helper(tmp: Path, token: str) -> str:
    """Symphony's own credential helper, reading the token from a file."""
    token_file = tmp / "gh-token"
    token_file.write_text(token + "\n")
    helper = tmp / "git-credential-symphony"
    helper.write_text(ghapp.credential_helper_script(str(token_file)))
    helper.chmod(0o700)
    return str(helper)


def _clone(gh, tmp: Path, repo: str, token: str, dest: str = "work"):
    helper = _helper(tmp, token)
    target = tmp / dest
    proc = _git("-c", "credential.helper=", "-c", f"credential.helper={helper}", "clone", "-q",
                f"{gh.url}/{repo}.git", str(target), check=False)
    return proc, target, helper


def _push_branch(gh, tmp: Path, token: str, branch: str, files: dict[str, str], repo: str = "acme/widgets",
                 message: str = "change") -> str:
    proc, work, helper = _clone(gh, tmp, repo, token, dest=f"w-{branch}")
    assert proc.returncode == 0, proc.stderr
    _git("checkout", "-q", "-b", branch, cwd=str(work))
    for name, text in files.items():
        (work / name).write_text(text)
    _git("add", "-A", cwd=str(work))
    _git("commit", "-q", "-m", message, cwd=str(work))
    _git("-c", f"credential.helper={helper}", "push", "-q", "origin", branch, cwd=str(work))
    return _git("rev-parse", "HEAD", cwd=str(work)).stdout.strip()


# -- App tokens ------------------------------------------------------------------------------------
def test_installation_credential_mints_scoped_tokens(gh, app):
    full = _cred(app, ["acme/widgets"]).token()
    listed = _api(gh, full, "GET", "/installation/repositories").json()
    assert [r["full_name"] for r in listed["repositories"]] == ["acme/widgets"]
    assert listed["repository_selection"] == "selected"
    # outside the token's repositories: GitHub answers 404, as for a repository that does not exist
    assert _api(gh, full, "GET", "/repos/acme/gadgets").status_code == 404

    both = _cred(app, ["acme/widgets", "acme/gadgets"]).token()
    names = {r["full_name"] for r in _api(gh, both, "GET", "/installation/repositories").json()["repositories"]}
    assert names == {"acme/widgets", "acme/gadgets"}

    ro = _cred(app, ["acme/widgets"], ghapp.READ_ONLY).token()
    repo = _api(gh, ro, "GET", "/repos/acme/widgets").json()
    assert repo["permissions"]["push"] is False and repo["permissions"]["pull"] is True


def test_installation_credential_refusals(gh, app):
    with pytest.raises(ghapp.NotAuthorizedForRepo):
        _cred(app, ["acme/secret"]).token()  # the App is not installed there
    with pytest.raises(ghapp.NotAuthorizedForRepo):
        _cred(app, ["acme/widgets", "other/elsewhere"]).token()  # no one installation covers both owners
    with pytest.raises(ghapp.CredentialError, match="422"):
        _cred(app, (), {"administration": "write"}).token()  # more than the installation was granted


def test_app_jwt_rules(gh, app):
    url = f"{gh.url}/app/installations/{app['installation']}/access_tokens"
    now = int(time.time())

    def mint(claims, key=app["key"]):
        return httpx.post(url, headers={"Authorization": f"Bearer {jwt.encode(claims, key, algorithm='RS256')}"})

    assert mint({"iat": now - 60, "exp": now + 540, "iss": app["id"]}).status_code == 201
    too_long = mint({"iat": now - 60, "exp": now + 900, "iss": app["id"]})
    assert too_long.status_code == 401 and "too far in the future" in too_long.json()["message"]
    assert mint({"iat": now - 900, "exp": now - 300, "iss": app["id"]}).status_code == 401
    assert mint({"iat": now - 60, "exp": now + 500, "iss": "999"}).status_code == 401
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    bad = mint({"iat": now - 60, "exp": now + 500, "iss": app["id"]}, other)
    assert bad.status_code == 401 and bad.json()["documentation_url"].startswith("https://docs.github.com/")
    assert httpx.get(f"{gh.url}/app", headers={"Authorization": "Bearer ghs_nope"}).status_code == 401


# -- git -------------------------------------------------------------------------------------------
def test_git_clone_and_push_with_the_credential_helper(gh, app, tmp_path):
    token = _cred(app, ["acme/widgets"]).token()
    sha = _push_branch(gh, tmp_path, token, "SYM-1", {"feature.txt": "hello\n"})
    assert _api(gh, token, "GET", "/repos/acme/widgets/git/ref/heads/SYM-1").json()["object"]["sha"] == sha

    ro = _cred(app, ["acme/widgets"], ghapp.READ_ONLY).token()
    (tmp_path / "ro").mkdir()
    proc, work, helper = _clone(gh, tmp_path / "ro", "acme/widgets", ro)
    assert proc.returncode == 0, proc.stderr
    _git("checkout", "-q", "-b", "SYM-ro", cwd=str(work))
    (work / "x.txt").write_text("x\n")
    _git("add", "-A", cwd=str(work))
    _git("commit", "-q", "-m", "nope", cwd=str(work))
    push = _git("-c", f"credential.helper={helper}", "push", "origin", "SYM-ro", cwd=str(work), check=False)
    assert push.returncode != 0 and "403" in push.stderr
    assert "refs/heads/SYM-ro" not in _git("ls-remote", f"{gh.url}/acme/widgets.git", cwd=str(work),
                                           env={"GIT_ASKPASS": "true"}, check=False).stdout

    # a repository outside the token is "not found", like on GitHub
    (tmp_path / "scope").mkdir()
    proc, _, _ = _clone(gh, tmp_path / "scope", "acme/gadgets", token)
    assert proc.returncode != 0 and "not found" in proc.stderr.lower()


def test_https_port_with_private_ca(gh, app, tmp_path):
    token = _cred(app, ["acme/widgets"]).token()
    verify = ssl.create_default_context(cafile=gh.ca_path)
    resp = httpx.get(f"{gh.https_url}/api/v3/repos/acme/widgets", verify=verify,
                     headers={"Authorization": f"token {token}"})
    assert resp.status_code == 200
    assert resp.json()["url"] == f"{gh.https_url}/api/v3/repos/acme/widgets"
    # GitHub Enterprise's GraphQL path, as `gh` uses it for a non-github.com host
    viewer = httpx.post(f"{gh.https_url}/api/graphql", verify=verify, headers={"Authorization": f"bearer {token}"},
                        json={"query": "{ viewer { login } }"}).json()
    assert viewer == {"data": {"viewer": {"login": "symphony"}}}
    helper = _helper(tmp_path, token)
    proc = _git("-c", f"http.sslCAInfo={gh.ca_path}", "-c", f"credential.helper={helper}", "clone", "-q",
                f"{gh.https_url}/acme/widgets.git", str(tmp_path / "tls"), check=False)
    assert proc.returncode == 0, proc.stderr


# -- the implementer's PR, the review watch and the merger ------------------------------------------
def test_open_review_and_rebase_merge(gh, app, tmp_path):
    cred = _cred(app, ["acme/widgets"])
    token = cred.token()
    head = _push_branch(gh, tmp_path, token, "SYM-2", {"app.py": "def answer():\n    return 42\n"},
                        message="SYM-2: fix the answer")
    # the implementer: a ready PR with the symphony label, via REST
    pr = _api(gh, token, "POST", "/repos/acme/widgets/pulls",
              json={"title": "SYM-2: fix the answer", "head": "SYM-2", "base": "main", "body": "Fixes it."})
    assert pr.status_code == 201, pr.text
    n = pr.json()["number"]
    assert pr.json()["head"]["sha"] == head and pr.json()["mergeable"] is True
    assert _api(gh, token, "POST", f"/repos/acme/widgets/issues/{n}/labels", json={"labels": ["symphony"]}).status_code == 200

    gh_ = RestGitHub("acme/widgets", token=cred.token)
    listed = {p["number"]: p for p in gh_.list_open()}
    assert listed[n]["labels"] == ["symphony"] and listed[n]["head_sha"] == head and listed[n]["author"] == "symphony"
    assert gh_.pr(n)["mergeable"] == "MERGEABLE"

    # a person asks on the PR in all three places; the review watch sees them as human comments
    alice = _person(gh, "alice")
    assert _api(gh, alice, "POST", f"/repos/acme/widgets/issues/{n}/comments", json={"body": "Why 42?"}).status_code == 201
    rc = _api(gh, alice, "POST", f"/repos/acme/widgets/pulls/{n}/comments",
              json={"body": "nit: name it", "commit_id": head, "path": "app.py", "line": 2, "side": "RIGHT"})
    assert rc.status_code == 201, rc.text
    assert _api(gh, alice, "POST", f"/repos/acme/widgets/pulls/{n}/comments/{rc.json()['id']}/replies",
                json={"body": "also a doc string"}).status_code == 201
    assert _api(gh, alice, "POST", f"/repos/acme/widgets/pulls/{n}/reviews",
                json={"body": "Looks right", "event": "COMMENT"}).status_code == 200
    httpx.post(f"{gh.url}/_mock/comment", json={"repo": "acme/widgets", "number": n, "user": "bob",
                                                "body": "Ship it?"}).raise_for_status()

    reader = codehost.GitHubReader(lambda repo: cred.token())
    assert n in [p["number"] for p in reader.open_pulls("acme/widgets")]
    convo = reader.conversation("acme/widgets", n)
    from symphony import prwatch

    humans = {c["body"] for c in prwatch.human_comments(convo)}
    assert {"Why 42?", "nit: name it", "also a doc string", "Looks right", "Ship it?"} <= humans

    discussion = gh_.discussion(n)
    assert [d["where"] for d in discussion if d["who"] == "alice"][:3] == ["PR", "on app.py:2", "on app.py:2"]

    # the merger's own conversation calls
    url = gh_.comment(n, "<!-- symphony-merger --> reviewing")
    assert url.endswith(f"/acme/widgets/pull/{n}#issuecomment-{url.rsplit('-', 1)[-1]}")
    gh_.edit_comment(url, "<!-- symphony-merger --> reviewed")
    gh_.add_labels(n, ["reviewed", "wait-ci"])
    gh_.remove_label(n, "wait-ci")
    gh_.remove_label(n, "never-there")  # 404 tolerated
    assert set(gh_.pr(n)["labels"]) == {"symphony", "reviewed"}
    gh_.set_draft(n, True)
    assert gh_.pr(n)["draft"] is True
    gh_.set_draft(n, False)
    ready = gh_.pr(n)
    # the ready-for-review time is the timeline event's, later than the PR's creation
    assert ready["draft"] is False and ready["ready_at"] >= listed[n]["ready_at"]

    _drain(gh)
    checks = gh_.check_runs(head)
    assert checks and {c["name"] for c in checks} == {"test"}
    assert all(c["status"] == "completed" and c["conclusion"] == "success" for c in checks)
    runs = gh_.workflow_runs(sha=head)
    assert runs and runs[0]["event"] == "pull_request" and runs[0]["sha"] == head
    run = gh_.run(runs[0]["id"])
    jobs = gh_.run_jobs(run["id"])
    assert jobs[0]["steps"] and "Job test" in gh_.job_log(jobs[0]["id"])
    gh_.cancel(run["id"])  # completed: 409, tolerated
    gh_.rerun(run["id"])
    _drain(gh)
    assert gh_.run(run["id"])["attempt"] == 2
    gh_.rerun_failed(run["id"])
    _drain(gh)

    # the train: merge the tested head only, by rebase; main moves
    ok, message = gh_.merge(n, "0" * 40, "rebase")
    assert not ok and "Head branch was modified" in message
    ok, message = gh_.merge(n, head, "rebase")
    assert ok, message
    merged = gh_.pr(n)
    assert merged["state"] == "merged"
    main = _git("ls-remote", f"{gh.url}/acme/widgets.git", "refs/heads/main",
                env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                     "GIT_CONFIG_VALUE_0": _helper(tmp_path, token)}).stdout.split()[0]
    detail = _api(gh, token, "GET", f"/repos/acme/widgets/pulls/{n}").json()
    assert detail["merged"] is True and detail["merge_commit_sha"] == main != head
    commit = _api(gh, token, "GET", f"/repos/acme/widgets/pulls/{n}/commits").json()[0]
    assert commit["commit"]["author"]["name"] == "Agent"  # rebase keeps authorship
    files = _api(gh, token, "GET", f"/repos/acme/widgets/pulls/{n}/files").json()
    assert files[0]["filename"] == "app.py" and files[0]["status"] == "modified"
    _drain(gh)
    pushes = gh_.workflow_runs(branch="main", event="push", status="completed")
    assert pushes and pushes[0]["sha"] == main and pushes[0]["conclusion"] == "success"


def test_conflicts_protection_and_ci_results(gh, app, tmp_path):
    token = _cred(app, ["acme/widgets"]).token()
    _push_branch(gh, tmp_path, token, "SYM-3a", {"notes.txt": "uno\n"})
    _push_branch(gh, tmp_path, token, "SYM-3b", {"notes.txt": "eins\n"})
    a = _api(gh, token, "POST", "/repos/acme/widgets/pulls", json={"title": "a", "head": "SYM-3a", "base": "main"}).json()
    b = _api(gh, token, "POST", "/repos/acme/widgets/pulls", json={"title": "b", "head": "SYM-3b", "base": "main"}).json()
    gh_ = RestGitHub("acme/widgets", token=lambda: token)
    admin = _person(gh, "maintainer")
    put = _api(gh, admin, "PUT", "/repos/acme/widgets/branches/main/protection", json={
        "required_status_checks": {"strict": False, "contexts": ["test"]}, "enforce_admins": False,
        "required_pull_request_reviews": None, "restrictions": None})
    assert put.status_code == 200, put.text
    assert _api(gh, admin, "GET", "/repos/acme/widgets/branches/main/protection").json()["required_status_checks"][
        "contexts"] == ["test"]
    assert _api(gh, token, "GET", "/repos/acme/widgets/branches/main/protection").status_code == 403

    # CI red on a's head, set through the control API
    httpx.post(f"{gh.url}/_mock/ci", json={"repo": "acme/widgets", "sha": a["head"]["sha"], "conclusion": "failure",
                                           "log": "boom", "rerun": True}).raise_for_status()
    _drain(gh)
    assert gh_.pr(a["number"])["mergeable"] == "MERGEABLE"
    assert _api(gh, token, "GET", f"/repos/acme/widgets/pulls/{a['number']}").json()["mergeable_state"] == "blocked"
    ok, message = gh_.merge(a["number"], a["head"]["sha"], "squash")
    assert not ok and 'Required status check "test"' in message
    httpx.post(f"{gh.url}/_mock/ci", json={"repo": "acme/widgets", "sha": a["head"]["sha"], "clear": True})
    httpx.post(f"{gh.url}/_mock/ci", json={"repo": "acme/widgets", "sha": a["head"]["sha"], "conclusion": "success",
                                           "rerun": True}).raise_for_status()
    _drain(gh)
    ok, message = gh_.merge(a["number"], a["head"]["sha"], "squash")
    assert ok, message
    # b touched the same line: now it conflicts
    assert gh_.pr(b["number"])["mergeable"] == "CONFLICTING"
    ok, message = gh_.merge(b["number"], b["head"]["sha"], "merge")
    assert not ok
    assert _api(gh, admin, "DELETE", "/repos/acme/widgets/branches/main/protection").status_code == 204

    # a red main run with failed steps, as the main watch reads it
    httpx.post(f"{gh.url}/_mock/ci", json={"repo": "acme/widgets", "branch": "main", "conclusion": "failure",
                                           "log": "E   assert 1 == 2"}).raise_for_status()
    admin_push = _push_branch(gh, tmp_path, admin, "tmp-main", {"main.txt": "m\n"})
    _api(gh, admin, "PATCH", "/repos/acme/widgets/git/refs/heads/main", json={"sha": admin_push, "force": True})
    _drain(gh)
    red = gh_.workflow_runs(branch="main", event="push", status="completed")[0]
    assert red["conclusion"] == "failure" and red["sha"] == admin_push
    job = gh_.run_jobs(red["id"])[0]
    assert job["conclusion"] == "failure" and [s["conclusion"] for s in job["steps"]][1] == "failure"
    assert "assert 1 == 2" in gh_.job_log(job["id"])
    httpx.post(f"{gh.url}/_mock/ci", json={"repo": "acme/widgets", "branch": "main", "clear": True})


def test_ci_runs_the_repository_tests(gh, app, tmp_path):
    token = _cred(app, ["acme/tested"]).token()
    head = _push_branch(gh, tmp_path, token, "SYM-4", {"calc.py": "def add(a, b):\n    return a - b\n"},
                        repo="acme/tested")
    pr = _api(gh, token, "POST", "/repos/acme/tested/pulls", json={"title": "SYM-4", "head": "SYM-4", "base": "main"})
    assert pr.status_code == 201
    _drain(gh)
    gh_ = RestGitHub("acme/tested", token=lambda: token)
    [check] = gh_.check_runs(head)
    assert check["conclusion"] == "failure"
    run = gh_.workflow_runs(sha=head)[0]
    assert "assert" in gh_.job_log(gh_.run_jobs(run["id"])[0]["id"])


def test_reflector_reads_and_opens_the_learnings_pr(gh, app, tmp_path):
    token = _cred(app, ["acme/gadgets"]).token()
    client = HttpxGitHub(token, "acme/gadgets")
    assert client.get_file("README.md") == "# gadgets\n"
    assert client.get_file("missing.md") is None
    url = client.open_pr("learnings/SYM-5", "SYM-5: learnings", "What we learned.",
                         {"docs/learnings.md": "- keep PRs small\n", "README.md": "# gadgets\n\nmore\n"},
                         ["learnings"])
    assert url.endswith("/acme/gadgets/pull/1")
    found = client.get_pr_for_issue("SYM-5")
    assert found["number"] == 1 and found["head"]["ref"] == "learnings/SYM-5"
    diff = client.get_pr_diff(1)
    assert "+- keep PRs small" in diff and "diff --git a/README.md" in diff
    assert client.get_review_threads(1) == []
    _drain(gh)
    assert client.get_ci_results(1) == [{"name": "test", "status": "completed", "conclusion": "success"}]
    labels = _api(gh, token, "GET", "/repos/acme/gadgets/issues/1/labels").json()
    assert [x["name"] for x in labels] == ["learnings"]


def test_read_only_token_cannot_write(gh, app, tmp_path):
    full = _cred(app, ["acme/widgets"]).token()
    _push_branch(gh, tmp_path, full, "SYM-6", {"six.txt": "6\n"})
    ro = _cred(app, ["acme/widgets"], ghapp.READ_ONLY).token()
    opened = _api(gh, ro, "POST", "/repos/acme/widgets/pulls", json={"title": "x", "head": "SYM-6", "base": "main"})
    assert opened.status_code == 403 and opened.json()["message"] == rest.FORBIDDEN
    ref = _api(gh, ro, "POST", "/repos/acme/widgets/git/refs", json={"ref": "refs/heads/ro", "sha": "0" * 40})
    assert ref.status_code == 403
    assert _api(gh, ro, "GET", "/repos/acme/widgets/pulls").status_code == 200
    repo_id = RestGitHub("acme/widgets", token=lambda: ro)._graphql(
        "query { repository(owner: \"acme\", name: \"widgets\") { id } }", {})["repository"]["id"]
    out = _api(gh, ro, "POST", "/graphql", json={"query": "mutation($r: ID!) { createPullRequest(input: {repositoryId: $r, "
                                                       "baseRefName: \"main\", headRefName: \"SYM-6\", title: \"x\"}) "
                                                       "{ pullRequest { number } } }", "variables": {"r": repo_id}}).json()
    assert out["errors"][0]["type"] == "FORBIDDEN" and out["data"]["createPullRequest"] is None


def test_off_spec_requests_are_rejected(gh, app):
    token = _cred(app, ["acme/widgets"]).token()
    unknown = _api(gh, token, "GET", "/repos/acme/widgets/no-such-thing")
    assert unknown.status_code == 404 and unknown.json()["message"] == "Not Found"
    assert unknown.json()["documentation_url"].startswith("https://docs.github.com/")
    bad = _api(gh, token, "PUT", "/repos/acme/widgets/pulls/1/merge", json={"merge_method": "fast-forward"})
    assert bad.status_code == 422 and "merge_method" in bad.json()["message"]
    typed = _api(gh, token, "POST", "/repos/acme/widgets/pulls", json={"title": 7, "head": "x", "base": "main"})
    assert typed.status_code == 422 and "is not of type 'string'" in typed.json()["message"]
    assert _api(gh, token, "GET", "/repos/acme/widgets/pulls", params={"state": "sideways"}).status_code == 422
    assert _api(gh, token, "POST", "/repos/acme/widgets/pulls", content=b"{not json").status_code == 400
    wrong_method = _api(gh, token, "DELETE", "/repos/acme/widgets/pulls")
    assert wrong_method.status_code == 404
    gql = _api(gh, token, "POST", "/graphql", json={"query": "{ repository(owner: \"acme\", name: \"widgets\") { nope } }"})
    assert gql.status_code == 200 and "nope" in gql.json()["errors"][0]["message"] and "data" not in gql.json()
    missing = _api(gh, token, "POST", "/graphql", json={"query": "{ repository(owner: \"acme\", name: \"zzz\") { id } }"})
    assert missing.json()["errors"][0]["type"] == "NOT_FOUND" and missing.json()["data"] == {"repository": None}
    paging = _api(gh, token, "POST", "/graphql", json={
        "query": "{ repository(owner: \"acme\", name: \"widgets\") { pullRequests { totalCount } } }"})
    assert "first" in paging.json()["errors"][0]["message"]
    assert httpx.post(f"{gh.url}/graphql", json={"query": "{ viewer { login } }"}).status_code == 401


def test_rest_routes_beyond_symphony_flows(gh, app, tmp_path):
    """The rest of the served surface, so every route's response is checked against its schema."""
    token = _cred(app, ["acme/widgets"]).token()
    admin = _person(gh, "maintainer")
    jwt_token = ghapp.build_assertion(app["id"], app["key"], int(time.time()))
    for path in ("/app", "/app/installations", f"/app/installations/{app['installation']}",
                 "/repos/acme/widgets/installation"):
        assert httpx.get(f"{gh.url}{path}", headers={"Authorization": f"Bearer {jwt_token}"}).status_code == 200, path
    head = _push_branch(gh, tmp_path, token, "SYM-7", {"seven.txt": "7\n"})
    pr = _api(gh, token, "POST", "/repos/acme/widgets/pulls", json={"title": "SYM-7", "head": "SYM-7", "base": "main",
                                                                    "draft": True}).json()
    n = pr["number"]
    calls = [
        ("GET", "/repos/acme/widgets/contents/README.md", None, 200),
        ("GET", f"/repos/acme/widgets/compare/main...{head}", None, 200),
        ("GET", "/repos/acme/widgets/labels", None, 200),
        ("POST", "/repos/acme/widgets/labels", {"name": "train-ci", "color": "fbca04"}, 201),
        ("GET", f"/repos/acme/widgets/issues/{n}", None, 200),
        ("GET", f"/repos/acme/widgets/issues/{n}/comments", None, 200),
        ("GET", f"/repos/acme/widgets/pulls/{n}/reviews", None, 200),
        ("GET", f"/repos/acme/widgets/pulls/{n}/merge", None, 404),
        ("PATCH", f"/repos/acme/widgets/pulls/{n}", {"title": "SYM-7: seven", "body": "now with a body"}, 200),
        ("GET", "/repos/acme/widgets/pulls?state=all&head=acme:SYM-7", None, 200),
        ("GET", "/search/issues?q=repo:acme/widgets+is:pr+SYM-7", None, 200),
        ("POST", f"/repos/acme/widgets/statuses/{head}", {"state": "success", "context": "lint"}, 201),
        ("GET", f"/repos/acme/widgets/commits/{head}/statuses", None, 403),
        ("GET", f"/repos/acme/widgets/commits/{head}/check-suites", None, 200),
    ]
    for method, path, body, status in calls:
        who = admin if method == "POST" and "statuses" in path else token
        resp = _api(gh, who, method, path, json=body)
        assert resp.status_code == status, (method, path, resp.text)
    for path in (f"/repos/acme/widgets/commits/{head}/statuses", f"/repos/acme/widgets/commits/{head}/status"):
        assert _api(gh, admin, "GET", path).status_code == 200
    comment = _api(gh, token, "POST", f"/repos/acme/widgets/issues/{n}/comments", json={"body": "hi"}).json()
    assert _api(gh, token, "GET", f"/repos/acme/widgets/issues/comments/{comment['id']}").json()["body"] == "hi"
    _drain(gh)
    suite = _api(gh, token, "GET", f"/repos/acme/widgets/commits/{head}/check-suites").json()["check_suites"][0]
    assert _api(gh, token, "GET", f"/repos/acme/widgets/check-suites/{suite['id']}").status_code == 200
    check = _api(gh, token, "GET", f"/repos/acme/widgets/commits/{head}/check-runs").json()["check_runs"][0]
    assert check["conclusion"] == "skipped"  # a draft's run is skipped
    assert _api(gh, token, "GET", f"/repos/acme/widgets/check-runs/{check['id']}").status_code == 200
    run = _api(gh, token, "GET", "/repos/acme/widgets/actions/runs", params={"head_sha": head}).json()["workflow_runs"][0]
    job = _api(gh, token, "GET", f"/repos/acme/widgets/actions/runs/{run['id']}/jobs").json()["jobs"][0]
    assert _api(gh, token, "GET", f"/repos/acme/widgets/actions/jobs/{job['id']}").status_code == 200
    # contents API writes and ref moves
    put = _api(gh, token, "PUT", "/repos/acme/widgets/contents/docs/new.md",
               json={"message": "add", "content": "aGk=", "branch": "SYM-7"})
    assert put.status_code == 201, put.text
    upd = _api(gh, token, "PUT", "/repos/acme/widgets/contents/docs/new.md",
               json={"message": "update", "content": "aG8=", "branch": "SYM-7"})
    assert upd.status_code == 422  # sha required to update
    deleted = _api(gh, token, "DELETE", "/repos/acme/widgets/contents/docs/new.md",
                   json={"message": "rm", "sha": put.json()["content"]["sha"], "branch": "SYM-7"})
    assert deleted.status_code == 200, deleted.text
    ref = _api(gh, token, "POST", "/repos/acme/widgets/git/refs", json={"ref": "refs/heads/tmp-7", "sha": head})
    assert ref.status_code == 201
    assert _api(gh, token, "PATCH", "/repos/acme/widgets/git/refs/heads/tmp-7", json={"sha": head}).status_code == 200
    assert _api(gh, token, "DELETE", "/repos/acme/widgets/git/refs/heads/tmp-7").status_code == 204
    closed = _api(gh, token, "PATCH", f"/repos/acme/widgets/pulls/{n}", json={"state": "closed"})
    assert closed.json()["state"] == "closed"
    raw = httpx.get(f"{gh.url}/repos/acme/widgets/contents/README.md",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"})
    assert raw.content == b"# widgets\n"


# -- issues as a tracker -------------------------------------------------------------------------------
def test_issues_rest_sub_issues_and_dependencies(gh, app):
    token = _cred(app, ["acme/widgets", "acme/gadgets"]).token()
    base = "/repos/acme/widgets/issues"
    opened = _api(gh, token, "POST", base, json={"title": "Parent", "body": "p", "labels": ["symphony"]})
    assert opened.status_code == 201, opened.text
    parent = opened.json()
    assert parent["labels"][0]["name"] == "symphony" and parent["sub_issues_summary"]["total"] == 0
    kids = [_api(gh, token, "POST", base, json={"title": f"Child {i}"}).json() for i in range(3)]
    other = _api(gh, token, "POST", "/repos/acme/gadgets/issues", json={"title": "Elsewhere"}).json()
    n = parent["number"]
    listed = _api(gh, token, "GET", base, params={"labels": "symphony", "state": "open"}).json()
    assert [i["number"] for i in listed] == [n]
    assert _api(gh, token, "GET", f"{base}/{n}").json()["title"] == "Parent"
    edited = _api(gh, token, "PATCH", f"{base}/{n}", json={"title": "Parent (edited)", "labels": ["symphony", "bug"]})
    assert edited.status_code == 200 and {x["name"] for x in edited.json()["labels"]} == {"symphony", "bug"}
    assert _api(gh, token, "PUT", f"{base}/{n}/labels", json={"labels": ["symphony"]}).status_code == 200
    assert _api(gh, token, "POST", f"{base}/{n}/labels", json={"labels": ["investigation"]}).status_code == 200
    assert _api(gh, token, "DELETE", f"{base}/{n}/labels/investigation").status_code == 200
    comment = _api(gh, token, "POST", f"{base}/{n}/comments", json={"body": "## Workpad\n- [ ] one"}).json()
    assert _api(gh, token, "PATCH", f"/repos/acme/widgets/issues/comments/{comment['id']}",
                json={"body": "## Workpad\n- [x] one"}).status_code == 200
    assert _api(gh, token, "GET", f"{base}/{n}/comments").json()[-1]["body"].endswith("[x] one")
    # sub-issues: by REST integer id, ordered, one parent each, same owner
    for kid in kids:
        added = _api(gh, token, "POST", f"{base}/{n}/sub_issues", json={"sub_issue_id": kid["id"]})
        assert added.status_code == 201, added.text
    assert added.json()["sub_issues_summary"]["total"] == 3
    again = _api(gh, token, "POST", f"/repos/acme/widgets/issues/{kids[0]['number']}/sub_issues",
                 json={"sub_issue_id": kids[1]["id"]})
    assert again.status_code == 422  # already has a parent
    loop = _api(gh, token, "POST", f"/repos/acme/widgets/issues/{kids[0]['number']}/sub_issues",
                json={"sub_issue_id": parent["id"]})
    assert loop.status_code == 422
    assert _api(gh, token, "POST", f"{base}/{n}/sub_issues", json={"sub_issue_id": "x"}).status_code == 422
    moved = _api(gh, token, "PATCH", f"{base}/{n}/sub_issues/priority",
                 json={"sub_issue_id": kids[2]["id"], "before_id": kids[0]["id"]})
    assert moved.status_code == 200, moved.text
    order = [i["id"] for i in _api(gh, token, "GET", f"{base}/{n}/sub_issues").json()]
    assert order == [kids[2]["id"], kids[0]["id"], kids[1]["id"]]
    child = _api(gh, token, "GET", f"{base}/{kids[0]['number']}").json()
    assert child["parent_issue_url"].endswith(f"/repos/acme/widgets/issues/{n}")
    removed = _api(gh, token, "DELETE", f"{base}/{n}/sub_issue", json={"sub_issue_id": kids[1]["id"]})
    assert removed.status_code == 200 and removed.json()["sub_issues_summary"]["total"] == 2
    # dependencies: kids[0] is blocked by kids[1] and by an issue in another repository
    k0 = kids[0]["number"]
    for blocker in (kids[1], other):
        dep = _api(gh, token, "POST", f"{base}/{k0}/dependencies/blocked_by", json={"issue_id": blocker["id"]})
        assert dep.status_code == 201, dep.text
    assert _api(gh, token, "POST", f"{base}/{k0}/dependencies/blocked_by",
                json={"issue_id": other["id"]}).status_code == 422  # duplicate
    blockers = _api(gh, token, "GET", f"{base}/{k0}/dependencies/blocked_by").json()
    assert [b["id"] for b in blockers] == [kids[1]["id"], other["id"]]
    blocking = _api(gh, token, "GET", f"/repos/acme/gadgets/issues/{other['number']}/dependencies/blocking").json()
    assert [b["number"] for b in blocking] == [k0]
    summary = _api(gh, token, "GET", f"{base}/{k0}").json()["issue_dependencies_summary"]
    assert summary["blocked_by"] == 2 and summary["total_blocked_by"] == 2
    assert _api(gh, token, "DELETE", f"{base}/{k0}/dependencies/blocked_by/{other['id']}").status_code == 200
    assert _api(gh, token, "DELETE", f"{base}/{k0}/dependencies/blocked_by/{other['id']}").status_code == 404
    closed = _api(gh, token, "PATCH", f"{base}/{kids[1]['number']}", json={"state": "closed", "state_reason": "completed"})
    assert closed.json()["state"] == "closed" and closed.json()["closed_by"]["login"] == "symphony[bot]"
    assert _api(gh, token, "GET", f"{base}/{k0}").json()["issue_dependencies_summary"]["blocked_by"] == 0
    reopened = _api(gh, token, "PATCH", f"{base}/{kids[1]['number']}", json={"state": "open"})
    assert reopened.json()["state"] == "open" and reopened.json()["state_reason"] == "reopened"
    with_parent = _api(gh, token, "POST", base, json={"title": "Grandchild", "parent_issue_id": kids[0]["id"]})
    assert with_parent.status_code == 201
    assert _api(gh, token, "GET", f"{base}/{k0}/sub_issues").json()[0]["title"] == "Grandchild"


def test_issue_permissions_are_enforced(gh, app):
    issue = gh.mock.create_issue(gh.mock.find_repo("acme", "widgets"), "reporter", "Perms")
    n = issue["number"]
    pr_only = _cred(app, ["acme/widgets"], {"pull_requests": "write", "contents": "read"}).token()
    for method, path, body in (("POST", "/repos/acme/widgets/issues", {"title": "x"}),
                               ("GET", f"/repos/acme/widgets/issues/{n}", None),
                               ("PATCH", f"/repos/acme/widgets/issues/{n}", {"title": "y"}),
                               ("POST", f"/repos/acme/widgets/issues/{n}/comments", {"body": "z"}),
                               ("GET", f"/repos/acme/widgets/issues/{n}/sub_issues", None)):
        resp = _api(gh, pr_only, method, path, json=body)
        assert resp.status_code == 403 and resp.json()["message"] == rest.FORBIDDEN, (method, path)
    assert all(i.get("pull_request") for i in _api(gh, pr_only, "GET", "/repos/acme/widgets/issues").json())
    read_only = _cred(app, ["acme/widgets"], {"issues": "read"}).token()
    assert _api(gh, read_only, "GET", f"/repos/acme/widgets/issues/{n}").status_code == 200
    assert _api(gh, read_only, "PATCH", f"/repos/acme/widgets/issues/{n}", json={"title": "y"}).status_code == 403
    gh.mock.create_project(OWNER, 3)
    query = "query { organization(login: \"acme\") { projectV2(number: 3) { id } } }"
    out = _api(gh, read_only, "POST", "/graphql", json={"query": query}).json()
    assert out["errors"][0]["type"] == "FORBIDDEN"
    proj_ro = _cred(app, ["acme/widgets"], {"issues": "read", "organization_projects": "read"}).token()
    pid = _api(gh, proj_ro, "POST", "/graphql", json={"query": query}).json()["data"]["organization"]["projectV2"]["id"]
    issue_id = _api(gh, proj_ro, "POST", "/graphql", json={
        "query": f"{{ repository(owner: \"acme\", name: \"widgets\") {{ issue(number: {n}) {{ id }} }} }}"}).json()
    add = _api(gh, proj_ro, "POST", "/graphql", json={
        "query": "mutation($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c}) "
                 "{ item { id } } }", "variables": {"p": pid, "c": issue_id["data"]["repository"]["issue"]["id"]}})
    assert add.json()["errors"][0]["type"] == "FORBIDDEN"
    other = _api(gh, proj_ro, "POST", "/graphql", json={"query": "{ user(login: \"nobody-here\") { login } }"}).json()
    assert other["errors"][0]["type"] == "NOT_FOUND"


PROJECT_QUERY = """
query($o: String!, $n: Int!) { repositoryOwner(login: $o) { ... on ProjectV2Owner { projectV2(number: $n) {
  id title
  fields(first: 50) { nodes {
    ... on ProjectV2SingleSelectField { id name options { id name } }
    ... on ProjectV2Field { id name dataType } } } } } } }
"""
ITEMS_QUERY = """
query($p: ID!, $a: String) { node(id: $p) { ... on ProjectV2 { items(first: 2, after: $a) {
  pageInfo { hasNextPage endCursor }
  nodes { id type
    status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name optionId } }
    priority: fieldValueByName(name: "Priority") { ... on ProjectV2ItemFieldNumberValue { number } }
    fieldValues(first: 20) { nodes { __typename } }
    content { ... on Issue { id number title body url createdAt updatedAt state
      repository { name nameWithOwner } labels(first: 50) { nodes { name } } assignees(first: 10) { nodes { login id } }
      blockedBy(first: 20) { nodes { id number state repository { name nameWithOwner } } }
      parent { id } subIssues(first: 50) { nodes { id } }
      projectItems(first: 20) { nodes { id project { id } fieldValueByName(name: "Status") {
        ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } } } } } }
"""


def test_projects_v2_graphql_and_control(gh, app, tmp_path):
    m = gh.mock
    token = _cred(app, ["acme/widgets"]).token()
    created = httpx.post(f"{gh.url}/_mock/projects", json={"owner": OWNER, "number": 7, "priority": True}).json()
    assert [f["name"] for f in created["fields"]] == ["Title", "Status", "Priority"]
    assert [o["name"] for o in created["fields"][1]["options"]] == [
        "Todo", "In Progress", "In Review", "Rework", "Merging", "Done", "Canceled"]
    first = httpx.post(f"{gh.url}/_mock/issues", json={"repo": "acme/widgets", "user": "reporter", "title": "Ticket A",
                                                        "labels": ["symphony"], "project": 7, "status": "Todo"}).json()
    second = httpx.post(f"{gh.url}/_mock/issues", json={"repo": "acme/widgets", "user": "reporter",
                                                         "title": "Ticket B", "project": 7}).json()
    m.set_item_field(OWNER, 7, second["item_id"], "Priority", 2)
    httpx.post(f"{gh.url}/_mock/project-items", json={"owner": OWNER, "number": 7, "draft_title": "Idea"})
    _push_branch(gh, tmp_path, token, "SYM-P", {"p.txt": "p\n"})
    pr = _api(gh, token, "POST", "/repos/acme/widgets/pulls", json={"title": "P", "head": "SYM-P", "base": "main"}).json()
    m.add_project_item(OWNER, 7, repo=m.find_repo(OWNER, "widgets"), issue_number=pr["number"])
    gql = RestGitHub("acme/widgets", token=lambda: token)._graphql
    project = gql(PROJECT_QUERY, {"o": OWNER, "n": 7})["repositoryOwner"]["projectV2"]
    status = next(f for f in project["fields"]["nodes"] if f["name"] == "Status")
    nodes, after = [], None
    while True:
        page = gql(ITEMS_QUERY, {"p": project["id"], "a": after})["node"]["items"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    assert [n["type"] for n in nodes] == ["ISSUE", "ISSUE", "DRAFT_ISSUE", "PULL_REQUEST"]
    a, b = nodes[0], nodes[1]
    assert a["status"]["name"] == "Todo" and a["content"]["labels"]["nodes"] == [{"name": "symphony"}]
    assert b["status"] is None and b["priority"] == {"number": 2.0}
    assert a["content"]["projectItems"]["nodes"][0]["project"]["id"] == project["id"]
    assert "ProjectV2ItemFieldTextValue" in {v["__typename"] for v in a["fieldValues"]["nodes"]}
    in_progress = next(o["id"] for o in status["options"] if o["name"] == "In Progress")
    mutation = ("mutation($p: ID!, $i: ID!, $f: ID!, $v: String!) { updateProjectV2ItemFieldValue(input: "
                "{projectId: $p, itemId: $i, fieldId: $f, value: {singleSelectOptionId: $v}}) "
                "{ projectV2Item { id fieldValueByName(name: \"Status\") { "
                "... on ProjectV2ItemFieldSingleSelectValue { name } } } } }")
    moved = gql(mutation, {"p": project["id"], "i": a["id"], "f": status["id"], "v": in_progress})
    assert moved["updateProjectV2ItemFieldValue"]["projectV2Item"]["fieldValueByName"]["name"] == "In Progress"
    bad = _api(gh, token, "POST", "/graphql", json={"query": mutation, "variables": {
        "p": project["id"], "i": a["id"], "f": status["id"], "v": "nope"}}).json()
    assert bad["errors"][0]["type"] == "UNPROCESSABLE"
    # a person moves it on the board
    httpx.post(f"{gh.url}/_mock/project-items/move", json={"owner": OWNER, "number": 7, "repo": "acme/widgets",
                                                           "issue_number": first["issue"]["number"],
                                                           "status": "Rework", "user": "maintainer"})
    by_id = gql("query($ids: [ID!]!) { nodes(ids: $ids) { ... on Issue { number projectItems(first: 20) { nodes { "
                "fieldValueByName(name: \"Status\") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }",
                {"ids": [a["content"]["id"]]})
    assert by_id["nodes"][0]["projectItems"]["nodes"][0]["fieldValueByName"]["name"] == "Rework"
    # add (idempotent) and delete through GraphQL
    third = m.create_issue(m.find_repo(OWNER, "widgets"), "reporter", "Ticket C")
    content = gql(f"{{ repository(owner: \"acme\", name: \"widgets\") {{ id issue(number: {third['number']}) "
                  f"{{ id }} }} }}", {})["repository"]["issue"]["id"]
    add = "mutation($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } } }"
    item = gql(add, {"p": project["id"], "c": content})["addProjectV2ItemById"]["item"]["id"]
    assert gql(add, {"p": project["id"], "c": content})["addProjectV2ItemById"]["item"]["id"] == item
    deleted = gql("mutation($p: ID!, $i: ID!) { deleteProjectV2Item(input: {projectId: $p, itemId: $i}) "
                  "{ deletedItemId } }", {"p": project["id"], "i": item})
    assert deleted["deleteProjectV2Item"]["deletedItemId"] == item
    org = gql("{ organization(login: \"acme\") { projectV2(number: 7) { title items(first: 10) { totalCount } } } }", {})
    assert org["organization"]["projectV2"]["items"]["totalCount"] == 4
    assert gql("query($i: ID!) { node(id: $i) { ... on ProjectV2Item { type } } }", {"i": a["id"]})["node"]["type"] == "ISSUE"
    unknown = _api(gh, token, "POST", "/graphql", json={"query": "query($ids: [ID!]!) { nodes(ids: $ids) { id } }",
                                                        "variables": {"ids": ["I_bm9wZTo5OTk5", a["id"]]}}).json()
    assert unknown["errors"][0]["type"] == "NOT_FOUND" and unknown["errors"][0]["path"] == ["nodes", 0]
    assert unknown["data"]["nodes"] == [None, {"id": a["id"]}]
    # a token scoped to another repository sees the issue items redacted
    narrow = _cred(app, ["acme/gadgets"]).token()
    redacted = RestGitHub("acme/gadgets", token=lambda: narrow)._graphql(ITEMS_QUERY, {"p": project["id"], "a": None})
    assert redacted["node"]["items"]["nodes"][0] == {**redacted["node"]["items"]["nodes"][0], "type": "REDACTED",
                                                     "content": None}
    m.hooks.flush(30)
    edits = [d for d in m.hooks.deliveries if d["event"] == "projects_v2_item" and d["action"] == "edited"]
    change = edits[-1]["payload"]["changes"]["field_value"]
    assert change["field_name"] == "Status" and change["to"]["name"] == "Rework" and change["from"]["name"] == "In Progress"


def test_webhooks_are_signed_and_route_like_github(gh, receiver):
    assert gh.mock.hooks.flush(30)
    got = receiver.RequestHandlerClass.got
    events = {h["x-github-event"] for h, _ in got}
    assert {"push", "pull_request", "issue_comment", "pull_request_review", "pull_request_review_comment",
            "check_run", "check_suite", "workflow_run", "issues", "sub_issues", "issue_dependencies",
            "projects_v2_item"} <= events
    for headers, body in got:
        webhooks._verify(SECRET, body, headers["x-hub-signature-256"], prefix="sha256=")  # raises on mismatch
        assert len(headers["x-github-delivery"]) == 36
    spec = gh.mock.spec
    for delivery in gh.mock.hooks.deliveries:
        ptr = spec.webhook_pointer(delivery["schema"])
        assert ptr is not None, delivery["schema"]
        assert spec.errors(ptr, delivery["payload"]) == [], delivery["schema"]
    wakes = set()
    for headers, body in got:
        for target, reason in webhooks.github_wakes(headers["x-github-event"], json.loads(body)):
            wakes.add((target, reason.split(":")[1]))
    # Merger wakes are per repository (`merger:<owner/name>`, internals §5).
    merger = {reason for target, reason in wakes if target.startswith("merger:")}
    assert ("daemon", "comment") in wakes and {"pull_request", "push"} <= merger
    assert not any(target == "merger" for target, _ in wakes)
    tampered = got[0][1] + b" "
    with pytest.raises(Exception, match="signature"):
        webhooks._verify(SECRET, tampered, got[0][0]["x-hub-signature-256"], prefix="sha256=")


def test_every_route_served_and_every_response_on_spec(gh):
    """Runs last: no response the suite saw broke its documented schema, and every route served."""
    assert gh.mock.violations == []
    served = {(m, t) for m, t, _ in gh.mock.served}
    missing = sorted(set(rest.ROUTES) - served)
    assert missing == []
