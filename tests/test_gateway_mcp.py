"""The gateway's MCP proxy (A3) and the Admin MCP routes (A4), end to end against the MCP mock:
the agent's token decides access, the stored header reaches the server, the agent's bearer
never does, and streams pass through unbuffered both ways. Local mode stages the same through
its own gateway, without touching the host's omp agent dir."""

from __future__ import annotations

import json
import queue
import shlex
import threading
import time
from pathlib import Path

import httpx
import pytest
import redis.asyncio
from scripts.mocks.mcp import server as mock_mcp
from tests.api_support import Server, bearer, client_for, make_services, tokens_for
from tests.test_local_stack import WORKFLOW, LocalFakeEngine, until

from symphony import mcp_servers, state
from symphony.entry.local import LocalStack
from symphony.gateway_api import build_app
from symphony.kv import RedisKV
from symphony.mcp_servers import McpRepository

NS = "test"
ACCEPT = "application/json, text/event-stream"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "omp", "version": "18.20.2"}}}
STORED = f"Bearer {mock_mcp.DEFAULT_TOKEN}"


@pytest.fixture
def upstream():
    srv = mock_mcp.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def repo(tmp_path, upstream, monkeypatch):
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1")
    repository = McpRepository(None, NS, local_path=tmp_path / "mcp.json")
    repository.put("mock", {"transport": "http", "url": f"{upstream}/mcp", "roles": ["reviewer", "assistant"]})
    repository.set_headers("mock", {"Authorization": STORED})
    return repository


@pytest.fixture
def gateway(redis_client, repo):
    with Server(build_app(lambda: RedisKV(redis_client), namespace=NS, mcp=repo)) as server:
        yield server.url


def recorded(upstream):
    return httpx.get(f"{upstream}/_control/requests").json()["requests"]


def send(gateway, message, token, *, name="mock", session=None, extra=None):
    headers = {"Accept": ACCEPT, **(bearer(token) if token else {})}
    if session:
        headers |= {"MCP-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"}
    return httpx.post(f"{gateway}/v1/mcp/{name}", json=message, headers=headers | (extra or {}), timeout=10)


def handshake(gateway, token) -> str:
    response = send(gateway, INIT, token)
    assert response.status_code == 200, response.text
    session = response.headers["MCP-Session-Id"]
    assert send(gateway, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token,
                session=session).status_code == 202
    return session


def test_a_live_token_reaches_the_server_with_the_stored_header_only(gateway, upstream, redis_client):
    token = mcp_servers.mint_token(redis_client, NS, "reviewer", "merger:7:reviewer:1")
    session = handshake(gateway, token)
    called = send(gateway, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "echo", "arguments": {"text": "through"}}},
                  token, session=session, extra={"X-Agent-Note": "x", "Cookie": "c=1"})
    assert called.json()["result"]["content"][0]["text"] == "through"
    # the mock accepts only its own token, which the agent never had: the gateway added it
    seen = recorded(upstream)
    assert all(r["has_auth"] for r in seen)
    assert "x-agent-note" not in seen[-1]["headers"] and "cookie" not in seen[-1]["headers"]
    assert {"mcp-session-id", "mcp-protocol-version", "accept"} <= set(seen[-1]["headers"])
    assert mcp_servers.call_counts(redis_client, NS) == {"mock": {"reviewer": 3}}


def test_the_agents_bearer_never_reaches_the_server(gateway, upstream, repo, redis_client):
    repo.set_headers("mock", {"Authorization": None, "X-Api-Key": "k"})
    token = mcp_servers.mint_token(redis_client, NS, "reviewer", "s")
    response = send(gateway, INIT, token)
    # status and body pass through unchanged: the mock's own 401
    assert response.status_code == 401 and response.headers["WWW-Authenticate"].startswith("Bearer")
    [seen] = recorded(upstream)
    assert seen["has_auth"] is False and "x-api-key" in seen["headers"]


@pytest.mark.parametrize("case", ["role", "revoked", "expired", "no_token", "unknown", "disabled",
                                  "stdio", "not_mcp"])
def test_everything_else_is_one_opaque_403(case, gateway, upstream, repo, redis_client):
    role = "planner" if case == "role" else "reviewer"
    token = mcp_servers.mint_token(redis_client, NS, role, "s")
    name = "mock"
    if case == "revoked":
        mcp_servers.revoke_token(redis_client, NS, token)
    elif case == "expired":
        key = mcp_servers.token_key(NS, token)
        redis_client.set(key, json.dumps({**json.loads(redis_client.get(key)), "exp": int(time.time()) - 1}))
    elif case == "no_token":
        token = ""
    elif case == "unknown":
        name = "absent"
    elif case == "disabled":
        repo.put("mock", {"transport": "http", "url": f"{upstream}/mcp", "roles": ["reviewer"], "enabled": False})
    elif case == "stdio":
        repo.put("local", {"transport": "stdio", "command": "x", "roles": ["reviewer"]})
        name = "local"
    elif case == "not_mcp":
        token = "sym_not_an_mcp_token"
    response = send(gateway, INIT, token, name=name)
    assert response.status_code == 403 and response.json() == {"error": "mcp_forbidden"}
    assert recorded(upstream) == []


