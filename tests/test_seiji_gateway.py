"""Symphony's gateway with Seiji (internals §18), against the Seiji mock: the agent's bearer
never reaches Seiji and the stored one does, links point at the gateway, binary bodies and
uploads stream through untouched, held leases are released at session end and by the
sweep, the viewer only opens leases that a live session holds, and the admin test reads
`/v1/me`."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import httpx
import pytest
from scripts.mocks.seiji import server as mock_seiji
from tests.api_support import Server, bearer, client_for, make_services, tokens_for

from symphony import mcp_servers
from symphony.gateway_api import build_app
from symphony.kv import RedisKV
from symphony.mcp_servers import McpLaunch, McpRepository
from symphony.sessions import SessionStore

NS = "test"
ACCEPT = "application/json, text/event-stream"
STORED = f"Bearer {mock_seiji.DEFAULT_TOKEN}"
PUBLIC = "https://seiji.example.test"
SID = "issue-1:1"


@pytest.fixture
def seiji():
    srv = mock_seiji.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield base
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def repo(tmp_path, seiji, monkeypatch):
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1")
    repository = McpRepository(None, NS, local_path=tmp_path / "mcp.json")
    repository.put("seiji", {"preset": "seiji", "url": f"{seiji}/mcp", "roles": ["implementer"],
                             "rewrite_origins": [PUBLIC]})
    repository.set_headers("seiji", {"Authorization": STORED})
    return repository


@pytest.fixture
def gateway(redis_client, repo):
    with Server(build_app(lambda: RedisKV(redis_client), namespace=NS, mcp=repo,
                          sweep_interval_s=None)) as server:
        yield server.url


@pytest.fixture
def token(redis_client):
    SessionStore(redis_client, NS).open(SID, identifier="ENG-1", title="t", kind="implement")
    return mcp_servers.mint_token(redis_client, NS, "implementer", SID)


def state(seiji) -> dict:
    return httpx.get(f"{seiji}/_control/state").json()


def rpc(gateway, token, method, params=None, rpc_id=1):
    message = {"jsonrpc": "2.0", "id": rpc_id, "method": method, **({"params": params} if params else {})}
    return httpx.post(f"{gateway}/v1/mcp/seiji", json=message, timeout=10,
                      headers={"Accept": ACCEPT, "MCP-Protocol-Version": "2025-11-25", **bearer(token)})


def call(gateway, token, tool, arguments=None, rpc_id=2):
    response = rpc(gateway, token, "tools/call", {"name": tool, "arguments": arguments or {}}, rpc_id)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def text_of(result) -> str:
    return result["content"][0]["text"]


def acquire(gateway, token) -> str:
    return json.loads(text_of(call(gateway, token, "acquire_device", {"platform": "ios"})))["lease_id"]


# -- credentials -------------------------------------------------------------------------
def test_the_stored_header_reaches_seiji_and_the_agents_bearer_never_does(gateway, seiji, token):
    initialized = rpc(gateway, token, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "omp", "version": "18.20.2"}})
    assert initialized.json()["result"]["serverInfo"]["name"] == "seiji"
    assert "mcp-session-id" not in initialized.headers
    budget = json.loads(text_of(call(gateway, token, "budget_status")))
    assert budget["user"] == mock_seiji.USER
    me = httpx.get(f"{gateway}/v1/mcp/seiji/http/v1/me", headers=bearer(token), timeout=10)
    assert me.status_code == 200 and me.json()["user"] == mock_seiji.USER
    seen = state(seiji)
    assert seen["requests"] and all(r["auth"] == "valid" for r in seen["requests"])
    assert seen["other_bearers"] == []  # the session token never reached Seiji
    # Seiji's documented headers pass on the MCP route
    first = seen["requests"][0]
    assert {"accept", "content-type", "mcp-protocol-version", "authorization"} <= set(first["headers"])


def test_without_a_live_token_nothing_reaches_seiji(gateway, seiji, token, redis_client):
    mcp_servers.revoke_token(redis_client, NS, token)
    assert rpc(gateway, token, "tools/list").status_code == 403
    assert httpx.get(f"{gateway}/v1/mcp/seiji/http/v1/me", headers=bearer(token)).status_code == 403
    assert httpx.get(f"{gateway}/v1/mcp/seiji/http/v1/me").status_code == 403
    assert state(seiji)["requests"] == []


def test_viewer_routes_are_never_proxied(gateway, seiji, token):
    for path in ("view/open/abc", "View/s/x/stream", "v1/../view/open/x", "v1//me"):
        response = httpx.get(f"{gateway}/v1/mcp/seiji/http/{path}", headers=bearer(token))
        assert response.status_code == 404, path
    assert state(seiji)["requests"] == []


# -- links -------------------------------------------------------------------------------
def test_links_are_rewritten_in_json_and_markdown(gateway, seiji, token):
    http_url = f"{gateway}/v1/mcp/seiji/http"
    acquire(gateway, token)
    summary = json.loads(text_of(call(gateway, token, "run_actions", {
        "name": "Sign in", "actions": [{"launch_app": {}}, {"tap": {"label": "Sign In"}}]})))
    run_id = summary["run_id"]
    assert summary["evidence_zip"] == f"{http_url}/v1/runs/{run_id}/evidence.zip"  # http_base
    httpx.post(f"{seiji}/_control/config", json={"public_url": PUBLIC})  # Seiji's publicUrl
    markdown = text_of(call(gateway, token, "export_evidence", {"run_id": run_id}))
    assert f"]({http_url}/v1/runs/{run_id}/evidence.zip)" in markdown
    assert PUBLIC not in markdown and seiji not in markdown
    report = httpx.get(f"{http_url}/v1/runs/{run_id}/report.md", headers=bearer(token), timeout=10)
    assert report.headers["content-type"].startswith("text/markdown")
    assert report.text == markdown
    log = httpx.get(f"{http_url}/v1/runs/{run_id}/files/app.log", headers=bearer(token), timeout=10)
    assert f"{http_url}/v1/runs/{run_id}/files/video.mp4" in log.text
    # the link works for the agent: the gateway serves it with the stored header
    assert httpx.get(f"{http_url}/v1/runs/{run_id}/evidence.zip", headers=bearer(token)).status_code == 200


def test_a_similar_origin_is_not_rewritten():
    rewrite = mcp_servers.link_rewriter(
        {"http_base": "https://seiji.example.com", "rewrite_origins": ["https://pub.example.com"]},
        "http://gw/v1/mcp/seiji/http")
    text = ("https://seiji.example.com/v1/me https://seiji.example.com.evil.net/x "
            "https://seiji.example.com:8443/x https://pub.example.com. https://pub.example.com/a")
    assert rewrite(text) == ("http://gw/v1/mcp/seiji/http/v1/me https://seiji.example.com.evil.net/x "
                             "https://seiji.example.com:8443/x http://gw/v1/mcp/seiji/http. "
                             "http://gw/v1/mcp/seiji/http/a")


def test_binary_bodies_pass_through_untouched(gateway, seiji, token):
    acquire(gateway, token)
    run_id = json.loads(text_of(call(gateway, token, "run_actions", {"actions": [{"launch_app": {}}]})))["run_id"]
    direct = httpx.get(f"{seiji}/v1/runs/{run_id}/evidence.zip", headers={"Authorization": STORED}).content
    assert seiji.encode() in direct  # the stored zip holds Seiji links in app.log
    proxied = httpx.get(f"{gateway}/v1/mcp/seiji/http/v1/runs/{run_id}/evidence.zip", headers=bearer(token))
    assert proxied.headers["content-type"] == "application/zip"
    assert proxied.headers["content-length"] == str(len(direct))
    assert proxied.content == direct
    video = httpx.get(f"{gateway}/v1/mcp/seiji/http/v1/runs/{run_id}/files/video.mp4", headers=bearer(token))
    assert video.content == mock_seiji.VIDEO


def test_large_text_bodies_pass_unchanged(monkeypatch):
    import asyncio

    class Upstream:
        headers = {"content-length": str(mcp_servers.REWRITE_LIMIT + 1)}

        async def aiter_raw(self):
            yield b"x"

    head, rest = asyncio.run(mcp_servers.read_limited(Upstream()))
    assert head == b"" and rest is not None


# -- uploads -----------------------------------------------------------------------------
def test_uploads_stream_through(gateway, seiji, token):
    first, second = b"a" * (1 << 20), b"b" * (3 << 20)
    progressed = threading.Event()

    def body():
        yield first
        deadline = time.monotonic() + 10
        while state(seiji)["receiving"] < len(first) and time.monotonic() < deadline:
            time.sleep(0.05)
        progressed.set()  # Seiji holds the first part while the rest is still unsent
        yield second

    response = httpx.post(f"{gateway}/v1/mcp/seiji/http/v1/apps", content=body(), timeout=30,
                          headers={**bearer(token), "X-Filename": "My+App.zip",
                                   "Content-Length": str(len(first) + len(second))})
    assert response.status_code == 200, response.text
    assert progressed.is_set() and state(seiji)["receiving"] == 0
    assert response.json()["filename"] == "My_App.zip" and response.json()["size"] == len(first) + len(second)
    [app] = state(seiji)["apps"]
    assert app["sha256"] == hashlib.sha256(first + second).hexdigest() and app["chunked"] is False
    missing = httpx.post(f"{gateway}/v1/mcp/seiji/http/v1/apps", content=b"x", headers=bearer(token))
    assert missing.status_code == 400 and "X-Filename" in missing.json()["error"]


# -- held leases -------------------------------------------------------------------------
def test_a_held_lease_is_recorded_shown_and_released_at_session_end(gateway, seiji, token, redis_client, repo):
    lease = acquire(gateway, token)
    leases = mcp_servers.session_leases(redis_client, NS, [SID])
    assert leases == {SID: [{"server": "seiji", "id": lease, "model": "iphone-16", "platform": "ios"}]}
    events = [(kind, data) for _, kind, data in SessionStore(redis_client, NS).read_events(SID)]
    assert (mcp_servers.EV_MCP_LEASE, {"server": "seiji", "id": lease, "model": "iphone-16",
                                       "platform": "ios", "held": True}) in events
    McpLaunch(redis_client, NS, repo, gateway).end(token)
    assert [x["status"] for x in state(seiji)["leases"]] == ["released"]
    assert mcp_servers.session_leases(redis_client, NS, [SID]) == {SID: []}
    assert redis_client.smembers(f"sym:{NS}:mcp:held") == set()
    released = [d for _, k, d in SessionStore(redis_client, NS).read_events(SID) if k == mcp_servers.EV_MCP_LEASE]
    assert released[-1]["held"] is False
    # the release went to Seiji with the stored header only
    assert state(seiji)["other_bearers"] == []


def test_release_device_by_the_agent_forgets_the_lease(gateway, seiji, token, redis_client):
    first, second = acquire(gateway, token), acquire(gateway, token)
    assert text_of(call(gateway, token, "release_device", {"lease_id": first})) == f"released {first}"
    assert [x["id"] for x in mcp_servers.session_leases(redis_client, NS, [SID])[SID]] == [second]
    call(gateway, token, "release_device", {"lease_id": "l_000000000000"})  # isError: nothing changes
    assert [x["id"] for x in mcp_servers.session_leases(redis_client, NS, [SID])[SID]] == [second]
    call(gateway, token, "release_device")  # the only one held
    assert mcp_servers.session_leases(redis_client, NS, [SID]) == {SID: []}


def test_the_sweep_releases_what_an_expired_token_held(redis_client, repo, seiji, token):
    app = build_app(lambda: RedisKV(redis_client), namespace=NS, mcp=repo, sweep_interval_s=0.2)
    with Server(app) as server:
        lease = acquire(server.url, token)
        mcp_servers.revoke_token(redis_client, NS, token)  # a crash: the token simply expires
        deadline = time.monotonic() + 10
        while state(seiji)["leases"][0]["status"] != "released" and time.monotonic() < deadline:
            time.sleep(0.1)
    app.state.stop_sweep.set()
    assert state(seiji)["leases"] == [{**state(seiji)["leases"][0], "lease_id": lease, "status": "released"}]
    assert mcp_servers.session_leases(redis_client, NS, [SID]) == {SID: []}


def test_a_live_token_is_never_swept(gateway, seiji, token, redis_client, repo):
    acquire(gateway, token)
    assert mcp_servers.sweep_held(redis_client, NS, repo) == 0
    assert state(seiji)["leases"][0]["status"] == "ready"


def test_a_release_that_cannot_reach_seiji_is_left_for_the_sweep(gateway, seiji, token, redis_client, repo):
    lease = acquire(gateway, token)
    stored = repo.get("seiji")
    definition = {k: v for k, v in stored.items() if k not in ("name", "headers_set")}
    repo.put("seiji", {**definition, "url": "http://127.0.0.1:9/mcp"})  # Seiji is down
    McpLaunch(redis_client, NS, repo, gateway).end(token)
    assert [x["id"] for x in mcp_servers.session_leases(redis_client, NS, [SID])[SID]] == [lease]
    repo.put("seiji", definition)  # Seiji is back
    assert mcp_servers.sweep_held(redis_client, NS, repo) == 1
    assert state(seiji)["leases"][0]["status"] == "released"
    assert mcp_servers.session_leases(redis_client, NS, [SID]) == {SID: []}


# -- the session list and the viewer ------------------------------------------------------
@pytest.fixture
def api(redis_client, repo):
    services = make_services(redis_client)
    services.mcp = repo
    services.namespace = NS
    return services, client_for(services), tokens_for(services)


def test_sessions_show_their_held_leases(api, gateway, token):
    _, client, tokens = api
    lease = acquire(gateway, token)
    items = client.get("/v1/sessions", headers=bearer(tokens["viewer"])).json()["items"]
    assert next(i for i in items if i["sid"] == SID)["mcp_leases"] == [
        {"server": "seiji", "id": lease, "model": "iphone-16", "platform": "ios"}]
    detail = client.get(f"/v1/sessions/{SID}", headers=bearer(tokens["viewer"])).json()
    assert detail["meta"]["mcp_leases"][0]["id"] == lease


def test_the_viewer_opens_only_a_lease_that_a_live_session_holds(api, gateway, seiji, token, redis_client):
    services, client, tokens = api
    operator = bearer(tokens["operator"])
    lease = acquire(gateway, token)
    # a lease that exists on Seiji but no session holds: refused before Seiji is asked
    other = json.loads(text_of(call(gateway, token, "acquire_device", {"platform": "ios"}, 3)))["lease_id"]
    mcp_servers.forget_held(redis_client, NS, token, "seiji", other)
    refused = client.post("/v1/mcp/seiji/viewer", json={"lease_id": other}, headers=operator)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "lease_not_held"
    assert client.post("/v1/mcp/seiji/viewer", json={"lease_id": lease},
                       headers=bearer(tokens["viewer"])).status_code == 403
    link = client.post("/v1/mcp/seiji/viewer", json={"lease_id": lease}, headers=operator)
    assert link.status_code == 200 and link.headers["cache-control"] == "no-store"
    body = link.json()
    assert set(body) == {"url", "expires_in", "control"} and body["control"] is False
    assert body["url"].startswith(f"{seiji}/view/open/") and body["expires_in"] == 60
    assert state(seiji)["viewers"] == [{"lease_id": lease, "control": False}]
    # without publicUrl, Seiji's relative link becomes absolute against http_base
    httpx.post(f"{seiji}/_control/config", json={"public_url": None})
    driven = client.post("/v1/mcp/seiji/viewer", json={"lease_id": lease, "control": True}, headers=operator)
    assert driven.json()["url"].startswith(f"{seiji}/view/open/") and driven.json()["control"] is True
    audit = [e for e in services.store.audit()["items"] if e["action"] == "mcp.viewer"]
    assert len(audit) == 2 and all("/view/" not in json.dumps(e) for e in audit)
    # a session that ended no longer holds anything that can be watched
    mcp_servers.revoke_token(redis_client, NS, token)
    gone = client.post("/v1/mcp/seiji/viewer", json={"lease_id": lease}, headers=operator)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "lease_not_held"


# -- the admin test ----------------------------------------------------------------------
def test_the_admin_test_reports_v1_me(api, repo):
    _, client, tokens = api
    result = client.post("/v1/admin/mcp/seiji/test", headers=bearer(tokens["admin"])).json()
    assert result["ok"] is True and result["protocol_version"] == "2025-11-25"
    assert result["me"] == {"user": mock_seiji.USER, "quota": mock_seiji.QUOTA}
    assert set(result["tools"]) >= {"budget_status", "acquire_device", "release_device", "export_evidence"}
    repo.set_headers("seiji", {"Authorization": "Bearer sj_wrong"})
    rejected = client.post("/v1/admin/mcp/seiji/test", headers=bearer(tokens["admin"])).json()
    assert rejected["ok"] is False and rejected["error"]["code"] == "rejected"
    assert "sj_wrong" not in json.dumps(rejected)
