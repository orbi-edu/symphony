"""Real Seiji through Symphony's gateway (opt-in: `SYMPHONY_SEIJI_BIN=<path to seiji>`).

It runs `seiji user add` and `seiji serve` in shared mode with a temporary config and data
dir. This Mac has no simulator runtime, so `acquire_device` fails with "device failed to
start (not billed)". The test still proves auth, the headers, the protocol version, the
instructions, `tools/list`, `budget_status`, `/v1/me` and an upload through the gateway, and
that the pinned omp lists Seiji's tools through it (`--mode rpc`, `get_state`).

Build the binary once in the Seiji repository: `cargo build --release`, then
`SYMPHONY_SEIJI_BIN=<seiji repo>/target/release/seiji uv run pytest tests/test_seiji_real.py`.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time

import httpx
import pytest
from tests.api_support import Server, bearer

from symphony import mcp_servers
from symphony.gateway_api import build_app
from symphony.kv import RedisKV
from symphony.mcp_servers import McpLaunch, McpRepository

SEIJI = os.environ.get("SYMPHONY_SEIJI_BIN", "")
OMP = os.environ.get(
    "SYMPHONY_OMP_BIN", os.path.expanduser("~/Library/Application Support/Tern/omp/versions/18.20.2"))
NS = "real"
ACCEPT = "application/json, text/event-stream"
TOOLS = 23

pytestmark = pytest.mark.skipif(not SEIJI or not os.access(SEIJI, os.X_OK),
                                reason="real Seiji runs are opt-in (SYMPHONY_SEIJI_BIN)")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def seiji(tmp_path):
    """`seiji serve` on a free port; yields (base url, the user's token)."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    config = tmp_path / "seiji.json"
    config.write_text(json.dumps({
        "listen": {"host": "127.0.0.1", "port": port, "publicUrl": base},
        "dataDir": str(tmp_path / "data"), "seats": 2, "capacity": {"ios": 1, "android": 0},
        "defaultQuota": {"monthlyMinutes": 60, "maxConcurrent": 1, "maxLeaseMinutes": 10, "idleMinutes": 5},
        "isolation": {"mode": "shared"},
        "ios": {"models": {"iphone-16": {
            "deviceType": "com.apple.CoreSimulator.SimDeviceType.iPhone-16",
            "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-18-5"}}, "defaultModel": "iphone-16"},
    }))
    added = subprocess.run([SEIJI, "user", "add", "agent", "-c", str(config)],
                           capture_output=True, text=True, check=True, timeout=30)
    token = re.search(r"sj_[A-Za-z0-9_-]+", added.stdout + added.stderr).group(0)
    log = (tmp_path / "serve.log").open("w")
    proc = subprocess.Popen([SEIJI, "serve", "-c", str(config)], stdout=log, stderr=log, cwd=tmp_path)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=1).text == "ok":
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "seiji serve did not start"
            time.sleep(0.2)
        yield base, token
    finally:
        proc.terminate()
        proc.wait(30)
        log.close()


@pytest.fixture
def gateway(seiji, redis_client, tmp_path, monkeypatch):
    base, token = seiji
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1")
    repo = McpRepository(None, NS, local_path=tmp_path / "mcp.json")
    repo.put("seiji", {"preset": "seiji", "url": f"{base}/mcp", "roles": ["implementer"]})
    repo.set_headers("seiji", {"Authorization": f"Bearer {token}"})
    with Server(build_app(lambda: RedisKV(redis_client), namespace=NS, mcp=repo,
                          sweep_interval_s=None)) as server:
        yield server.url, repo


def _rpc(url, token, message, version=True):
    headers = {"Accept": ACCEPT, **bearer(token)}
    if version:
        headers["MCP-Protocol-Version"] = mcp_servers.PROTOCOL_VERSION
    return httpx.post(f"{url}/v1/mcp/seiji", json=message, headers=headers, timeout=60)


