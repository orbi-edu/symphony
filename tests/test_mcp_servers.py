"""MCP servers (A1, A2, A6): definitions and their checks, the three stores, tokens, and what
an agent launch stages. A stored header value never reaches a staged file, an API answer or
Redis."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from types import SimpleNamespace

import pytest
from kubernetes import client as k8s
from tests.test_merger_support import ScriptedAgents
from tests.test_merger_units import _spec
from tests.test_provider_runtime import ShellSandbox

from symphony import mcp_servers, provider_runtime
from symphony.config import Config
from symphony.mcp_servers import McpError, McpLaunch, McpRepository
from symphony.merger import agents as ag
from symphony.sessions import SessionStore

NS = "t"
SECRET = "mcp-header-secret-value-0123"
GATEWAY = "http://symphony-gateway.t.svc.cluster.local:8090"


@pytest.fixture
def repo(tmp_path):
    return McpRepository(None, NS, local_path=tmp_path / "mcp.json")


def remote(**changes):
    return {"transport": "http", "url": "https://mcp.example.com/mcp", "roles": ["reviewer"], **changes}


def stdio(**changes):
    return {"transport": "stdio", "command": "npx", "args": ["-y", "docs-mcp"],
            "env": {"DOCS_REGION": "eu"}, "roles": ["implementer"], **changes}


def field_of(call) -> str:
    with pytest.raises(McpError) as caught:
        call()
    assert caught.value.code == "invalid_mcp_server" and caught.value.status == 422
    return caught.value.field


# -- definitions -------------------------------------------------------------------------
def test_definitions_are_checked_field_by_field(repo, monkeypatch):
    monkeypatch.delenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", raising=False)
    assert field_of(lambda: repo.put("Bad Name", remote())) == "name"
    assert field_of(lambda: repo.put("x" * 49, remote())) == "name"
    assert field_of(lambda: repo.put("docs", remote(transport="ws"))) == "transport"
    assert field_of(lambda: repo.put("docs", remote(url="http://mcp.example.com/mcp"))) == "url"
    assert field_of(lambda: repo.put("docs", remote(url="https://user:pw@mcp.example.com/"))) == "url"
    assert field_of(lambda: repo.put("docs", remote(roles=["owner"]))) == "roles"
    assert field_of(lambda: repo.put("docs", remote(command="x"))) == "command"
    assert field_of(lambda: repo.put("docs", remote(headers={"Authorization": SECRET}))) == "headers"
    assert field_of(lambda: repo.put("docs", remote(colour="red"))) == "colour"
    assert field_of(lambda: repo.put("docs", stdio(command=""))) == "command"
    assert field_of(lambda: repo.put("docs", stdio(url="https://x.example.com"))) == "url"
    # stdio env lands in the agent's files: only non-secret values belong there
    assert field_of(lambda: repo.put("docs", stdio(env={"DOCS_API_KEY": "k"}))) == "env.DOCS_API_KEY"
    assert field_of(lambda: repo.put("docs", stdio(env={"GITHUB_TOKEN": "k"}))) == "env.GITHUB_TOKEN"
    assert repo.list() == []


def test_plain_http_is_only_for_listed_hosts(repo, monkeypatch):
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1, mcp-mock")
    assert repo.put("mock", remote(url="http://mcp-mock:4600/mcp"))["url"] == "http://mcp-mock:4600/mcp"
    assert field_of(lambda: repo.put("other", remote(url="http://other:4600/mcp"))) == "url"


def test_roles_are_the_team_roles_at_call_time(repo, monkeypatch):
    from symphony import teamconfig

    stored = repo.put("docs", remote(roles=["planner", "reviewer", "reviewer"]))
    assert stored["roles"] == ["reviewer", "planner"]  # ROLES order, once each
    monkeypatch.setattr(teamconfig, "ROLES", (*teamconfig.ROLES, "auditor"))
    assert repo.put("docs", remote(roles=["auditor"]))["roles"] == ["auditor"]


# -- the local store ---------------------------------------------------------------------
def test_local_store_is_private_and_headers_are_write_only(repo, tmp_path):
    repo.put("docs", remote(timeout_ms=5000, instructions=False))
    assert repo.set_headers("docs", {"Authorization": f"Bearer {SECRET}", "X-Team": "eng"}) == [
        "Authorization", "X-Team"]
    path = tmp_path / "mcp.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [listed] = repo.list()
    assert listed == {"name": "docs", **remote(), "enabled": True, "timeout_ms": 5000,
                      "instructions": False, "headers_set": ["Authorization", "X-Team"]}
    assert SECRET not in json.dumps(repo.list()) and SECRET not in json.dumps(repo.get("docs"))
    assert repo.headers("docs")["Authorization"] == f"Bearer {SECRET}"
    # same keys as the Secret: servers.json and headers.<name>.json
    assert set(json.loads(path.read_text())) == {"servers.json", "headers.docs.json"}
    # a PUT replaces the definition and keeps the stored headers
    assert repo.put("docs", remote(roles=["oracle"]))["headers_set"] == ["Authorization", "X-Team"]
    # None removes one header, case-insensitively; the last removal drops the key
    assert repo.set_headers("docs", {"x-team": None}) == ["Authorization"]
    assert repo.set_headers("docs", {"Authorization": None}) == []
    assert set(json.loads(path.read_text())) == {"servers.json"}
    repo.remove("docs")
    assert repo.list() == []


def test_header_rules(repo):
    repo.put("docs", remote())
    repo.put("local", stdio())
    assert field_of(lambda: repo.set_headers("local", {"Authorization": SECRET})) == "headers"
    assert field_of(lambda: repo.set_headers("docs", {"MCP-Session-Id": "x"})) == "headers"
    assert field_of(lambda: repo.set_headers("docs", {"Authorization": "a\r\nInjected: 1"})) == "headers.Authorization"
    assert field_of(lambda: repo.set_headers("docs", {})) == "headers"
    with pytest.raises(McpError) as missing:
        repo.set_headers("nope", {"Authorization": SECRET})
    assert missing.value.status == 404
    repo.set_headers("docs", {"Authorization": SECRET})
    assert field_of(lambda: repo.put("docs", stdio())) == "transport"  # headers first
    with pytest.raises(McpError) as gone:
        repo.remove("nope")
    assert gone.value.status == 404


def test_for_role_lists_enabled_servers_of_that_role(repo):
    repo.put("a", remote(roles=["reviewer", "oracle"]))
    repo.put("b", remote(roles=["reviewer"], enabled=False))
    repo.put("c", stdio(roles=["oracle"]))
    assert [d["name"] for d in repo.for_role("reviewer")] == ["a"]
    assert [d["name"] for d in repo.for_role("oracle")] == ["a", "c"]
    assert repo.for_role("planner") == []


# -- the Secret and the gateway's mount --------------------------------------------------
class SecretCore:
    def __init__(self, exists=True):
        self.secret = k8s.V1Secret(metadata=k8s.V1ObjectMeta(name="symphony-mcp", resource_version="7"),
                                   data={}) if exists else None
        self.patches = []

    def read_namespaced_secret(self, name=None, namespace=None, **kwargs):
        if self.secret is None:
            raise ApiStatus(404)
        return self.secret

    def patch_namespaced_secret(self, name=None, namespace=None, body=None, **kwargs):
        self.patches.append(body)
        if body["metadata"]["resourceVersion"] != self.secret.metadata.resource_version:
            raise ApiStatus(409)
        for key, value in body["data"].items():
            if value is None:
                self.secret.data.pop(key, None)
            else:
                self.secret.data[key] = value
        self.secret.metadata.resource_version = str(int(self.secret.metadata.resource_version) + 1)


class ApiStatus(Exception):
    """A kubernetes ApiException stand-in."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def no_secret(*args, **kwargs):
    raise ApiStatus(404)


