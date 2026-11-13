"""MCP mock (A5): Streamable HTTP per the vendored MCP 2025-11-25 spec, with bearer auth,
sessions, SSE answers, 501 for what it does not serve, and a control API that records header
names only."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from scripts.mocks.mcp import server as mcp

ROOT = Path(__file__).resolve().parents[2]
TOKEN = mcp.DEFAULT_TOKEN
ACCEPT = "application/json, text/event-stream"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


@pytest.fixture
def mock():
    srv = mcp.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield base
    srv.shutdown()
    srv.server_close()


def post(base, message, *, token=TOKEN, session=None, headers=None):
    sent = {"Accept": ACCEPT, "Authorization": f"Bearer {token}"} if token else {"Accept": ACCEPT}
    if session:
        sent |= {"MCP-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"}
    return httpx.post(f"{base}/mcp", json=message, headers=sent | (headers or {}), timeout=5)


def start(base) -> str:
    response = post(base, INIT)
    assert response.status_code == 200
    mcp.validator("InitializeResult").validate(response.json()["result"])
    session = response.headers["MCP-Session-Id"]
    assert post(base, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session=session).status_code == 202
    return session


def test_lifecycle_tools_list_and_calls(mock):
    session = start(mock)
    listed = post(mock, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session=session).json()
    mcp.validator("ListToolsResult").validate(listed["result"])
    assert [t["name"] for t in listed["result"]["tools"]] == ["echo", "lookup_ticket_context"]
    echoed = post(mock, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "echo", "arguments": {"text": "hi"}}}, session=session).json()
    mcp.validator("CallToolResult").validate(echoed["result"])
    assert echoed["result"]["content"] == [{"type": "text", "text": "hi"}]
    looked = post(mock, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "lookup_ticket_context", "arguments": {"identifier": "ENG-1"}}}, session=session).json()
    assert looked["result"]["structuredContent"]["identifier"] == "ENG-1"
    unknown = post(mock, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "nope", "arguments": {}}}, session=session)
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == -32602


def test_auth_origin_and_accept_are_enforced(mock):
    assert post(mock, INIT, token=None).status_code == 401
    wrong = post(mock, INIT, token="other")
    assert wrong.status_code == 401 and wrong.headers["WWW-Authenticate"].startswith("Bearer")
    assert post(mock, INIT, headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(mock, INIT, headers={"Origin": "http://localhost:3000"}).status_code == 200
    assert post(mock, INIT, headers={"Accept": "application/json"}).status_code == 406


def test_sessions_and_protocol_version(mock):
    session = start(mock)
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    assert post(mock, listing).status_code == 400  # no session
    assert post(mock, listing, session="unknown").status_code == 404
    assert post(mock, listing, session=session, headers={"MCP-Protocol-Version": "1999-01-01"}).status_code == 400
    deleted = httpx.delete(f"{mock}/mcp", headers={"Authorization": f"Bearer {TOKEN}", "MCP-Session-Id": session})
    assert deleted.status_code == 200
    assert post(mock, listing, session=session).status_code == 404


def test_schema_violations_and_unserved_operations(mock):
    session = start(mock)
    bad = post(mock, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"arguments": {}}},
               session=session)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32602
    assert post(mock, [INIT]).status_code == 400  # no batches in 2025-11-25
    unserved = post(mock, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, session=session)
    assert unserved.status_code == 501 and unserved.json()["error"]["code"] == -32601
    assert httpx.post(f"{mock}/other", json={}).status_code == 501
    assert httpx.put(f"{mock}/mcp", json={}).status_code == 501


def test_sse_answers_start_with_a_priming_event(mock):
    httpx.post(f"{mock}/_control/config", json={"sse": True})
    response = post(mock, INIT)
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [block for block in response.text.split("\n\n") if block]
    assert events[0].startswith("id: ") and events[0].endswith("data: ")
    message = json.loads(events[1].split("data: ", 1)[1])
    mcp.validator("InitializeResult").validate(message["result"])
    assert response.headers["MCP-Session-Id"]


def test_server_stream_stays_open_until_the_client_leaves(mock):
    session = start(mock)
    headers = {"Accept": "text/event-stream", "Authorization": f"Bearer {TOKEN}", "MCP-Session-Id": session}
    with httpx.stream("GET", f"{mock}/mcp", headers=headers, timeout=5) as stream:
        assert stream.status_code == 200
        assert next(stream.iter_lines()).startswith("id: ")
        assert httpx.get(f"{mock}/_control/streams").json() == {"open": 1}
    deadline = time.monotonic() + 5
    while httpx.get(f"{mock}/_control/streams").json()["open"] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert httpx.get(f"{mock}/_control/streams").json() == {"open": 0}


def test_control_records_header_names_never_values(mock):
    start(mock)
    post(mock, INIT, token=None)
    body = httpx.get(f"{mock}/_control/requests").json()
    assert TOKEN not in json.dumps(body)
    first, _, last = body["requests"]
    assert first["rpc_method"] == "initialize" and first["has_auth"] is True
    assert "authorization" in first["headers"] and "accept" in first["headers"]
    assert last["has_auth"] is False
    httpx.post(f"{mock}/_control/reset")
    assert httpx.get(f"{mock}/_control/requests").json() == {"requests": []}


def test_vendored_specs_match_sources():
    specs = ROOT / "scripts" / "mocks" / "specs"
    rows = re.findall(r"^\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|$", (specs / "SOURCES.md").read_text(), re.M)
    mine = {name: sha for name, sha in rows if name.startswith("mcp-")}
    assert {"mcp-2025-11-25-transports.md", "mcp-2025-11-25-lifecycle.md", "mcp-2025-11-25-tools.md",
            "mcp-2025-11-25-schema.json.gz"} == set(mine)
    for name, sha in mine.items():
        data = (specs / name).read_bytes()
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        assert hashlib.sha256(data).hexdigest() == sha, name


def test_cli_starts_fast_and_reads_the_token_from_the_environment():
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.mocks.mcp", "--port", "0"], cwd=ROOT, stdout=subprocess.PIPE,
        text=True, env={"PATH": "/usr/bin:/bin", "MCP_MOCK_TOKEN": "env-token"},
    )
    try:
        started = time.monotonic()
        line = proc.stdout.readline()
        assert time.monotonic() - started < 3.0
        url = line.strip().rsplit(" ", 1)[-1]
        sent = {"Accept": ACCEPT, "Authorization": "Bearer env-token"}
        assert httpx.post(url, json=INIT, headers=sent).status_code == 200
    finally:
        proc.terminate()
        proc.wait(5)
