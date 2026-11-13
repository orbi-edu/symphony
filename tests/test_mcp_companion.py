"""MCP companion fields (internals §18): `http_base`, `agent_env`, `rewrite_origins` and
`cleanup`, the presets, what an agent launch exports, and the prompt paragraph."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, runner_env
from tests.test_mcp_servers import no_secret
from tests.test_provider_runtime import ShellSandbox
from tests.test_runner import CFG, _claim

from symphony import mcp_servers, provider_runtime
from symphony.cli.main import build_parser, mcp_definition
from symphony.config import Config
from symphony.mcp_servers import McpError, McpLaunch, McpRepository
from symphony.runner import run_attempt
from symphony.trackers.memory import MemoryTracker

NS = "t"
GATEWAY = "http://symphony-gateway.t.svc.cluster.local:8090"
SEIJI_SECRET = "sj_" + "S" * 43


@pytest.fixture
def repo(tmp_path):
    return McpRepository(None, NS, local_path=tmp_path / "mcp.json")


def seiji(**changes):
    return {"preset": "seiji", "url": "https://seiji.example.com/mcp", "roles": ["implementer"], **changes}


def field_of(call) -> str:
    with pytest.raises(McpError) as caught:
        call()
    assert caught.value.code == "invalid_mcp_server"
    return caught.value.field


# -- presets -----------------------------------------------------------------------------
def test_the_seiji_preset_fills_a_plain_definition(repo):
    stored = repo.put("seiji", seiji())
    assert stored == {
        "name": "seiji", "transport": "http", "url": "https://seiji.example.com/mcp",
        "http_base": "https://seiji.example.com", "timeout_ms": 3_900_000,
        "agent_env": {"SEIJI_TOKEN": "{token}", "SEIJI_URL": "{http_url}"},
        "cleanup": {"acquire_tool": "acquire_device", "id_field": "lease_id",
                    "release_tool": "release_device", "release_arg": "lease_id"},
        "roles": ["implementer"], "enabled": True, "headers_set": [],
    }
    # the preset is not a stored mode: the store has the plain fields only
    assert "preset" not in json.dumps(repo.list())
    assert mcp_servers.preset_of(stored) is mcp_servers.PRESETS["seiji"]


def test_explicit_fields_override_the_preset(repo):
    stored = repo.put("seiji", seiji(
        url="https://lab.example.com/seiji/mcp", timeout_ms=600_000,
        http_base="https://lab.example.com/seiji", agent_env={"SEIJI_URL": "{http_url}"},
        rewrite_origins=["https://seiji.internal:4780/"],
        cleanup={"acquire_tool": "a", "id_field": "b", "release_tool": "c", "release_arg": "d"},
    ))
    assert stored["timeout_ms"] == 600_000
    assert stored["http_base"] == "https://lab.example.com/seiji"
    assert stored["agent_env"] == {"SEIJI_URL": "{http_url}"}
    assert stored["rewrite_origins"] == ["https://seiji.internal:4780"]
    assert stored["cleanup"] == {"acquire_tool": "a", "id_field": "b", "release_tool": "c",
                                 "release_arg": "d"}
    # the url without /mcp is the default http_base, even below a path
    assert repo.put("lab", seiji(url="https://lab.example.com/seiji/mcp"))["http_base"] == \
        "https://lab.example.com/seiji"


def test_preset_errors(repo):
    assert field_of(lambda: repo.put("x", seiji(preset="other"))) == "preset"
    assert field_of(lambda: repo.put("x", seiji(url="https://seiji.example.com/rpc"))) == "http_base"
    assert field_of(lambda: repo.put("x", seiji(transport="stdio", command="x"))) == "url"


def test_the_cli_sends_the_preset_and_only_the_given_flags():
    parser = build_parser()
    args = parser.parse_args(["admin", "mcp", "set", "seiji", "--preset", "seiji", "--url",
                              "https://seiji.example.com/mcp", "--role", "implementer",
                              "--role", "planner", "--timeout-ms", "60000"])
    assert mcp_definition(args) == {"preset": "seiji", "url": "https://seiji.example.com/mcp",
                                    "roles": ["implementer", "planner"], "enabled": True,
                                    "timeout_ms": 60000}
    args = parser.parse_args(["admin", "mcp", "set", "lab", "--transport", "http", "--url",
                              "https://lab.example.com/mcp", "--role", "implementer",
                              "--http-base", "https://lab.example.com", "--agent-env",
                              "LAB_URL={http_url}", "--rewrite-origin", "https://lab.internal",
                              "--cleanup", "open:id:close:id"])
    assert mcp_definition(args) == {
        "transport": "http", "url": "https://lab.example.com/mcp", "roles": ["implementer"],
        "enabled": True, "http_base": "https://lab.example.com", "agent_env": {"LAB_URL": "{http_url}"},
        "rewrite_origins": ["https://lab.internal"],
        "cleanup": {"acquire_tool": "open", "id_field": "id", "release_tool": "close", "release_arg": "id"},
    }


# -- field checks ------------------------------------------------------------------------
@pytest.mark.parametrize("agent_env", [
    {"SEIJI_TOKEN": SEIJI_SECRET},              # a credential-like name with a literal
    {"API_KEY": "short"},                       # a credential-like name with no placeholder
    {"SEIJI_URL": "{http_url}?k=" + "a" * 30},  # a literal that looks like a key
    {"HEADER": "Bearer {token}x"},              # a bearer literal
    {"X": "{secret}"},                          # an unknown placeholder
])
def test_agent_env_refuses_literal_secrets(repo, agent_env):
    name = next(iter(agent_env))
    assert field_of(lambda: repo.put("seiji", seiji(agent_env=agent_env))) == f"agent_env.{name}"
    assert repo.list() == []


def test_agent_env_takes_placeholders_and_harmless_literals(repo):
    stored = repo.put("seiji", seiji(agent_env={"SEIJI_TOKEN": "{token}", "SEIJI_URL": "{http_url}",
                                                "SEIJI_MCP": "{mcp_url}", "SEIJI_MODE": "shared"}))
    assert stored["agent_env"]["SEIJI_MODE"] == "shared"
    plain = {"transport": "http", "url": "https://x.example.com/mcp", "roles": []}
    assert field_of(lambda: repo.put("x", {**plain, "agent_env": {"U": "{http_url}"}})) == "agent_env.U"


def test_companion_fields_belong_to_remote_servers(repo):
    stdio = {"transport": "stdio", "command": "x", "roles": []}
    for key, value in (("http_base", "https://x.example.com"), ("agent_env", {}),
                       ("rewrite_origins", []), ("cleanup", {})):
        assert field_of(lambda k=key, v=value: repo.put("x", {**stdio, k: v})) == key
    sse = {"transport": "sse", "url": "https://x.example.com/sse", "roles": []}
    cleanup = {"acquire_tool": "a", "id_field": "b", "release_tool": "c", "release_arg": "d"}
    assert field_of(lambda: repo.put("x", {**sse, "cleanup": cleanup})) == "cleanup"
    http = {"transport": "http", "url": "https://x.example.com/mcp", "roles": []}
    assert field_of(lambda: repo.put("x", {**http, "http_base": "http://x.example.com"})) == "http_base"
    assert field_of(lambda: repo.put("x", {**http, "rewrite_origins": ["https://y"]})) == "rewrite_origins"
    assert field_of(lambda: repo.put("x", {**http, "http_base": "https://x.example.com",
                                           "rewrite_origins": ["https://y/path"]})) == "rewrite_origins"
    assert field_of(lambda: repo.put("x", {**http, "cleanup": {"acquire_tool": "a"}})) == "cleanup"


# -- staging -----------------------------------------------------------------------------
def test_the_workspace_exports_agent_env_through_the_private_env_file(
        redis_client, repo, tmp_path, monkeypatch):
    directory = tmp_path / "private" / "engine"
    monkeypatch.setattr(provider_runtime, "AGENT_DIR", str(directory))
    monkeypatch.setattr(provider_runtime, "ENV_FILE", str(directory.parent / "engine.env"))
    repo.put("seiji", seiji())
    repo.set_headers("seiji", {"Authorization": f"Bearer {SEIJI_SECRET}"})
    launch = McpLaunch(redis_client, NS, repo, GATEWAY)
    tokens: list[str] = []
    provider_runtime.prepare_provider(
        ShellSandbox(), SimpleNamespace(read_namespaced_secret=no_secret), NS,
        Config(tracker_kind="memory", engine_provider="amazon-bedrock", engine_model="m"),
        lambda write, agent_dir: tokens.append(launch.stage(write, agent_dir, "implementer", "i:1")),
    )
    env_file = directory / mcp_servers.AGENT_ENV_FILE
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    shown = subprocess.run(["bash", "-c", f". {directory.parent / 'engine.env'} && "
                            'printf "%s\\n%s" "$SEIJI_TOKEN" "$SEIJI_URL"'],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    assert shown == [tokens[0], f"{GATEWAY}/v1/mcp/seiji/http"]
    assert SEIJI_SECRET not in "".join(p.read_text() for p in directory.iterdir())


def test_no_env_file_when_no_server_has_agent_env(redis_client, repo, tmp_path, monkeypatch):
    directory = tmp_path / "private" / "engine"
    monkeypatch.setattr(provider_runtime, "AGENT_DIR", str(directory))
    monkeypatch.setattr(provider_runtime, "ENV_FILE", str(directory.parent / "engine.env"))
    repo.put("docs", {"transport": "http", "url": "https://docs.example.com/mcp", "roles": ["implementer"]})
    launch = McpLaunch(redis_client, NS, repo, GATEWAY)
    provider_runtime.prepare_provider(
        ShellSandbox(), SimpleNamespace(read_namespaced_secret=no_secret), NS,
        Config(tracker_kind="memory", engine_provider="amazon-bedrock", engine_model="m"),
        lambda write, agent_dir: launch.stage(write, agent_dir, "implementer", "i:1"),
    )
    assert not (directory / mcp_servers.AGENT_ENV_FILE).exists()
    # the env file still sources cleanly without it
    subprocess.run(["bash", "-c", f". {directory.parent / 'engine.env'}"], check=True)


def test_local_mode_gets_the_same_variables(redis_client, repo):
    repo.put("seiji", seiji(roles=["implementer", "reviewer"]))
    launch = McpLaunch(redis_client, NS, repo, "http://127.0.0.1:8090")
    assert launch.agent_env("implementer", "mcp_abc") == {
        "SEIJI_TOKEN": "mcp_abc", "SEIJI_URL": "http://127.0.0.1:8090/v1/mcp/seiji/http"}
    assert launch.agent_env("planner", "mcp_abc") == {}


# -- the prompt paragraph ----------------------------------------------------------------
def test_the_prompt_paragraph_is_only_for_roles_with_such_a_server(redis_client, repo):
    repo.put("seiji", seiji(roles=["implementer"]))
    repo.put("docs", {"transport": "http", "url": "https://docs.example.com/mcp", "roles": ["reviewer"]})
    launch = McpLaunch(redis_client, NS, repo, GATEWAY)
    text = launch.prompt_guidance("implementer")
    assert "MCP server `seiji`" in text and '"$SEIJI_URL/v1/apps"' in text
    assert "release_device" in text and "export_evidence" in text and "evidence.zip" in text
    assert "report.md" in text and "Reviewers cannot open Seiji links" in text
    assert launch.prompt_guidance("reviewer") == ""  # docs has no preset
    assert launch.prompt_guidance("planner") == ""


def test_the_attempt_prompt_carries_the_paragraph(redis_client, repo):
    repo.put("seiji", seiji())
    launch = McpLaunch(redis_client, NS, repo, GATEWAY)
    for guidance, expected in ((launch.prompt_guidance("implementer"), True), ("", False)):
        kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
        tracker.add_issue("i1", "ENG-1", state="Todo")
        _claim(kv)
        engine = FakeEngine()
        engine.run()
        env = runner_env(redis_client, kv=kv, tracker=tracker, cfg=CFG, clock=clock, engine=engine,
                         mcp_guidance=lambda g=guidance: g)
        run_attempt(env, "i1", 1, 1)
        prompt = engine.commands("prompt")[0]["message"]
        assert prompt.startswith("issue ENG-1")
        assert ("Seiji device lab" in prompt) is expected
