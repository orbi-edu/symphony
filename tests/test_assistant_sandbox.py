"""Tests for the assistant sandbox: config, tool offering, pod manifest, token scoping,
output truncation, timeout, cleanup on session end, and orphan sweep."""

from __future__ import annotations

import types

import pytest

from symphony.config import ConfigError, parse_workflow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(extra: str = "") -> object:
    return parse_workflow(
        "---\n"
        "tracker: {kind: memory}\n"
        "assistant:\n"
        "  enabled: true\n"
        f"{extra}"
        "---\nx"
    )


def _cfg_sandbox(**overrides) -> object:
    lines = "  sandbox:\n"
    for k, v in {"enabled": True, **overrides}.items():
        if isinstance(v, bool):
            lines += f"    {k}: {'true' if v else 'false'}\n"
        else:
            lines += f"    {k}: {v}\n"
    return _cfg(lines)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestConfig:
    def test_sandbox_defaults(self):
        cfg = _cfg()
        assert cfg.assistant_sandbox_enabled is False
        assert cfg.assistant_sandbox_cpu == 2
        assert cfg.assistant_sandbox_memory_mib == 4096
        assert cfg.assistant_sandbox_command_timeout_s == 300
        assert cfg.assistant_sandbox_max_lifetime_s == 14_400
        assert cfg.assistant_sandbox_max_output_chars == 20_000

    def test_sandbox_enabled(self):
        cfg = _cfg_sandbox(enabled=True)
        assert cfg.assistant_sandbox_enabled is True

    def test_sandbox_custom_values(self):
        cfg = _cfg_sandbox(cpu=4, memory_mib=8192, command_timeout_s=600,
                           max_lifetime_s=3600, max_output_chars=50000)
        assert cfg.assistant_sandbox_cpu == 4
        assert cfg.assistant_sandbox_memory_mib == 8192
        assert cfg.assistant_sandbox_command_timeout_s == 600
        assert cfg.assistant_sandbox_max_lifetime_s == 3600
        assert cfg.assistant_sandbox_max_output_chars == 50000

    @pytest.mark.parametrize("key, value, message", [
        ("cpu", 0, "cpu must be positive"),
        ("memory_mib", -1, "memory_mib must be positive"),
        ("command_timeout_s", 0, "command_timeout_s must be positive"),
        ("max_lifetime_s", 0, "max_lifetime_s must be positive"),
        ("max_output_chars", 0, "max_output_chars must be positive"),
    ])
    def test_sandbox_invalid_values(self, key, value, message):
        with pytest.raises(ConfigError, match=message):
            _cfg_sandbox(**{key: value})

    def test_sandbox_unknown_key(self):
        with pytest.raises(ConfigError, match="unknown keys"):
            _cfg("  sandbox:\n    bogus: true\n")


# ---------------------------------------------------------------------------
# Sandbox lifecycle against a fake cluster
# ---------------------------------------------------------------------------

REPO = "acme/app"
TOKEN = "ghs_" + "x" * 36


class FakeCluster:
    """Records created/deleted pods and exec calls; pods become Running on create."""

    def __init__(self, clone_exit: int = 0):
        self.pods: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.execs: list[tuple[str, str, float | None, str | None]] = []
        self.clone_exit = clone_exit

    # CoreV1Api subset
    def create_namespaced_pod(self, ns, manifest):
        self.pods[manifest["metadata"]["name"]] = manifest

    def list_namespaced_pod(self, ns, label_selector=""):
        want = dict(kv.split("=") for kv in label_selector.split(","))
        items = [types.SimpleNamespace(metadata=types.SimpleNamespace(name=n))
                 for n, m in self.pods.items()
                 if all(m["metadata"]["labels"].get(k) == v for k, v in want.items())]
        return types.SimpleNamespace(items=items)


