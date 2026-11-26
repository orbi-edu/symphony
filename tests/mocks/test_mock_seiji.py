"""Seiji mock: Seiji's `/mcp` rules in their documented order, the tools, the HTTP API and the
control API, per the vendored `seiji-mcp.md`."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from scripts.mocks.seiji import server as seiji

ROOT = Path(__file__).resolve().parents[2]
AUTH = {"Authorization": f"Bearer {seiji.DEFAULT_TOKEN}"}
HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


@pytest.fixture
def mock():
    srv = seiji.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def post(base, message, headers=None, **kwargs):
    return httpx.post(f"{base}/mcp", content=json.dumps(message), headers={**HEADERS, **(headers or {})},
                      **kwargs)


def tool(base, name, arguments=None):
    response = post(base, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_endpoint_checks_run_in_seijis_order(mock):
    # the token first: a bad token and a bad Host get 401
    bad = post(mock, INIT, {"Authorization": "Bearer sj_wrong", "Host": "evil.example"})
    assert bad.status_code == 401 and bad.text == "missing, invalid or disabled bearer token"
    assert post(mock, INIT, {"Authorization": f"bearer {seiji.DEFAULT_TOKEN}"}).status_code == 401
    host = post(mock, INIT, {"Host": "evil.example"})
    assert host.status_code == 403 and host.text == "Forbidden: Host header is not allowed"
    assert post(mock, INIT, {"Host": "localhost:9999"}).status_code == 200  # no port: every port
    get = httpx.get(f"{mock}/mcp", headers=AUTH)
    assert get.status_code == 405 and get.headers["allow"] == "POST"
    assert httpx.delete(f"{mock}/mcp", headers=AUTH).status_code == 405
    accept = post(mock, INIT, {"Accept": "application/json"})
    assert accept.status_code == 406 and accept.text.startswith("Not Acceptable")
    media = post(mock, INIT, {"Content-Type": "text/plain"})
    assert media.status_code == 415 and media.text.startswith("Unsupported Media Type")
    version = post(mock, INIT, {"MCP-Protocol-Version": "2099-01-01"})
    assert version.status_code == 400 and version.text == "Bad Request: Unsupported MCP-Protocol-Version: 2099-01-01"
    mismatch = post(mock, INIT, {"MCP-Protocol-Version": "2025-06-18"})
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == -32600


def test_initialize_is_stateless_and_json_only(mock):
    response = post(mock, INIT)
    assert response.headers["content-type"] == "application/json" and "mcp-session-id" not in response.headers
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-11-25" and result["serverInfo"] == {"name": "seiji", "version": "0.1.0"}
    assert '$SEIJI_TOKEN' in result["instructions"] and "release_device" in result["instructions"]
    older = post(mock, {**INIT, "params": {**INIT["params"], "protocolVersion": "2025-03-26"}})
    assert older.json()["result"]["protocolVersion"] == "2025-03-26"
    assert post(mock, {"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    # no initialize needed first, and no header means 2025-03-26
    listed = post(mock, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in listed.json()["result"]["tools"]} >= {
        "budget_status", "list_device_models", "acquire_device", "release_device", "export_evidence"}
    unknown = post(mock, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}})
    assert unknown.json()["error"] == {"code": -32602, "message": "tool not found"}


def test_leases_follow_the_lease_id_rule(mock):
    assert tool(mock, "release_device")["result"] == {
        "content": [{"type": "text", "text": "you hold no device; call acquire_device first"}], "isError": True}
    missing = tool(mock, "acquire_device")
    assert missing["error"]["message"] == "failed to deserialize parameters: missing field `platform`"
    android = tool(mock, "acquire_device", {"platform": "android"})["result"]
    assert android["isError"] and android["content"][0]["text"] == "android is not available on this host"
    first = json.loads(tool(mock, "acquire_device", {"platform": "ios"})["result"]["content"][0]["text"])
    assert re.fullmatch(r"l_[0-9a-f]{12}", first["lease_id"]) and first["status"] == "ready"
    second = json.loads(tool(mock, "acquire_device", {"platform": "ios"})["result"]["content"][0]["text"])
    full = tool(mock, "acquire_device", {"platform": "ios"})["result"]
    assert full["content"][0]["text"] == "you already hold 2 device(s), your limit is 2; release one first"
    assert tool(mock, "release_device")["result"]["content"][0]["text"] == "you hold 2 devices; pass lease_id"
    budget = json.loads(tool(mock, "budget_status")["result"]["content"][0]["text"])
    assert [lease["lease_id"] for lease in budget["active_leases"]] == [first["lease_id"], second["lease_id"]]
    released = tool(mock, "release_device", {"lease_id": first["lease_id"]})["result"]
    assert released == {"content": [{"type": "text", "text": f"released {first['lease_id']}"}], "isError": False}
    again = tool(mock, "release_device", {"lease_id": first["lease_id"]})["result"]
    assert again["content"][0]["text"] == f'no active lease "{first["lease_id"]}" for you'
    httpx.post(f"{mock}/_control/config", json={"acquire_error": "device failed to start (not billed): x"})
    failed = tool(mock, "acquire_device", {"platform": "ios"})["result"]
    assert failed["isError"] and failed["content"][0]["text"].startswith("device failed to start (not billed)")


def test_runs_evidence_and_links_use_the_public_url(mock):
    httpx.post(f"{mock}/_control/config", json={"public_url": "https://seiji.example.com"})
    tool(mock, "acquire_device", {"platform": "ios"})
    summary = json.loads(tool(mock, "run_actions", {"name": "Login", "actions": [{"launch_app": {}}]})
                         ["result"]["content"][0]["text"])
    run_id = summary["run_id"]
    assert summary["evidence_zip"] == f"https://seiji.example.com/v1/runs/{run_id}/evidence.zip"
    markdown = tool(mock, "export_evidence", {"run_id": run_id})["result"]["content"][0]["text"]
    assert markdown.startswith("### ✅ Login") and f"(https://seiji.example.com/v1/runs/{run_id}/evidence.zip)" in markdown
    report = httpx.get(f"{mock}/v1/runs/{run_id}/report.md", headers=AUTH)
    assert report.headers["content-type"] == "text/markdown; charset=utf-8" and report.text == markdown
    archive = zipfile.ZipFile(BytesIO(httpx.get(f"{mock}/v1/runs/{run_id}/evidence.zip", headers=AUTH).content))
    assert f"{run_id}/manifest.json" in archive.namelist()
    assert httpx.get(f"{mock}/v1/runs/r_000000000000/report.md", headers=AUTH).json() == {
        "error": 'no run "r_000000000000" for you'}
    assert httpx.get(f"{mock}/v1/runs/{run_id}/report.md").status_code == 401


def test_uploads_me_and_viewer(mock):
    assert httpx.post(f"{mock}/v1/apps", content=b"x", headers=AUTH).status_code == 400
    assert httpx.post(f"{mock}/v1/apps", content=b"", headers={**AUTH, "X-Filename": "a.zip"}).json() == {
        "error": "empty upload"}
    uploaded = httpx.post(f"{mock}/v1/apps", content=b"zipdata", headers={**AUTH, "X-Filename": ".My+App.zip"})
    assert uploaded.json()["filename"] == "My_App.zip" and uploaded.json()["size"] == 7
    assert httpx.get(f"{mock}/v1/me", headers=AUTH).json() == {"user": seiji.USER, "quota": seiji.QUOTA}
    lease = json.loads(tool(mock, "acquire_device", {"platform": "ios"})["result"]["content"][0]["text"])["lease_id"]
    link = httpx.post(f"{mock}/v1/leases/{lease}/viewer", headers=AUTH)  # control defaults to true
    assert link.json()["control"] is True and link.json()["url"].startswith(f"{mock}/view/open/")
    bad = httpx.post(f"{mock}/v1/leases/{lease}/viewer", headers=AUTH, json={"control": False, "x": 1})
    assert bad.status_code == 400 and bad.json()["error"].startswith("invalid body")
    gone = httpx.post(f"{mock}/v1/leases/l_000000000000/viewer", headers=AUTH)
    assert gone.status_code == 404 and gone.json() == {"error": 'no active lease "l_000000000000" for you'}
    httpx.post(f"{mock}/_control/config", json={"public_url": None})
    relative = httpx.post(f"{mock}/v1/leases/{lease}/viewer", headers=AUTH, json={"control": False})
    assert relative.json()["url"].startswith("/view/open/") and relative.headers["cache-control"] == "no-store"


def test_builds_are_limited_deleted_and_expired(mock):
    upload = {**AUTH, "X-Filename": "a.zip"}
    assert httpx.post(f"{mock}/_control/config", json={"max_builds": 1}).json()["max_builds"] == 1
    app_id = httpx.post(f"{mock}/v1/apps", content=b"zip", headers=upload).json()["app_id"]
    full = httpx.post(f"{mock}/v1/apps", content=b"zip", headers=upload)
    assert full.status_code == 429 and full.json() == {
        "error": "you already keep 1 builds, the limit is 1; they expire 72h after upload"}
    assert httpx.delete(f"{mock}/v1/apps/{app_id}").status_code == 401
    deleted = httpx.delete(f"{mock}/v1/apps/{app_id}", headers=AUTH)
    assert deleted.status_code == 204 and deleted.content == b""
    again = httpx.delete(f"{mock}/v1/apps/{app_id}", headers=AUTH)
    assert again.status_code == 404 and again.json() == {"error": f'no uploaded app "{app_id}"'}
    app_id = httpx.post(f"{mock}/v1/apps", content=b"zip", headers=upload).json()["app_id"]
    httpx.post(f"{mock}/_control/expire_apps")
    assert httpx.get(f"{mock}/_control/state").json()["apps"] == []
    lease = json.loads(tool(mock, "acquire_device", {"platform": "ios"})["result"]["content"][0]["text"])["lease_id"]
    gone = tool(mock, "install_app", {"lease_id": lease, "app_id": app_id})["result"]
    assert gone["isError"] and gone["content"][0]["text"] == f'no uploaded app "{app_id}"'
    httpx.post(f"{mock}/_control/reset")
    assert httpx.get(f"{mock}/_control/state").json()["max_builds"] == seiji.MAX_BUILDS


def test_control_records_header_names_and_foreign_bearers_by_hash(mock):
    post(mock, INIT)
    post(mock, INIT, {"Authorization": "Bearer mcp_agent_token"})
    seen = httpx.get(f"{mock}/_control/state").json()
    assert [r["auth"] for r in seen["requests"]] == ["valid", "other"]
    assert seen["requests"][0]["rpc_method"] == "initialize" and "authorization" in seen["requests"][0]["headers"]
    assert seen["other_bearers"] == [hashlib.sha256(b"mcp_agent_token").hexdigest()]
    assert seiji.DEFAULT_TOKEN not in json.dumps(seen) and "mcp_agent_token" not in json.dumps(seen)
    httpx.post(f"{mock}/_control/reset")
    assert httpx.get(f"{mock}/_control/state").json()["requests"] == []


def test_the_vendored_spec_matches_sources():
    specs = ROOT / "scripts" / "mocks" / "specs"
    rows = dict(re.findall(r"^\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|$", (specs / "SOURCES.md").read_text(), re.M))
    assert hashlib.sha256((specs / "seiji-mcp.md").read_bytes()).hexdigest() == rows["seiji-mcp.md"]
    text = (specs / "seiji-mcp.md").read_text()
    # the rules the mock enforces are the documented ones
    for rule in ("Bad Request: Unsupported MCP-Protocol-Version", "Forbidden: Host header is not allowed",
                 "missing, invalid or disabled bearer token", "Not Acceptable: Client must accept both",
                 "Unsupported Media Type: Content-Type must be application/json"):
        assert rule in text, rule


def test_cli_starts_and_reads_the_token_from_the_environment():
    proc = subprocess.Popen([sys.executable, "-m", "scripts.mocks.seiji", "--port", "0"], cwd=ROOT,
                            stdout=subprocess.PIPE, text=True,
                            env={"PATH": "/usr/bin:/bin", "SEIJI_MOCK_TOKEN": "sj_env-token"})
    try:
        started = time.monotonic()
        line = proc.stdout.readline()
        assert line.startswith("seiji mock listening on http://127.0.0.1:") and time.monotonic() - started < 10
        base = line.split()[-1].removesuffix("/mcp")
        assert httpx.get(f"{base}/v1/me", headers={"Authorization": "Bearer sj_env-token"}).status_code == 200
    finally:
        proc.terminate()
        proc.wait(5)