def test_secret_store_patches_named_keys_with_optimistic_concurrency():
    core = SecretCore()
    repository = McpRepository(core, NS)
    repository.put("docs", remote())
    repository.set_headers("docs", {"Authorization": SECRET})
    assert set(core.secret.data) == {"servers.json", "headers.docs.json"}
    assert all(p["metadata"]["resourceVersion"] for p in core.patches)
    assert json.loads(base64.b64decode(core.secret.data["headers.docs.json"])) == {"Authorization": SECRET}
    assert repository.list()[0]["headers_set"] == ["Authorization"]
    repository.remove("docs")
    assert core.patches[-1]["data"] == {"servers.json": base64.b64encode(b"{}").decode(),
                                        "headers.docs.json": None}

    stale = McpRepository(core, NS)
    real_read = core.read_namespaced_secret
    core.read_namespaced_secret = lambda **kw: k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(resource_version="1"), data=dict(real_read().data))
    with pytest.raises(McpError) as conflict:
        stale.put("docs", remote())
    assert conflict.value.status == 409


def test_a_missing_secret_is_empty_and_not_writable():
    repository = McpRepository(SecretCore(exists=False), NS)
    assert repository.list() == [] and repository.for_role("reviewer") == []
    with pytest.raises(McpError) as caught:
        repository.put("docs", remote())
    assert caught.value.code == "mcp_not_bootstrapped" and caught.value.status == 503