@pytest.fixture
def cluster(monkeypatch):
    from symphony import kube

    c = FakeCluster()

    def get_workspace(core, ns, name):
        return (types.SimpleNamespace(status=types.SimpleNamespace(phase="Running"))
                if name in core.pods else None)

    def delete_pod(core, ns, name, wait=False, timeout_s=120):
        core.pods.pop(name, None)
        core.deleted.append(name)

    class PodSandbox:
        def __init__(self, core, ns, name, env=None):
            self.core, self.name = core, name

        def exec_script(self, script, timeout_s=None, *, stdin=None):
            self.core.execs.append((self.name, script, timeout_s, stdin))
            if "git" in script and "clone" in script:
                return self.core.clone_exit, "fatal: repository not found" if self.core.clone_exit else ""
            return 0, "ok\n"

    monkeypatch.setattr(kube, "get_workspace", get_workspace)
    monkeypatch.setattr(kube, "delete_pod", delete_pod)
    monkeypatch.setattr(kube, "wait_workspace_ready", lambda core, ns, name, timeout_s=0: None)
    monkeypatch.setattr(kube, "PodSandbox", PodSandbox)
    return c


def _sandbox(cluster, cfg=None, branch=None, thread="slack:C1:111.2"):
    from symphony.assistant.sandbox import AssistantSandbox

    cfg = cfg or _cfg_sandbox(cpu=1, memory_mib=2048, command_timeout_s=60)
    return AssistantSandbox(cluster, "ns", lambda: cfg, thread_key=thread,
                            token_for=lambda repo: TOKEN, base_branch_for=lambda repo: branch)


class TestLifecycle:
    def test_manifest_is_an_isolated_workspace_pod(self, cluster):
        sb = _sandbox(cluster)
        sb.run(REPO, "true")
        [manifest] = cluster.pods.values()
        spec, container = manifest["spec"], manifest["spec"]["containers"][0]
        # The workspace NetworkPolicy selects these labels; no SA token reaches the agent.
        assert manifest["metadata"]["labels"]["component"] == "workspace"
        assert manifest["metadata"]["labels"]["role"] == "assistant-sandbox"
        assert spec["automountServiceAccountToken"] is False
        assert container["securityContext"]["privileged"] is False
        assert container["resources"]["limits"] == {"cpu": "1", "memory": "2048Mi"}
        assert all("persistentVolumeClaim" not in v for v in spec["volumes"])
        assert "env" not in container or all("TOKEN" not in e["name"] for e in container["env"])

    def test_image_follows_the_repository_runtime(self, cluster):
        cfg = parse_workflow(
            "---\ntracker: {kind: memory}\nkubernetes:\n  repo_runtimes: {acme/app: polyglot}\n"
            "  agent_images: {polyglot: registry.example.com/agent:polyglot}\n"
            "assistant:\n  enabled: true\n  sandbox: {enabled: true}\n---\nx")
        _sandbox(cluster, cfg).run(REPO, "true")
        [manifest] = cluster.pods.values()
        assert manifest["spec"]["containers"][0]["image"] == "registry.example.com/agent:polyglot"

    def test_token_reaches_the_pod_only_on_stdin(self, cluster):
        _sandbox(cluster, branch="develop").run(REPO, "true")
        clone = next(e for e in cluster.execs if "clone" in e[1])
        _, script, _, stdin = clone
        assert stdin == TOKEN + "\n"
        assert TOKEN not in script  # never in the script text
        # …nor substituted into git's argv at run time: no credentials in the clone URL.
        assert "x-access-token:" not in script and "@github.com" not in script
        assert "credential.helper" in script
        assert "--branch develop" in script
        assert "https://github.com/acme/app.git" in script  # origin stays the plain URL

    def test_each_repository_is_cloned_once(self, cluster):
        sb = _sandbox(cluster)
        sb.run(REPO, "true")
        sb.run(REPO, "ls")
        sb.run("acme/lib", "ls")
        clones = [e[1] for e in cluster.execs if "clone" in e[1]]
        assert len(clones) == 2 and "acme/lib.git" in clones[1]
        assert len(cluster.pods) == 1

    def test_a_failed_clone_is_reported_and_retried(self, cluster):
        from symphony.assistant.sandbox import SandboxError

        cluster.clone_exit = 128
        sb = _sandbox(cluster)
        with pytest.raises(SandboxError, match="exit 128"):
            sb.run(REPO, "true")
        cluster.clone_exit = 0
        assert sb.run(REPO, "true")[0] == 0  # not marked cloned after the failure

    def test_command_runs_in_the_checkout_with_a_capped_timeout(self, cluster):
        sb = _sandbox(cluster)
        sb.run(REPO, "pytest -q", timeout_s=10_000)
        name, script, timeout, _ = cluster.execs[-1]
        assert script == "cd /workspace/acme/app && pytest -q"
        assert timeout == 60.0  # command_timeout_s

    def test_close_deletes_the_pod_and_a_new_run_recreates_it(self, cluster):
        sb = _sandbox(cluster)
        sb.run(REPO, "true")
        name = sb.pod_name
        sb.close()
        assert cluster.deleted == [name] and not cluster.pods
        sb.run(REPO, "true")
        assert name in cluster.pods
        assert sum("clone" in e[1] for e in cluster.execs) == 2

    def test_sweep_deletes_every_sandbox_pod_but_not_workspaces(self, cluster):
        from symphony.assistant.sandbox import sweep_orphans

        _sandbox(cluster, thread="slack:C1:1").run(REPO, "true")
        _sandbox(cluster, thread="slack:C1:2").run(REPO, "true")
        cluster.pods["ws-eng-1"] = {"metadata": {"name": "ws-eng-1",
                                                 "labels": {"app": "symphony", "component": "workspace"}}}
        assert sweep_orphans(cluster, "ns", "symphony") == 2
        assert list(cluster.pods) == ["ws-eng-1"]


