"""MCP streamable HTTP: initialize, notifications, tools/list, tools/call and role refusal."""

from __future__ import annotations

import json

import pytest
from tests.api_support import (
    FakeMerger,
    bearer,
    client_for,
    fresh_daemon,
    make_services,
    tokens_for,
)

from symphony.api.mcp import TOOLS

HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client, merger=FakeMerger())
    services.sessions.open("s:1", kind="implement", identifier="ENG-1", title="Login")
    services.sessions.update("s:1", state="running")
    return services, client_for(services), tokens_for(services)


def rpc(client, token, method, params=None, mid=1, headers=HEADERS):
    message = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        message["id"] = mid
    if params is not None:
        message["params"] = params
    return client.post("/mcp", headers=headers | bearer(token), json=message)


def call(client, token, name, arguments=None):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200
    return response.json()["result"]


def test_initialize_and_initialized_notification(api):
    _, client, tokens = api
    response = rpc(client, tokens["viewer"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "omp", "version": "18.20.2"}})
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert body["result"]["serverInfo"]["name"] == "symphony"
    note = rpc(client, tokens["viewer"], "notifications/initialized", mid=None)
    assert note.status_code == 202 and note.content == b""


def test_tools_list_has_the_contract_tools(api):
    _, client, tokens = api
    tools = rpc(client, tokens["viewer"], "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "list_sessions", "get_session", "send_message", "interrupt", "list_issues", "overview",
        "pause", "resume", "hold", "release", "merger_status", "setup_status", "mcp_servers"]
    assert all(t["inputSchema"]["type"] == "object" and "role" not in t for t in tools)


def test_read_tools(api):
    _, client, tokens = api
    sessions = call(client, tokens["viewer"], "list_sessions", {"state": "running"})
    assert [s["sid"] for s in sessions["structuredContent"]["items"]] == ["s:1"]
    assert json.loads(sessions["content"][0]["text"])[0]["sid"] == "s:1"
    detail = call(client, tokens["viewer"], "get_session", {"sid": "s:1"})
    assert detail["structuredContent"]["meta"]["identifier"] == "ENG-1"
    overview = call(client, tokens["viewer"], "overview")
    assert "counts" in overview["structuredContent"]
    merger = call(client, tokens["viewer"], "merger_status")["structuredContent"]
    assert [entry["repo"] for entry in merger["repos"]] == ["acme/app"]
    assert merger["repos"][0]["alive"] is True and merger["repos"][0]["queue"][0]["pr"] == 9
    assert call(client, tokens["viewer"], "list_issues")["structuredContent"] == {"items": []}


def test_write_tools_need_operator(api):
    services, client, tokens = api
    refused = call(client, tokens["viewer"], "send_message", {"sid": "s:1", "text": "hi"})
    assert refused["isError"] is True and "operator" in refused["content"][0]["text"]
    assert services.sessions.read_intents("s:1") == []
    refused_pause = call(client, tokens["viewer"], "pause", {"reason": "maintenance window"})
    assert refused_pause["isError"] is True
    sent = call(client, tokens["operator"], "send_message", {"sid": "s:1", "text": "hi"})
    assert sent["structuredContent"]["kind"] == "steer"
    [(_, kind, actor, payload)] = services.sessions.read_intents("s:1")
    assert (kind, actor, payload) == ("steer", "operator-person", {"text": "hi"})
    stopped = call(client, tokens["operator"], "interrupt", {"sid": "s:1"})
    assert stopped["structuredContent"]["kind"] == "interrupt"


def test_pause_tool_is_a_durable_command(api, redis_client):
    services, client, tokens = api
    unavailable = call(client, tokens["operator"], "pause", {"reason": "maintenance window"})
    assert unavailable["isError"] is True and "daemon_unavailable" in unavailable["content"][0]["text"]
    fresh_daemon(redis_client)
    accepted = call(client, tokens["operator"], "pause", {"reason": "maintenance window"})
    record = services.store.get(accepted["structuredContent"]["id"])
    assert record["action"] == "pause" and record["actor"] == "operator-person"


def test_tool_errors_and_protocol_errors(api):
    _, client, tokens = api
    missing = call(client, tokens["viewer"], "get_session", {"sid": "none:1"})
    assert missing["isError"] is True and "not_found" in missing["content"][0]["text"]
    bad_args = call(client, tokens["viewer"], "list_sessions", {"limit": "many"})
    assert bad_args["isError"] is True
    unknown = rpc(client, tokens["viewer"], "tools/call", {"name": "rm_rf", "arguments": {}})
    assert unknown.json()["error"]["code"] == -32602
    method = rpc(client, tokens["viewer"], "resources/list")
    assert method.json()["error"]["code"] == -32601
    version = rpc(client, tokens["viewer"], "tools/list",
                  headers=HEADERS | {"MCP-Protocol-Version": "1999-01-01"})
    assert version.status_code == 400
    parse = client.post("/mcp", headers=HEADERS | bearer(tokens["viewer"]), content=b"{nope")
    assert parse.status_code == 400 and parse.json()["error"]["code"] == -32700


def test_mcp_requires_the_bearer_token(api):
    _, client, _ = api
    response = client.post("/mcp", headers=HEADERS,
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert client.get("/mcp").status_code == 405


def test_every_tool_declares_a_role():
    assert {tool["role"] for tool in TOOLS} == {"viewer", "operator"}