def test_the_gateway_reads_the_mounted_secret(tmp_path):
    (tmp_path / "..data").mkdir()  # kubelet's own entries are not keys
    (tmp_path / "servers.json").write_text(json.dumps({"docs": mcp_servers.validate(remote())}))
    (tmp_path / "headers.docs.json").write_text(json.dumps({"Authorization": SECRET}))
    repository = McpRepository(None, NS, mount_dir=tmp_path)
    assert repository.get("docs")["headers_set"] == ["Authorization"]
    assert repository.headers("docs") == {"Authorization": SECRET}
    with pytest.raises(McpError):
        repository.put("other", remote())
    assert McpRepository(None, NS, mount_dir=tmp_path / "absent").list() == []


# -- tokens ------------------------------------------------------------------------------
def test_tokens_are_hashed_in_redis_with_a_ttl_and_revocable(redis_client):
    token = mcp_servers.mint_token(redis_client, NS, "reviewer", "merger:7:reviewer:1", ttl_s=600)
    [key] = redis_client.keys(f"sym:{NS}:mcp:token:*")
    assert token not in key and token not in redis_client.get(key)
    assert 590 <= redis_client.ttl(key) <= 600
    record = mcp_servers.token_record(redis_client, NS, token)
    assert record["role"] == "reviewer" and record["session"] == "merger:7:reviewer:1"
    assert mcp_servers.token_record(redis_client, NS, token + "x") is None
    mcp_servers.revoke_token(redis_client, NS, token)
    assert mcp_servers.token_record(redis_client, NS, token) is None
    with pytest.raises(ValueError):
        mcp_servers.mint_token(redis_client, NS, "owner", "s")


def test_an_expired_token_is_dead_even_before_redis_drops_it(redis_client):
    token = mcp_servers.mint_token(redis_client, NS, "reviewer", "s")
    key = mcp_servers.token_key(NS, token)
    record = json.loads(redis_client.get(key))
    redis_client.set(key, json.dumps({**record, "exp": record["exp"] - 10 * 3600}), ex=60)
    assert mcp_servers.token_record(redis_client, NS, token) is None


# -- staging -----------------------------------------------------------------------------
def staged_files(agent_dir):
    return {name: (agent_dir / name).read_text() for name in os.listdir(agent_dir)
            if not name.endswith(".pending")}


def test_stage_mcp_points_remote_servers_at_the_gateway(repo, tmp_path):
    repo.put("docs", remote(roles=["reviewer"], timeout_ms=9000))
    repo.put("tools", stdio(roles=["reviewer"]))
    repo.put("off", remote(roles=["reviewer"], enabled=False))
    repo.put("other", remote(roles=["oracle"]))
    repo.set_headers("docs", {"Authorization": f"Bearer {SECRET}"})
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "models.yml").write_text("{}")
    names = mcp_servers.stage_mcp(mcp_servers.write_private_file, str(agent_dir), "reviewer",
                                  "mcp_agenttoken", GATEWAY, repository=repo)
    assert names == ["docs", "tools"]
    files = staged_files(agent_dir)
    assert files["models.yml"] == "{}"  # never overwritten
    assert all(SECRET not in text for text in files.values())
    assert json.loads(files["mcp.json"]) == {"mcpServers": {
        "docs": {"type": "http", "url": f"{GATEWAY}/v1/mcp/docs",
                 "headers": {"Authorization": "Bearer mcp_agenttoken"}, "timeout": 9000},
        "tools": {"type": "stdio", "command": "npx", "args": ["-y", "docs-mcp"],
                  "env": {"DOCS_REGION": "eu"}},
    }}
    assert files["config.yml"] == "mcp:\n  enableProjectConfig: false\ntools:\n  xdev: false\n"
    for name in ("mcp.json", "config.yml"):
        assert stat.S_IMODE((agent_dir / name).stat().st_mode) == 0o600