class TestPodName:
    def test_dns_safe_deterministic_and_distinct(self):
        from symphony.assistant.sandbox import pod_name

        a = pod_name("symphony", "slack:C0123:1700000000.123456")
        assert a == pod_name("symphony", "slack:C0123:1700000000.123456")
        assert a != pod_name("symphony", "slack:C0999:1700000000.123456")
        assert len(a) <= 63 and a.replace("-", "").isalnum() and a == a.lower()


class TestTool:
    def _tool(self, cluster, login="alice"):
        from symphony.assistant.sandbox import sandbox_run_tool

        sb = _sandbox(cluster)
        who = {"login": login}
        return sandbox_run_tool(lambda: sb, lambda: ["Acme/App"], lambda: who["login"]), who

    def test_unmapped_asker_is_refused_at_call_time(self, cluster):
        tool, who = self._tool(cluster)
        assert tool.check({"repo": REPO, "command": "ls"}) is None
        who["login"] = None  # another person speaks in the same thread
        assert "notify.users" in tool.check({"repo": REPO, "command": "ls"})

    def test_only_the_assistant_repositories(self, cluster):
        tool, _ = self._tool(cluster)
        assert "repo must be one of" in tool.check({"repo": "evil/repo", "command": "ls"})

    def test_answer_reports_exit_code_and_errors(self, cluster):
        import json

        tool, _ = self._tool(cluster)
        assert json.loads(tool.answer({"repo": "acme/app", "command": "ls"}, 0)) == {
            "exit_code": 0, "output": "ok\n"}
        cluster.clone_exit = 1
        tool2, _ = self._tool(cluster)
        assert json.loads(tool2.answer({"repo": REPO, "command": "ls"}, 0))["error"] == "sandbox_failed"


class TestTruncation:
    def test_head_and_tail_are_kept(self):
        from symphony.assistant.sandbox import TRUNCATION_MARKER, truncate_output

        out = truncate_output("a" * 500 + "b" * 500, 200)
        assert len(out) <= 200 and out.startswith("a") and out.endswith("b")
        assert TRUNCATION_MARKER in out
        assert truncate_output("short", 200) == "short"


class TestInstructions:
    def test_sandbox_instructions_only_with_the_tool(self):
        from symphony.assistant.engine import SANDBOX_INSTRUCTIONS, instructions

        kw = dict(transport="slack", is_dm=False, repos=[REPO])
        assert SANDBOX_INSTRUCTIONS in instructions("assistant", has_sandbox=True, **kw)
        assert SANDBOX_INSTRUCTIONS not in instructions("assistant", has_sandbox=False, **kw)

    def test_planner_always_writes_a_spec(self):
        from symphony.assistant import engine

        kw = dict(transport="slack", is_dm=False, repos=[REPO])
        for has_sandbox in (True, False):
            text = engine.instructions("planner", has_sandbox=has_sandbox, **kw)
            for section in (engine.SPEC_SECTION_PROBLEM, engine.SPEC_SECTION_ACCEPTANCE_CRITERIA,
                            engine.SPEC_SECTION_VERIFICATION_PLAN, engine.SPEC_SECTION_RISKS):
                assert section in text
        assert engine.PLANNER_SPEC_INSTRUCTIONS not in engine.instructions("assistant", **kw)