def test_sse_passes_through_unbuffered(gateway, upstream, redis_client):
    token = mcp_servers.mint_token(redis_client, NS, "assistant", "assist:1")
    session = handshake(gateway, token)
    httpx.post(f"{upstream}/_control/config", json={"sse": True, "sse_delay_s": 1.5})
    headers = {"Accept": ACCEPT, "MCP-Session-Id": session, "MCP-Protocol-Version": "2025-11-25", **bearer(token)}
    started = time.monotonic()
    with httpx.stream("POST", f"{gateway}/v1/mcp/mock", headers=headers, timeout=10,
                      json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"}) as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.iter_lines()
        assert next(lines).startswith("id: ")
        assert time.monotonic() - started < 1.0  # the priming event, before the 1.5 s pause
        rest = [line for line in lines if line.startswith("data: {")]
    assert json.loads(rest[0][6:])["id"] == 9
    assert time.monotonic() - started >= 1.5


def test_closing_the_client_closes_the_upstream_stream(gateway, upstream, redis_client):
    token = mcp_servers.mint_token(redis_client, NS, "reviewer", "s")
    session = handshake(gateway, token)
    headers = {"Accept": "text/event-stream", "MCP-Session-Id": session, **bearer(token)}
    with httpx.stream("GET", f"{gateway}/v1/mcp/mock", headers=headers, timeout=10) as stream:
        assert stream.status_code == 200
        assert next(stream.iter_lines()).startswith("id: ")
        assert httpx.get(f"{upstream}/_control/streams").json() == {"open": 1}
    deadline = time.monotonic() + 5
    while httpx.get(f"{upstream}/_control/streams").json()["open"] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert httpx.get(f"{upstream}/_control/streams").json() == {"open": 0}


def test_no_secret_in_redis(gateway, redis_client):
    token = mcp_servers.mint_token(redis_client, NS, "reviewer", "s")
    handshake(gateway, token)
    for key in redis_client.keys("*"):
        kind = redis_client.type(key)
        values = [redis_client.get(key)] if kind == "string" else (
            list(redis_client.hgetall(key).items()) if kind == "hash" else [])
        assert STORED not in json.dumps(values) and token not in json.dumps(values) and token not in key


# -- Admin (A4) --------------------------------------------------------------------------
@pytest.fixture
def admin(redis_client, repo):
    services = make_services(redis_client)
    services.mcp = repo
    return services, client_for(services), tokens_for(services)


def test_admin_lists_saves_and_removes_without_values(admin, upstream):
    services, client, tokens = admin
    listed = client.get("/v1/admin/mcp", headers=bearer(tokens["admin"]))
    assert listed.status_code == 200
    body = listed.json()
    assert body["servers"][0]["name"] == "mock" and body["servers"][0]["headers_set"] == ["Authorization"]
    assert "planner" in body["roles"] and STORED not in listed.text
    assert client.get("/v1/admin/mcp", headers=bearer(tokens["viewer"])).status_code == 403
    saved = client.put("/v1/admin/mcp/docs", headers=bearer(tokens["admin"]),
                       json={"transport": "stdio", "command": "docs-mcp", "env": {"REGION": "eu"},
                             "roles": ["researcher"]})
    assert saved.status_code == 200 and saved.json()["headers_set"] == []
    bad = client.put("/v1/admin/mcp/docs", headers=bearer(tokens["admin"]),
                     json={"transport": "http", "url": "ftp://x", "roles": []})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_mcp_server" and bad.json()["error"]["field"] == "url"
    headers = client.put("/v1/admin/mcp/mock/headers", headers=bearer(tokens["admin"]),
                         json={"headers": {"X-Team": "eng-secret-value", "Authorization": STORED}})
    assert headers.json() == {"headers_set": ["Authorization", "X-Team"]}
    assert "eng-secret-value" not in client.get("/v1/admin/mcp", headers=bearer(tokens["admin"])).text
    assert client.delete("/v1/admin/mcp/docs", headers=bearer(tokens["admin"])).status_code == 204
    assert client.delete("/v1/admin/mcp/docs", headers=bearer(tokens["admin"])).status_code == 404
    events = services.store.audit()["items"]
    assert {"admin.mcp.put", "admin.mcp.headers", "admin.mcp.remove"} <= {e["action"] for e in events}
    assert "eng-secret-value" not in json.dumps(events) and STORED not in json.dumps(events)


def test_admin_test_reports_the_mock_tools(admin, upstream):
    _, client, tokens = admin
    result = client.post("/v1/admin/mcp/mock/test", headers=bearer(tokens["admin"]))
    assert result.status_code == 200
    assert result.json() == {"ok": True, "tools": ["echo", "lookup_ticket_context"],
                             "protocol_version": "2025-11-25"}
    methods = [r["rpc_method"] for r in recorded(upstream) if r["method"] == "POST"]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]


