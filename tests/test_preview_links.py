"""Preview links (`preview_links`, `api/previews.py`, `PreviewManager.open_link`): a signed link
on the PR opens that build in Seiji for a mapped person, behind Cloudflare Access.

Against the Seiji mock, a fake GitHub (an httpx transport) and a fake Access JWKS endpoint
with a locally generated RSA key.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.mocks.seiji import server as mock_seiji
from tests.api_support import client_for, make_services

from symphony import mcp_servers, preview_links
from symphony.access import AccessVerifier
from symphony.api.control import Control
from symphony.api.previews import build_router, person_for_email
from symphony.config import parse_workflow
from symphony.previews import LEASE_MAX_AGE_S, PreviewManager

NS = "test"  # tests.api_support.NS
SECRET = "test-preview-link-secret-0123456789"
TEAM = "acme.cloudflareaccess.com"
AUD = "aud-0123456789abcdef"
REPO = "acme/app"
SHA_NEW = "abcdef012345" + "1" * 28
SHA_OLD = "123456abcdef" + "2" * 28
SHA_THIRD = "fedcba987654" + "3" * 28
SHA_EXPIRED = "0badc0ffee00" + "4" * 28

USERS = """\
notify:
  users:
    slack:
      alice: U_ALICE
      alice@example.com: U_ALICE
      carol@example.com: U_CAROL