def test_seiji_works_through_the_gateway(gateway, seiji, redis_client):
    url, repo = gateway
    base, seiji_token = seiji
    token = mcp_servers.mint_token(redis_client, NS, "implementer", "real:1")
    initialized = _rpc(url, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        version=False)
    assert initialized.status_code == 200, initialized.text
    result = initialized.json()["result"]
    assert result["protocolVersion"] == "2025-11-25" and result["serverInfo"]["name"] == "seiji"
    assert "$SEIJI_TOKEN" in result["instructions"] and "mcp-session-id" not in initialized.headers
    tools = _rpc(url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert len(tools) == TOOLS and {"acquire_device", "release_device", "export_evidence"} <= {t["name"] for t in tools}
    budget = _rpc(url, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "budget_status", "arguments": {}}}).json()["result"]
    assert budget["isError"] is False and json.loads(budget["content"][0]["text"])["user"] == "agent"
    me = httpx.get(f"{url}/v1/mcp/seiji/http/v1/me", headers=bearer(token), timeout=10)
    assert me.json() == {"user": "agent", "quota": {"monthlyMinutes": 60, "maxConcurrent": 1,
                                                    "maxLeaseMinutes": 10, "idleMinutes": 5}}
    upload = httpx.post(f"{url}/v1/mcp/seiji/http/v1/apps", content=b"PK\x03\x04" + b"x" * 100_000,
                        headers={**bearer(token), "X-Filename": "My+App.zip"}, timeout=30)
    assert upload.status_code == 200 and upload.json()["filename"] == "My_App.zip"
    assert upload.json()["size"] == 100_004
    acquired = _rpc(url, token, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "acquire_device", "arguments": {"platform": "ios"}}}).json()
    if acquired["result"]["isError"]:  # no simulator runtime on this host
        assert acquired["result"]["content"][0]["text"].startswith("device failed to start (not billed)")
        assert mcp_servers.session_leases(redis_client, NS, ["real:1"]) == {"real:1": []}
    # the session token never reaches Seiji: Seiji refuses it directly
    assert httpx.get(f"{base}/v1/me", headers=bearer(token)).status_code == 401
    assert httpx.get(f"{base}/v1/me", headers=bearer(seiji_token)).status_code == 200


def test_the_admin_test_reads_v1_me(gateway):
    import asyncio

    _url, repo = gateway
    result = asyncio.run(mcp_servers.probe(repo.get("seiji"), repo.headers("seiji")))
    assert result["ok"] is True and len(result["tools"]) == TOOLS
    assert result["protocol_version"] == "2025-11-25" and result["me"]["user"] == "agent"


@pytest.mark.skipif(not os.access(OMP, os.X_OK), reason="the pinned omp is not installed")
def test_the_pinned_omp_lists_seijis_tools_through_the_gateway(gateway, redis_client, tmp_path):
    url, repo = gateway
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(mode=0o700)
    launch = McpLaunch(redis_client, NS, repo, url)
    token = launch.stage(mcp_servers.write_private_file, str(agent_dir), "implementer", "real:2")
    assert (agent_dir / mcp_servers.AGENT_ENV_FILE).read_text().count(token) == 1
    proc = subprocess.Popen([OMP, "--mode", "rpc", "--no-session"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=tmp_path,
                            env={**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)})
    try:
        names: list[str] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and len(names) < TOOLS:
            time.sleep(2)  # omp connects its MCP servers in the background
            proc.stdin.write(json.dumps({"id": "s", "type": "get_state"}) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                frame = json.loads(line)
                if frame.get("id") == "s":
                    names = [t["name"] for t in frame["data"]["dumpTools"]
                             if t.get("name", "").startswith("mcp__seiji_")]
                    break
        assert len(names) == TOOLS, names
        assert "mcp__seiji_acquire_device" in names and "mcp__seiji_release_device" in names
    finally:
        proc.terminate()
        proc.wait(10)
        launch.end(token)