def test_a_role_without_servers_still_ignores_project_config(repo, tmp_path):
    assert mcp_servers.stage_mcp(mcp_servers.write_private_file, str(tmp_path), "planner", "t",
                                 GATEWAY, repository=repo) == []
    assert json.loads((tmp_path / "mcp.json").read_text()) == {"mcpServers": {}}
    assert "enableProjectConfig: false" in (tmp_path / "config.yml").read_text()


def test_the_workspace_stages_mcp_in_its_private_agent_dir(redis_client, repo, tmp_path, monkeypatch):
    directory = tmp_path / "private" / "engine"
    monkeypatch.setattr(provider_runtime, "AGENT_DIR", str(directory))
    monkeypatch.setattr(provider_runtime, "ENV_FILE", str(directory.parent / "engine.env"))
    repo.put("docs", remote(roles=["implementer"]))
    repo.set_headers("docs", {"Authorization": SECRET})
    launch = McpLaunch(redis_client, NS, repo, GATEWAY)
    tokens = []
    sandbox = ShellSandbox()
    core = SimpleNamespace(read_namespaced_secret=no_secret)
    provider_runtime.prepare_provider(
        sandbox, core, NS, Config(tracker_kind="memory", engine_provider="amazon-bedrock", engine_model="m"),
        lambda write, agent_dir: tokens.append(launch.stage(write, agent_dir, "implementer", "issue-1:3")),
    )
    staged = json.loads((directory / "mcp.json").read_text())["mcpServers"]["docs"]
    assert staged["headers"] == {"Authorization": f"Bearer {tokens[0]}"}
    assert mcp_servers.token_record(redis_client, NS, tokens[0])["role"] == "implementer"
    assert (directory / "models.yml").exists() and (directory / "config.yml").exists()
    assert all(SECRET not in " ".join(command) for command in sandbox.commands)
    assert SECRET not in "".join(p.read_text() for p in directory.iterdir())


def test_merger_agents_get_their_role_servers_and_the_token_dies_with_the_run(
        redis_client, repo, tmp_path):
    repo.put("docs", remote(roles=["reviewer"]))
    repo.set_headers("docs", {"Authorization": SECRET})
    agents = ScriptedAgents()
    runner = ag.AgentRunner(SessionStore(redis_client, NS), session_root=str(tmp_path), spawn=agents.spawn,
                            silence_s=5, command_timeout_s=5, mcp=McpLaunch(redis_client, NS, repo, GATEWAY))
    assert runner.run(_spec()).status == "completed"
    proc = agents.spawned[-1]
    agent_dir = proc.env["PI_CODING_AGENT_DIR"]
    assert agent_dir.startswith(str(tmp_path))
    staged = json.loads(proc.mcp_json)["mcpServers"]
    token = staged["docs"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert mcp_servers.token_record(redis_client, NS, token) is None  # revoked at the end
    assert proc.argv[proc.argv.index("--tools") + 1] == "read,grep"  # the allowlist stays
    assert SECRET not in json.dumps(proc.env) and SECRET not in " ".join(proc.argv)
    for key in redis_client.keys("*"):
        if redis_client.type(key) == "string":
            assert SECRET not in redis_client.get(key)


# -- the legacy SSE endpoint -------------------------------------------------------------
async def _rewritten(items, definition):
    async def lines():
        for item in items:
            yield item

    return [chunk async for chunk in mcp_servers.sse_rewritten(lines(), definition, "docs")]


def test_legacy_sse_endpoint_events_point_at_the_gateway():
    definition = {"transport": "sse", "url": "https://mcp.example.com/sse"}
    stream = ["event: endpoint", "data: /messages?session=abc", "", "event: message",
              'data: {"jsonrpc":"2.0"}', "", "event: endpoint", "data: https://evil.example.net/x", ""]
    out = asyncio.run(_rewritten(stream, definition))
    assert out[0] == b"event: endpoint\ndata: /v1/mcp/docs/messages?session=abc\n\n"
    assert out[1] == b'event: message\ndata: {"jsonrpc":"2.0"}\n\n'
    assert b"evil" not in out[2]
    assert mcp_servers.upstream_url(definition, "messages", "session=abc") == \
        "https://mcp.example.com/messages?session=abc"