"""
ACCESS = f"api:\n  access: {{team_domain: {TEAM}, aud: {AUD}}}\n"


def workflow(extra: str) -> object:
    return parse_workflow(f"---\ntracker: {{kind: memory}}\n{USERS}{extra}---\nx\n")


# -- fakes --------------------------------------------------------------------------------
def artifact_zip(head_sha: str) -> bytes:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", "<plist/>")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("app.zip", inner.getvalue())
        zf.writestr("preview.json", json.dumps({"platform": "ios", "bundle_id": "com.example.app",
                                                "head_sha": head_sha}))
    return outer.getvalue()


class FakeGitHub:
    """The REST calls of a preview: the commit, its runs, their artifacts, the download, and
    the PR (state, head, reviews) for the sweep."""

    def __init__(self) -> None:
        self.head = SHA_NEW
        self.state = "open"
        self.builds = {SHA_NEW: False, SHA_OLD: False, SHA_THIRD: False, SHA_EXPIRED: True}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = f"/repos/{REPO}"
        if path.startswith(f"{prefix}/commits/"):
            ref = path.rsplit("/", 1)[-1]
            for sha in self.builds:
                if sha.startswith(ref):
                    return httpx.Response(200, json={"sha": sha})
            return httpx.Response(404, json={"message": "No commit found for SHA"})
        if path == f"{prefix}/actions/runs":
            sha = request.url.params["head_sha"]
            runs = [{"id": i, "head_sha": sha} for i, s in enumerate(self.builds) if s == sha]
            return httpx.Response(200, json={"total_count": len(runs), "workflow_runs": runs})
        if path.startswith(f"{prefix}/actions/runs/") and path.endswith("/artifacts"):
            sha = list(self.builds)[int(path.split("/")[-2])]
            return httpx.Response(200, json={"total_count": 1, "artifacts": [{
                "name": "ios-simulator-preview", "expired": self.builds[sha],
                "archive_download_url": f"http://github.test/_download/{sha}.zip"}]})
        if path.startswith("/_download/"):
            return httpx.Response(200, content=artifact_zip(path.rsplit("/", 1)[-1][:-4]))
        if path == f"{prefix}/pulls/7":
            return httpx.Response(200, json={"number": 7, "state": self.state,
                                             "merged_at": None, "head": {"sha": self.head}})
        if path == f"{prefix}/pulls/7/reviews":
            return httpx.Response(200, json=[{"state": "APPROVED", "user": {"login": "bob"},
                                              "submitted_at": "2099-01-01T00:00:00Z"}])
        return httpx.Response(404, json={"message": "Not Found"})


class Route(httpx.BaseTransport):
    """GitHub to the fake, everything else (Seiji) to the network."""

    def __init__(self, github: FakeGitHub) -> None:
        self.github = github
        self.real = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.test":
            return self.github(request)
        return self.real.handle_request(request)


class Access:
    """A Cloudflare Access team: an RSA key, its JWKS endpoint, and tokens it signs."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}
        self.fetches = 0

    def http(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == f"https://{TEAM}/cdn-cgi/access/certs"
            self.fetches += 1
            return httpx.Response(200, json=self.jwks)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def token(self, email="alice@example.com", *, aud=AUD, exp_in=300, key=None, kid="k1",
              iss=f"https://{TEAM}") -> str:
        now = int(time.time())
        return jwt.encode({"aud": [aud], "iss": iss, "email": email, "iat": now,
                           "exp": now + exp_in, "sub": "u1"},
                          key or self.key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def seiji():
    srv = mock_seiji.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


class Deferred:
    """`PreviewManager.spawn` that runs the start when the test says so."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, work) -> None:
        self.pending.append(work)

    def run(self) -> None:
        while self.pending:
            self.pending.pop(0)()


@pytest.fixture
def stack(redis_client, seiji, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "http://github.test")
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1")
    monkeypatch.setenv(preview_links.SECRET_ENV, SECRET)
    repo = mcp_servers.McpRepository(None, NS, local_path=tmp_path / "mcp.json")
    repo.put("seiji", {"preset": "seiji", "url": f"{seiji}/mcp", "roles": ["assistant"]})
    repo.set_headers("seiji", {"Authorization": f"Bearer {mock_seiji.DEFAULT_TOKEN}"})
    github, access, spawn = FakeGitHub(), Access(), Deferred()
    mgr = PreviewManager(client=redis_client, namespace=NS, repository=repo,
                         github_token=lambda r: (lambda: "ghs_fake"),
                         http=lambda: httpx.Client(transport=Route(github), timeout=30),
                         spawn=spawn)
    services = make_services(redis_client)
    services.previews = mgr
    holder = {"cfg": workflow(ACCESS)}
    app = FastAPI()
    app.include_router(build_router(Control(services), config=lambda: holder["cfg"],
                                    jwks_http=access.http))
    client = TestClient(app, follow_redirects=False)

    class Stack:
        pass

    s = Stack()
    s.client, s.mgr, s.services, s.github, s.access, s.spawn, s.holder, s.seiji = (
        client, mgr, services, github, access, spawn, holder, seiji)
    return s


def link(sha: str, pr: int = 7, key: str | None = SECRET) -> str:
    url = preview_links.url("https://symphony.example.com", key, REPO, pr, sha[:12])
    return url.removeprefix("https://symphony.example.com")


def status_of(path: str) -> str:
    return path.replace("/open?", "/status?", 1)


def seiji_state(stack) -> dict:
    return httpx.get(f"{stack.seiji}/_control/state").json()


def audits(stack) -> list[dict]:
    return [e for e in stack.services.store.audit(limit=200)["items"]
            if e["action"] == "preview.opened"]


def signed_in(stack, email="alice@example.com") -> dict:
    return {"Cf-Access-Jwt-Assertion": stack.access.token(email)}


# -- the link -----------------------------------------------------------------------------
def test_a_link_is_signed_over_repo_pr_and_sha():
    sig = preview_links.sign(SECRET, REPO, 7, SHA_NEW[:12])
    assert preview_links.verify(SECRET, "Acme/App", 7, SHA_NEW[:12], sig)
    assert not preview_links.verify(SECRET, REPO, 8, SHA_NEW[:12], sig)
    assert not preview_links.verify(SECRET, REPO, 7, SHA_OLD[:12], sig)
    assert not preview_links.verify("another-secret", REPO, 7, SHA_NEW[:12], sig)
    query = parse_qs(urlsplit(link(SHA_NEW)).query)
    assert query == {"repo": [REPO], "pr": ["7"], "sha": [SHA_NEW[:12]], "sig": [sig]}


def test_an_email_maps_to_the_login_of_the_same_chat_user():
    users = workflow("").notify_users
    assert person_for_email(users, "Alice@Example.com") == "alice"
    assert person_for_email(users, "carol@example.com") == "carol@example.com"
    assert person_for_email(users, "mallory@example.com") is None
    assert person_for_email(users, None) is None


# -- refusals -----------------------------------------------------------------------------
def test_a_bad_signature_is_a_403_page(stack):
    path = link(SHA_NEW).replace("pr=7", "pr=8")
    resp = stack.client.get(path, headers=signed_in(stack))
    assert resp.status_code == 403 and "text/html" in resp.headers["content-type"]
    assert "This link is not valid" in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert stack.client.get(status_of(path)).json()["state"] == "refused"
    assert audits(stack) == [] and stack.access.fetches == 0


def test_a_malformed_link_is_a_400_page(stack):
    assert stack.client.get("/v1/previews/open?repo=acme/app&pr=7").status_code == 400


def test_the_control_api_serves_the_route_as_html_without_a_token(redis_client, tmp_path,
                                                                   monkeypatch):
    from symphony import edge

    monkeypatch.setenv(preview_links.SECRET_ENV, SECRET)
    monkeypatch.setattr(edge, "WORKFLOW_MOUNT", str(tmp_path))  # no WORKFLOW.md there
    resp = client_for(make_services(redis_client)).get(link(SHA_NEW))
    assert resp.status_code == 503 and "text/html" in resp.headers["content-type"]
    assert "www-authenticate" not in resp.headers



def test_without_an_access_token_it_is_a_401_page(stack):
    resp = stack.client.get(link(SHA_NEW))
    assert resp.status_code == 401 and "Sign-in required" in resp.text
    # The e-mail header alone is never trusted.
    resp = stack.client.get(link(SHA_NEW),
                            headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"})
    assert resp.status_code == 401


@pytest.mark.parametrize("token", [
    lambda a: a.token(aud="another-application"),
    lambda a: a.token(exp_in=-120),
    lambda a: a.token(iss="https://other.cloudflareaccess.com"),
    lambda a: a.token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048)),
    lambda a: a.token(kid="unknown"),
    lambda a: a.token()[:-6] + "AAAAAA",
    lambda a: "not-a-jwt",
], ids=["wrong-aud", "expired", "wrong-iss", "bad-signature", "unknown-kid", "tampered",
        "garbage"])
def test_a_refused_access_token_is_a_401_page(stack, token):
    resp = stack.client.get(link(SHA_NEW),
                            headers={"Cf-Access-Jwt-Assertion": token(stack.access)})
    assert resp.status_code == 401 and "could not be verified" in resp.text
    assert audits(stack) == [] and seiji_state(stack)["leases"] == []


def test_a_verified_email_that_is_not_mapped_is_a_403_page(stack):
    resp = stack.client.get(link(SHA_NEW), headers=signed_in(stack, "mallory@example.com"))
    assert resp.status_code == 403 and "not mapped" in resp.text


def test_without_access_and_without_public_links_the_route_refuses(stack):
    stack.holder["cfg"] = workflow("")
    resp = stack.client.get(link(SHA_NEW), headers=signed_in(stack))
    assert resp.status_code == 403 and "Preview links are off" in resp.text


def test_the_jwks_is_cached(stack):
    for _ in range(3):
        stack.client.get(status_of(link(SHA_NEW)), headers=signed_in(stack))
    assert stack.access.fetches == 1
    verifier = AccessVerifier(TEAM, AUD, http=stack.access.http)
    assert verifier.verify(stack.access.token())["email"] == "alice@example.com"


# -- opening ------------------------------------------------------------------------------
def test_a_mapped_person_opens_the_build_and_reloads_for_a_new_viewer(stack, monkeypatch):
    steps: list[str] = []
    real_step = stack.mgr._step
    monkeypatch.setattr(stack.mgr, "_step", lambda key, step: (steps.append(step),
                                                                 real_step(key, step)))
    path, headers = link(SHA_NEW), signed_in(stack)
    page = stack.client.get(path, headers=headers)
    assert page.status_code == 200 and 'fetch("status" + location.search' in page.text
    assert "setTimeout(poll, 2000)" in page.text
    assert "script-src 'nonce-" in page.headers["content-security-policy"]
    assert stack.client.get(status_of(path), headers=headers).json() == {
        "state": "starting", "step": "queued", "message": "Getting ready\u2026"}
    assert stack.client.get(path, headers=headers).status_code == 200  # no second start
    assert len(stack.spawn.pending) == 1

    stack.spawn.run()
    assert steps == ["downloading", "installing", "launching"]
    assert stack.client.get(status_of(path), headers=headers).json()["state"] == "ready"
    first = stack.client.get(path, headers=headers)
    assert first.status_code == 302 and first.headers["location"].startswith(
        f"{stack.seiji}/view/open/")
    again = stack.client.get(path, headers=headers)
    assert again.status_code == 302 and again.headers["location"] != first.headers["location"]

    state = seiji_state(stack)
    assert len(state["leases"]) == 1 and len(state["viewers"]) == 2
    assert all(v["control"] for v in state["viewers"])
    [live] = stack.mgr.live()
    assert (live.head_sha, live.origin) == (SHA_NEW, "link")
    entries = audits(stack)
    assert {e["actor"] for e in entries} == {"alice"}
    assert {e["target"] for e in entries} == {f"{REPO}#7@{SHA_NEW[:12]}"}
    assert sorted(e["reason"] for e in entries) == ["ready", "ready", "starting", "starting"]


def test_an_older_sha_gets_its_own_lease_and_a_third_releases_the_oldest(stack):
    headers = signed_in(stack)
    for sha in (SHA_NEW, SHA_OLD):
        assert stack.client.get(link(sha), headers=headers).status_code == 200
        stack.spawn.run()
    assert sorted(p.head_sha for p in stack.mgr.live()) == sorted([SHA_NEW, SHA_OLD])
    first = stack.mgr.get(REPO, 7, SHA_NEW[:12])
    assert stack.client.get(link(SHA_OLD), headers=headers).status_code == 302

    assert stack.client.get(link(SHA_THIRD), headers=headers).status_code == 200
    stack.spawn.run()
    assert sorted(p.head_sha for p in stack.mgr.live()) == sorted([SHA_OLD, SHA_THIRD])
    assert not mcp_servers.held_by_live_session(stack.services.client, NS, "seiji",
                                                first.lease_id)
    leases = {lease["lease_id"]: lease for lease in seiji_state(stack)["leases"]}
    assert leases[first.lease_id]["status"] == "released"


def test_an_expired_build_says_so(stack):
    resp = stack.client.get(link(SHA_EXPIRED), headers=signed_in(stack))
    assert resp.status_code == 410 and "This build is gone" in resp.text
    assert "has expired" in resp.text
    assert stack.spawn.pending == [] and seiji_state(stack)["leases"] == []


def test_a_sha_the_repository_does_not_have_is_gone(stack):
    resp = stack.client.get(link("999999999999" + "0" * 28), headers=signed_in(stack))
    assert resp.status_code == 410 and "has no commit" in resp.text


def test_a_failed_start_shows_its_error_and_a_reload_tries_again(stack):
    httpx.post(f"{stack.seiji}/_control/config", json={"acquire_error": "no simulator free"})
    path, headers = link(SHA_NEW), signed_in(stack)
    stack.client.get(path, headers=headers)
    stack.spawn.run()
    body = stack.client.get(status_of(path), headers=headers).json()
    assert body["state"] == "error" and "no simulator free" in body["message"]
    httpx.post(f"{stack.seiji}/_control/config", json={"acquire_error": None})
    stack.client.get(path, headers=headers)
    stack.spawn.run()
    assert stack.client.get(status_of(path), headers=headers).json()["state"] == "ready"


def test_public_links_open_without_access(stack):
    stack.holder["cfg"] = workflow("previews:\n  public_links: true\n")
    path = link(SHA_NEW)
    assert stack.client.get(path).status_code == 200
    stack.spawn.run()
    assert stack.client.get(path).status_code == 302
    assert {e["actor"] for e in audits(stack)} == {"public-link"}
    assert stack.client.get(path.replace("sig=", "sig=x")).status_code == 403


def test_public_links_need_a_signature_and_the_secret(stack, monkeypatch):
    stack.holder["cfg"] = workflow("previews:\n  public_links: true\n")
    resp = stack.client.get(link(SHA_NEW, key=None))
    assert resp.status_code == 403 and "This link is not valid" in resp.text
    monkeypatch.delenv(preview_links.SECRET_ENV)
    assert stack.client.get(link(SHA_NEW)).status_code == 503
    assert stack.spawn.pending == [] and audits(stack) == []


def test_behind_access_an_unsigned_link_opens_with_no_secret_at_all(stack, monkeypatch):
    """With Access the person is the credential: the operator provisions no link secret."""
    monkeypatch.delenv(preview_links.SECRET_ENV)
    path = link(SHA_NEW, key=None)
    assert "sig=" not in path
    assert stack.client.get(path).status_code == 401  # still no anonymous access
    assert stack.client.get(path, headers=signed_in(stack)).status_code == 200
    stack.spawn.run()
    resp = stack.client.get(path, headers=signed_in(stack))
    assert resp.status_code == 302 and resp.headers["location"].startswith(stack.seiji)
    # A signed link of earlier days still opens; with no secret its sig is not checked.
    assert stack.client.get(link(SHA_NEW), headers=signed_in(stack)).status_code == 302


def test_behind_access_a_signed_link_is_accepted_and_a_wrong_sig_refused(stack):
    assert stack.client.get(link(SHA_NEW, key=None), headers=signed_in(stack)).status_code == 200
    assert stack.client.get(link(SHA_NEW), headers=signed_in(stack)).status_code == 200
    forged = link(SHA_NEW, key="another-secret")
    assert stack.client.get(forged, headers=signed_in(stack)).status_code == 403


# -- lifetime -----------------------------------------------------------------------------
def test_a_link_lease_survives_head_moves_and_approvals_not_a_merge_or_the_time_limit(stack):
    stack.client.get(link(SHA_OLD), headers=signed_in(stack))
    stack.spawn.run()
    stack.github.head = SHA_NEW  # the PR moved on, and the fake has an approval since
    assert stack.mgr.sweep() == 0
    stack.github.state = "closed"
    assert stack.mgr.sweep() == 1 and stack.mgr.live() == []

    stack.github.state = "open"
    stack.client.get(link(SHA_OLD), headers=signed_in(stack))
    stack.spawn.run()
    [live] = stack.mgr.live()
    live.started_at = time.time() - LEASE_MAX_AGE_S - 1
    stack.mgr._save(live)
    assert stack.mgr.sweep() == 1


def test_a_button_lease_opened_by_link_is_pinned(stack):
    """The reviewer's button started it for the head; a link for the same build reuses the
    device and keeps it when the head moves."""
    stack.client.get(link(SHA_NEW), headers=signed_in(stack))
    stack.spawn.run()
    live = stack.mgr.get(REPO, 7, SHA_NEW[:12])
    live.origin = "button"
    stack.mgr._save(live)
    stack.github.head = SHA_OLD
    assert stack.client.get(link(SHA_NEW), headers=signed_in(stack)).status_code == 302
    assert stack.mgr.get(REPO, 7, SHA_NEW[:12]).origin == "link"
    assert stack.mgr.sweep() == 0


# -- configuration ------------------------------------------------------------------------
def test_api_access_and_public_links_parse():
    cfg = workflow(ACCESS)
    assert cfg.api_access == {"team_domain": TEAM, "aud": AUD}
    assert cfg.previews_public_links is False
    assert workflow("previews: {public_links: true}\n").previews_public_links is True
    assert workflow("").api_access is None


@pytest.mark.parametrize("extra, error", [
    (f"api: {{access: {{team_domain: 'https://{TEAM}', aud: x}}}}\n", "team_domain"),
    (f"api: {{access: {{team_domain: {TEAM}}}}}\n", "aud"),
    (f"api: {{access: {{team_domain: {TEAM}, aud: x, email: y}}}}\n", "unknown keys"),
    ("previews: {public_links: 'yes'}\n", "public_links"),
])
def test_bad_api_access_or_previews_is_refused(extra, error):
    from symphony.config import ConfigError

    with pytest.raises(ConfigError, match=error):
        workflow(extra)


def test_a_closed_prs_devices_are_released_at_once(stack):
    """The merger closed the PR (its ticket was canceled): every device of it goes back."""
    for sha in (SHA_NEW, SHA_OLD):
        stack.client.get(link(sha), headers=signed_in(stack))
        stack.spawn.run()
    assert len(stack.mgr.live()) == 2
    assert stack.mgr.release_pr("Acme/App", 8) == 0
    assert stack.mgr.release_pr("Acme/App", 7) == 2
    assert stack.mgr.live() == []
    assert {lease["status"] for lease in seiji_state(stack)["leases"]} == {"released"}
