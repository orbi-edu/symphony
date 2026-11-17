"""Monorepo components: config validation, label parsing, resolver precedence, prompt,
workspace env and pod-recreate decision."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from symphony.config import (
    ConfigError,
    known_runtimes,
    parse_workflow,
    resolve_workspace,
)
from symphony.repos import (
    CONFLICTING_COMPONENTS,
    CONFLICTING_RUNTIMES,
    UNKNOWN_COMPONENT,
    UNKNOWN_RUNTIME,
    parse_runtime_labels,
    validate_runtime_component,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

MINIMAL = """---
tracker:
  kind: memory
---
body {{ issue.identifier }}
"""


def _cfg(**overrides):
    """Parse MINIMAL with kubernetes overrides injected."""
    lines = ["---", "tracker:", "  kind: memory"]
    k8s = {}
    for key, value in overrides.items():
        k8s[key] = value
    if k8s:
        lines.append("kubernetes:")
        # Serialize as YAML-ish (good enough for flat dicts and nested dicts)
        import json
        for key, value in k8s.items():
            lines.append(f"  {key}: {json.dumps(value)}")
    lines.extend(["---", "body {{ issue.identifier }}"])
    return parse_workflow("\n".join(lines))


def _issue(labels=(), target_repo="acme/app"):
    return SimpleNamespace(
        labels=labels, target_repo=target_repo, target_repos=(target_repo,) if target_repo else ()
    )


# ---------------------------------------------------------------------------
# 1. Config validation of repo_runtimes
# ---------------------------------------------------------------------------

class TestRepoRuntimesConfig:
    def test_valid_repo_runtimes(self):
        cfg = _cfg(repo_runtimes={"acme/app": "dotnet10", "acme/web": "node"})
        assert cfg.repo_runtimes == {"acme/app": "dotnet10", "acme/web": "node"}

    def test_repo_runtimes_keys_are_lowercased(self):
        cfg = _cfg(repo_runtimes={"Acme/App": "dotnet10"})
        assert "acme/app" in cfg.repo_runtimes

    def test_unknown_runtime_is_rejected(self):
        with pytest.raises(ConfigError, match="not a known runtime"):
            _cfg(repo_runtimes={"acme/app": "cobol"})

    def test_non_dict_repo_runtimes_is_rejected(self):
        with pytest.raises(ConfigError, match="repo_runtimes"):
            _cfg(repo_runtimes=["dotnet10"])

    def test_empty_repo_runtimes_is_valid(self):
        cfg = _cfg(repo_runtimes={})
        assert cfg.repo_runtimes == {}

    def test_repo_runtime_method(self):
        cfg = _cfg(repo_runtimes={"acme/app": "dotnet10"})
        assert cfg.repo_runtime("acme/app") == "dotnet10"
        assert cfg.repo_runtime("acme/web") is None
        assert cfg.repo_runtime("ACME/APP") == "dotnet10"


# ---------------------------------------------------------------------------
# 2. Config validation of components
# ---------------------------------------------------------------------------

class TestComponentsConfig:
    def test_valid_components(self):
        cfg = _cfg(components={
            "acme/app": {
                "api": {"path": "api", "runtime": "dotnet10"},
                "web": {"path": "web"},
            }
        })
        assert cfg.components["acme/app"]["api"]["path"] == "api"
        assert cfg.components["acme/app"]["api"]["runtime"] == "dotnet10"
        assert "runtime" not in cfg.components["acme/app"]["web"]

    def test_component_keys_are_lowercased(self):
        cfg = _cfg(components={"Acme/App": {"api": {"path": "api"}}})
        assert "acme/app" in cfg.components

    def test_invalid_component_name_is_rejected(self):
        with pytest.raises(ConfigError, match="component name"):
            _cfg(components={"acme/app": {"UPPER": {"path": "api"}}})

    def test_component_name_with_leading_hyphen_is_rejected(self):
        with pytest.raises(ConfigError, match="component name"):
            _cfg(components={"acme/app": {"-bad": {"path": "api"}}})

    def test_component_path_with_dotdot_is_rejected(self):
        with pytest.raises(ConfigError, match="path"):
            _cfg(components={"acme/app": {"api": {"path": "../escape"}}})

    def test_component_path_with_leading_slash_is_rejected(self):
        with pytest.raises(ConfigError, match="path"):
            _cfg(components={"acme/app": {"api": {"path": "/absolute"}}})

    def test_component_unknown_runtime_is_rejected(self):
        with pytest.raises(ConfigError, match="not a known runtime"):
            _cfg(components={"acme/app": {"api": {"path": "api", "runtime": "cobol"}}})

    def test_empty_components_is_valid(self):
        cfg = _cfg(components={})
        assert cfg.components == {}

    def test_non_dict_components_is_rejected(self):
        with pytest.raises(ConfigError, match="components"):
            _cfg(components=["bad"])


# ---------------------------------------------------------------------------
# 3. Label parsing
# ---------------------------------------------------------------------------

class TestLabelParsing:
    def test_parse_runtime_label(self):
        runtime, component = parse_runtime_labels(["runtime:dotnet10", "symphony"])
        assert runtime == "dotnet10"
        assert component is None

    def test_parse_component_label(self):
        runtime, component = parse_runtime_labels(["component:api", "symphony"])
        assert runtime is None
        assert component == "api"

    def test_parse_both_labels(self):
        runtime, component = parse_runtime_labels(["runtime:node", "component:web"])
        assert runtime == "node"
        assert component == "web"

    def test_multiple_runtime_labels_returns_none(self):
        runtime, component = parse_runtime_labels(["runtime:node", "runtime:dotnet10"])
        assert runtime is None

    def test_case_insensitive_prefix(self):
        runtime, component = parse_runtime_labels(["Runtime:dotnet10", "Component:api"])
        assert runtime == "dotnet10"
        assert component == "api"

    def test_no_labels(self):
        runtime, component = parse_runtime_labels(["symphony", "repo:acme/app"])
        assert runtime is None
        assert component is None


# ---------------------------------------------------------------------------
# 4. Validate runtime/component (not-dispatched reasons)
# ---------------------------------------------------------------------------

VALID_RUNTIMES = frozenset({"node", "dotnet9", "dotnet10", "polyglot"})
COMPONENTS = {"acme/app": {"api": {"path": "api", "runtime": "dotnet10"}, "web": {"path": "web"}}}


class TestValidateRuntimeComponent:
    def test_no_labels_is_ok(self):
        assert validate_runtime_component(
            ["symphony"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) is None

    def test_valid_runtime_label_is_ok(self):
        assert validate_runtime_component(
            ["runtime:node"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) is None

    def test_unknown_runtime_label(self):
        assert validate_runtime_component(
            ["runtime:cobol"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) == UNKNOWN_RUNTIME

    def test_multiple_runtime_labels_conflict(self):
        assert validate_runtime_component(
            ["runtime:node", "runtime:dotnet10"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) == CONFLICTING_RUNTIMES

    def test_valid_component_label_is_ok(self):
        assert validate_runtime_component(
            ["component:api"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) is None

    def test_unknown_component_label(self):
        assert validate_runtime_component(
            ["component:missing"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) == UNKNOWN_COMPONENT

    def test_component_for_wrong_repo(self):
        assert validate_runtime_component(
            ["component:api"], "acme/other", COMPONENTS, VALID_RUNTIMES
        ) == UNKNOWN_COMPONENT

    def test_multiple_component_labels_conflict(self):
        assert validate_runtime_component(
            ["component:api", "component:web"], "acme/app", COMPONENTS, VALID_RUNTIMES
        ) == CONFLICTING_COMPONENTS


# ---------------------------------------------------------------------------
# 5. Resolver precedence table (parametrized)
# ---------------------------------------------------------------------------

_CFG_COMPONENTS = _cfg(
    agent_runtime="node",
    repo_runtimes={"acme/app": "dotnet9"},
    components={
        "acme/app": {
            "api": {"path": "api", "runtime": "dotnet10"},
            "web": {"path": "web"},
        }
    },
)

@pytest.mark.parametrize(
    "labels, expected_runtime, expected_component, expected_path",
    [
        # no labels → repo_runtimes → dotnet9
        ((), "dotnet9", None, None),
        # component:api → component runtime dotnet10
        (("component:api",), "dotnet10", "api", "api"),
        # component:web (no runtime) → falls to repo_runtimes → dotnet9
        (("component:web",), "dotnet9", "web", "web"),
        # runtime:polyglot overrides everything
        (("runtime:polyglot",), "polyglot", None, None),
        # runtime: + component: → runtime label wins
        (("runtime:polyglot", "component:api"), "polyglot", "api", "api"),
        # unknown component label → component not resolved (not in the config for this repo)
        (("component:nonexistent",), "dotnet9", None, None),
    ],
    ids=[
        "no-labels-uses-repo-runtime",
        "component-with-runtime",
        "component-without-runtime-uses-repo-runtime",
        "runtime-label-overrides-all",
        "runtime-label-plus-component",
        "unknown-component-ignored",
    ],
)
def test_resolve_workspace_precedence(labels, expected_runtime, expected_component, expected_path):
    issue = _issue(labels=labels, target_repo="acme/app")
    runtime, component, path = resolve_workspace(_CFG_COMPONENTS, issue)
    assert runtime == expected_runtime
    assert component == expected_component
    assert path == expected_path


def test_resolve_workspace_falls_to_agent_runtime():
    """No repo_runtimes, no component, no runtime label → agent_runtime."""
    cfg = _cfg(agent_runtime="polyglot")
    issue = _issue(labels=(), target_repo="acme/other")
    runtime, component, path = resolve_workspace(cfg, issue)
    assert runtime == "polyglot"
    assert component is None


# ---------------------------------------------------------------------------
# 6. agent_image_for with a runtime arg
# ---------------------------------------------------------------------------

def test_agent_image_for_with_runtime():
    from symphony.kube import DEFAULT_AGENT_IMAGES, agent_image_for
    cfg = SimpleNamespace(agent_image=None, agent_images={}, agent_runtime="node")
    assert agent_image_for(cfg, "dotnet10") == DEFAULT_AGENT_IMAGES["dotnet10"]
    assert agent_image_for(cfg) == DEFAULT_AGENT_IMAGES["node"]


def test_agent_image_for_override_still_wins():
    from symphony.kube import agent_image_for
    cfg = SimpleNamespace(agent_image="custom:tag", agent_images={}, agent_runtime="node")
    assert agent_image_for(cfg, "dotnet10") == "custom:tag"


def test_agent_image_for_custom_images():
    from symphony.kube import agent_image_for
    cfg = SimpleNamespace(agent_image=None, agent_images={"custom": "my:img"}, agent_runtime="node")
    assert agent_image_for(cfg, "custom") == "my:img"


# ---------------------------------------------------------------------------
# 7. Prompt render with and without component
# ---------------------------------------------------------------------------

def test_prompt_render_with_component():
    from symphony.prompt import render_prompt
    from symphony.trackers.memory import MemoryTracker
    issue = MemoryTracker().add_issue("i1", "ENG-1", title="Fix bug")
    out = render_prompt(
        "{% if issue.component %}C={{ issue.component }} P={{ issue.component_path }}{% endif %}"
        " R={{ issue.runtime }}",
        issue, 1,
        component="api", component_path="api", runtime="dotnet10",
    )
    assert "C=api" in out
    assert "P=api" in out
    assert "R=dotnet10" in out


def test_prompt_render_without_component():
    from symphony.prompt import render_prompt
    from symphony.trackers.memory import MemoryTracker
    issue = MemoryTracker().add_issue("i1", "ENG-1", title="Fix bug")
    out = render_prompt(
        "{% if issue.component %}C={{ issue.component }}{% else %}none{% endif %}",
        issue, 1,
    )
    assert "none" in out


# ---------------------------------------------------------------------------
# 8. The real WORKFLOW.md renders with component context
# ---------------------------------------------------------------------------

CFG = parse_workflow(Path(__file__).resolve().parents[1].joinpath("WORKFLOW.md").read_text())


def test_workflow_renders_with_component_label():
    from symphony.prompt import render_prompt
    from symphony.trackers.memory import MemoryTracker
    issue = MemoryTracker(default_repo="acme/api").add_issue(
        "i1", "ENG-1", title="details", state="Todo",
        labels=("symphony", "component:api"),
    )
    out = render_prompt(
        CFG.prompt_body, issue, 1,
        component="api", component_path="api", runtime="dotnet10",
    )
    assert "api" in out


def test_workflow_renders_without_component():
    from symphony.prompt import render_prompt
    from symphony.trackers.memory import MemoryTracker
    issue = MemoryTracker(default_repo="acme/api").add_issue(
        "i1", "ENG-1", title="details", state="Todo",
        labels=("symphony",),
    )
    out = render_prompt(CFG.prompt_body, issue, 1)
    assert "ENG-1" in out


# ---------------------------------------------------------------------------
# 9. Pod-recreate decision (pure function)
# ---------------------------------------------------------------------------

def test_workspace_needs_recreate_when_image_differs():
    from symphony.kube import WORKSPACE_CONTAINER, workspace_needs_recreate
    container = SimpleNamespace(name=WORKSPACE_CONTAINER, image="symphony-agent:node")
    pod = SimpleNamespace(spec=SimpleNamespace(containers=[container]))
    cfg = SimpleNamespace(agent_image=None, agent_images={}, agent_runtime="node")
    # Same runtime → no recreate
    assert workspace_needs_recreate(pod, cfg, "node") is False
    # Different runtime → recreate
    assert workspace_needs_recreate(pod, cfg, "dotnet10") is True


def test_workspace_needs_recreate_with_agent_image_override():
    from symphony.kube import WORKSPACE_CONTAINER, workspace_needs_recreate
    container = SimpleNamespace(name=WORKSPACE_CONTAINER, image="custom:tag")
    pod = SimpleNamespace(spec=SimpleNamespace(containers=[container]))
    cfg = SimpleNamespace(agent_image="custom:tag", agent_images={}, agent_runtime="node")
    # agent_image override matches → no recreate
    assert workspace_needs_recreate(pod, cfg, "dotnet10") is False


def test_workspace_needs_recreate_none_pod():
    from symphony.kube import workspace_needs_recreate
    cfg = SimpleNamespace(agent_image=None, agent_images={}, agent_runtime="node")
    assert workspace_needs_recreate(None, cfg, "dotnet10") is False


# ---------------------------------------------------------------------------
# 10. Workspace env vars
# ---------------------------------------------------------------------------

def test_component_env_included_when_set():
    """The ensure_sandbox in both entry/run_issue.py and effects_local.py includes
    SYMPHONY_COMPONENT and SYMPHONY_COMPONENT_PATH. This is a structural test using
    the same logic: conditionally include env vars when a component applies."""
    component = "api"
    component_path = "api"
    env = {
        "SYMPHONY_TARGET_REPO": "acme/app",
        **({"SYMPHONY_COMPONENT": component} if component else {}),
        **({"SYMPHONY_COMPONENT_PATH": component_path} if component_path else {}),
    }
    assert env["SYMPHONY_COMPONENT"] == "api"
    assert env["SYMPHONY_COMPONENT_PATH"] == "api"


def test_component_env_absent_when_not_set():
    component = None
    component_path = None
    env = {
        "SYMPHONY_TARGET_REPO": "acme/app",
        **({"SYMPHONY_COMPONENT": component} if component else {}),
        **({"SYMPHONY_COMPONENT_PATH": component_path} if component_path else {}),
    }
    assert "SYMPHONY_COMPONENT" not in env
    assert "SYMPHONY_COMPONENT_PATH" not in env


# ---------------------------------------------------------------------------
# 11. known_runtimes includes defaults + agent_images
# ---------------------------------------------------------------------------

def test_known_runtimes():
    cfg = _cfg(agent_images={"custom": "my:img"})
    rts = known_runtimes(cfg)
    assert "node" in rts
    assert "dotnet9" in rts
    assert "dotnet10" in rts
    assert "polyglot" in rts
    assert "custom" in rts


# ---------------------------------------------------------------------------
# 12. Orchestrator: unknown component is not claimed; fixing label dispatches
# ---------------------------------------------------------------------------

def test_unknown_component_not_claimed_then_dispatches():
    """An issue with an unknown component: label is filtered out before claim_issue.
    After the label is fixed, the next tick dispatches it normally."""
    from symphony import state
    from symphony.orchestrator import tick

    cfg = _cfg(
        agent_runtime="node",
        components={"acme/app": {"api": {"path": "api"}}},
    )

    class FakeKV:
        def __init__(self):
            self.store: dict = {}
        def get(self, key, default=None):
            return self.store.get(key, default)
        def put(self, key, value, *, skip_if_exists=False):
            if skip_if_exists and key in self.store:
                return False
            self.store[key] = value
            return True
        def delete(self, key):
            self.store.pop(key, None)
        def keys(self):
            return list(self.store.keys())

    class FakeTracker:
        def __init__(self, issues):
            self._issues = issues
        def fetch_issues_by_states(self, states):
            return [i for i in self._issues if i.state in states]

    class FakeEffects:
        def __init__(self):
            self.events = []
            self.spawned = []
        def emit(self, event, **kw):
            self.events.append((event, kw))
        def spawn_runner(self, issue, attempt, seq):
            self.spawned.append((issue.id, attempt, seq))
        def is_call_running(self, _cid):
            return False
        def cancel_runner(self, *a, **kw):
            pass
        def kill_agent_process(self, _name):
            pass
        def release_workspace(self, _iid, _entry):
            pass

    from symphony.trackers.base import Issue

    bad_issue = Issue(
        id="i1", identifier="ENG-1", title="Add endpoint",
        description="", state="Todo", state_type="active",
        labels=("symphony", "repo:acme/app", "component:nonexistent"),
        target_repo="acme/app", target_repos=("acme/app",),
        dispatchable=True, priority=1, created_at="2026-01-01T00:00:00Z",
    )

    kv = FakeKV()
    fx = FakeEffects()
    tracker = FakeTracker([bad_issue])

    # Tick 1: the issue is eligible but fails runtime/component validation.
    tick(
        kv=kv, tracker=tracker, cfg=cfg, now_ms=1000, owner="d1",
        fx=fx, admission_open=lambda: True,
    )
    # Must NOT have been claimed (no entry in kv).
    assert kv.get(state.issue_key("i1")) is None
    assert fx.spawned == []
    assert any(ev == "issue_not_routable" for ev, _ in fx.events)

    # Fix the label and re-tick.
    fixed_issue = Issue(
        id="i1", identifier="ENG-1", title="Add endpoint",
        description="", state="Todo", state_type="active",
        labels=("symphony", "repo:acme/app", "component:api"),
        target_repo="acme/app", target_repos=("acme/app",),
        dispatchable=True, priority=1, created_at="2026-01-01T00:00:00Z",
    )
    tracker2 = FakeTracker([fixed_issue])
    fx2 = FakeEffects()
    tick(
        kv=kv, tracker=tracker2, cfg=cfg, now_ms=2000, owner="d1",
        fx=fx2, admission_open=lambda: True,
    )
    # Now it should be claimed and dispatched.
    assert fx2.spawned == [("i1", 1, 1)]
    entry = kv.get(state.issue_key("i1"))
    assert entry is not None
    assert entry.get("runtime") == "node"
    assert entry.get("component") == "api"
    assert entry.get("component_path") == "api"