def test_admin_test_reports_rejected_unreachable_and_stdio(admin, repo):
    _, client, tokens = admin
    repo.set_headers("mock", {"Authorization": "Bearer wrong"})
    rejected = client.post("/v1/admin/mcp/mock/test", headers=bearer(tokens["admin"])).json()
    assert rejected["ok"] is False and rejected["error"]["code"] == "rejected"
    assert "wrong" not in json.dumps(rejected)
    repo.put("gone", {"transport": "http", "url": "http://127.0.0.1:9/mcp", "roles": []})
    unreachable = client.post("/v1/admin/mcp/gone/test", headers=bearer(tokens["admin"])).json()
    assert unreachable["ok"] is False and unreachable["error"]["code"] == "unreachable"
    repo.put("local", {"transport": "stdio", "command": "x", "roles": []})
    stdio = client.post("/v1/admin/mcp/local/test", headers=bearer(tokens["admin"])).json()
    assert stdio["ok"] is None and stdio["note"] == "stdio servers are tested when an agent starts"
    assert client.post("/v1/admin/mcp/absent/test", headers=bearer(tokens["admin"])).status_code == 404


def test_operator_mcp_tool_lists_names_roles_and_transports(admin):
    _, client, tokens = admin
    response = client.post("/mcp", headers={"Accept": ACCEPT, **bearer(tokens["viewer"])}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mcp_servers", "arguments": {}}})
    content = response.json()["result"]["structuredContent"]
    assert content == {"items": [{"name": "mock", "transport": "http", "roles": ["reviewer", "assistant"],
                                  "enabled": True}]}
    assert "url" not in response.text and STORED not in response.text


# -- local mode --------------------------------------------------------------------------
def test_local_engines_get_mcp_through_the_local_gateway_and_keep_the_host_agent_dir(
        redis_client, tmp_path, upstream, monkeypatch):
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "host-agent"))
    engines: queue.Queue = queue.Queue()
    launched: list[dict] = []

    def open_engine(argv, cwd, env, stderr_path):
        launched.append({"argv": argv, "env": env})
        return engines.get(timeout=5)

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(WORKFLOW)
    socket_path = redis_client.connection_pool.connection_kwargs["path"]
    aclient = redis.asyncio.Redis(unix_socket_path=socket_path, decode_responses=True, socket_timeout=60)
    stack = LocalStack(client=redis_client, aclient=aclient, namespace=NS, root=tmp_path / "root",
                       workflow=workflow, open_engine=open_engine, intent_block_ms=50)
    stack.mcp.put("mock", {"transport": "http", "url": f"{upstream}/mcp", "roles": ["implementer"]})
    stack.mcp.set_headers("mock", {"Authorization": STORED})
    stack.start_http("127.0.0.1", 0, 0)
    loop = threading.Thread(target=stack.run_loop, daemon=True)
    loop.start()
    operator = stack.tokens.create("alice", "operator", actor="test")[0]
    api = httpx.Client(base_url=f"http://127.0.0.1:{stack.api_port}", headers=bearer(operator), timeout=10)
    try:
        until(lambda: stack._swept, what="the first tick")
        engine = LocalFakeEngine()
        engine.push({"type": "agent_start"})
        engine.push(lambda e: False)
        engines.put(engine)
        created = api.post("/v1/dev/issues", json={"identifier": "DEV-9", "title": "Use MCP"})
        assert created.status_code == 201, created.text
        issue_id = created.json()["id"]
        until(lambda: engine.prompted, timeout=10, what="the attempt to start")

        launch = launched[0]
        words = shlex.split(launch["argv"][-1])
        plugin = Path(words[words.index("--plugin-dir") + 1])
        overlay = Path(words[words.index("--config") + 1])
        assert launch["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path / "host-agent")  # untouched
        assert overlay.read_text() == mcp_servers.AGENT_CONFIG  # project .omp/mcp.json stays off
        assert json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["name"] == "symphony"
        staged = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["mock"]
        assert staged["url"] == f"http://127.0.0.1:{stack.gateway_port}/v1/mcp/mock"
        assert STORED not in json.dumps(staged) and STORED not in json.dumps(launch)
        token = staged["headers"]["Authorization"].removeprefix("Bearer ")
        # the engine's token works at the local gateway, which adds the stored header
        response = send(f"http://127.0.0.1:{stack.gateway_port}", INIT, token)
        assert response.status_code == 200 and recorded(upstream)[-1]["has_auth"] is True

        moved = api.post(f"/v1/dev/issues/{issue_id}/state", json={"state": "Cancelled"})
        assert moved.status_code == 200
        until(lambda: engine.terminated, timeout=10, what="the engine to be stopped")
        until(lambda: mcp_servers.token_record(redis_client, NS, token) is None, what="token revoked")
        until(lambda: stack.kv.get(state.issue_key(issue_id)) is None, what="terminal cleanup")
        assert not plugin.exists()
    finally:
        api.close()
        stack.shutdown()
        loop.join(5)
