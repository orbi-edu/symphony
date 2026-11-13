"""FR-019 + WORKFLOW.md contract: defaults, $VAR, unknown keys, last-known-good."""

import pytest

from symphony.config import (
    ConfigError,
    ConfigHolder,
    parse_workflow,
    resolve_secret,
)

MINIMAL = """---
tracker:
  kind: memory
---
body {{ issue.identifier }}
"""

FULL = """---
tracker:
  kind: linear
  provider: {project_slug: "proj", api_key: $LINEAR_API_KEY}
  active_states: [Todo, Rework]
  terminal_states: [Done, Canceled]
  required_labels: [symphony]
polling: {interval_ms: 60000}
agent: {max_concurrent_agents: 3, max_turns: 7, max_retry_backoff_ms: 120000, stall_timeout_ms: 50}
engine: {provider: acme, model: acme-1, turn_timeout_ms: 100, read_timeout_ms: 5}
kubernetes:
  redispatch_cooldown_s: 10
  learning: {enabled: true, reflect_on: [Done]}
chave_desconhecida: {qualquer: coisa}
---
prompt {{ attempt }}
"""


def test_defaults():
    cfg = parse_workflow(MINIMAL)
    assert cfg.polling_interval_ms == 15_000  # safety poll; webhooks wake the daemon
    assert cfg.max_concurrent_agents == 5
    assert cfg.max_turns == 20
    assert cfg.max_retry_backoff_ms == 300_000
    assert cfg.turn_timeout_ms == 3_600_000
    assert cfg.read_timeout_ms == 5_000
    assert cfg.stall_timeout_ms == 300_000
    assert cfg.hooks_timeout_ms == 60_000
    assert "Cancelled" in cfg.terminal_states and "Canceled" in cfg.terminal_states
    assert cfg.workspace_root == "/workspace"
    assert cfg.prompt_body.startswith("body")


def test_unknown_keys_are_ignored_and_known_keys_applied():
    cfg = parse_workflow(FULL)
    assert cfg.active_states == ("Todo", "Rework")
    assert cfg.max_concurrent_agents == 3
    assert cfg.redispatch_cooldown_s == 10


def test_reflect_on_defaults_to_full_terminal_states():
    cfg = parse_workflow(MINIMAL)
    assert cfg.effective_reflect_on == cfg.terminal_states
    cfg2 = parse_workflow(FULL)
    assert cfg2.effective_reflect_on == ("Done",)


def test_secret_indirection():
    assert resolve_secret("$LINEAR_API_KEY", {"LINEAR_API_KEY": "k"}) == "k"
    assert resolve_secret("literal", {}) == "literal"
    with pytest.raises(ConfigError):
        resolve_secret("$MISSING", {})


@pytest.mark.parametrize(
    "bad",
    [
        "no front matter",
        "---\ntracker: {kind: unknown}\n---\nx",
        "---\ntracker: {}\n---\nx",
        "---\ntracker: {kind: memory}\npolling: {interval_ms: thirty}\n---\nx",
        "---\ntracker: {kind: memory}\n---\n{{ issue.x ",  # invalid Liquid
    ],
)
def test_invalid_configs_raise(bad):
    with pytest.raises((ConfigError, Exception)):
        parse_workflow(bad)


def test_holder_keeps_last_known_good():
    holder = ConfigHolder()
    assert holder.update(FULL, {"LINEAR_API_KEY": "host-key"})
    good = holder.config
    assert not holder.update("---\ntracker: {kind: memory}\nagent: {max_turns: 0}\n---\nx")
    assert holder.config is good  # an invalid version is never applied
    assert holder.last_error


def test_holder_applies_policy_whose_tracker_credential_is_not_provisioned_yet():
    """A missing credential is setup in progress, not invalid policy: the policy applies
    (so the daemon idles reporting setup, rather than crash-looping) and the tracker
    reports what is missing (`edge.TrackerNotConfigured`, tests/test_setup_idle.py)."""
    holder = ConfigHolder()
    assert holder.update(FULL, {})
    assert holder.config.tracker_kind == "linear"
    assert holder.rejected_document_hash is None


def test_bool_not_accepted_as_int():
    text = "---\ntracker: {kind: memory}\npolling: {interval_ms: true}\n---\nx"
    with pytest.raises(ConfigError):
        parse_workflow(text)


def test_engine_model_is_optional_but_never_half_set():
    """The published TeamConfig names each role's model (internals §4), so a WORKFLOW
    without `engine.provider/model` is runnable; half a reference is a typo, not a choice."""
    parse_workflow(MINIMAL)  # no engine model: parses
    for half in ("provider: acme", "model: acme-1"):
        with pytest.raises(ConfigError, match="together"):
            parse_workflow(f"---\ntracker: {{kind: memory}}\nengine: {{{half}}}\n---\nx")
    with pytest.raises(ConfigError, match="thinking"):
        parse_workflow("---\ntracker: {kind: memory}\nengine: {thinking: loud}\n---\nx")


@pytest.mark.parametrize("key", ["approval_policy", "thread_sandbox", "turn_sandbox_policy"])
def test_retired_engine_keys_are_rejected_loudly(key):
    """T006 — a retired key must FAIL, never be silently ignored. `thread_sandbox` in
    particular would tell an operator the agent is sandboxed when it is not."""
    with pytest.raises(ConfigError) as exc:
        parse_workflow(f"---\ntracker: {{kind: memory}}\nengine: {{{key}: whatever}}\n---\nx")
    assert key in str(exc.value)


def test_codex_block_itself_is_rejected():
    """T006 — the whole codex: block is retired in favour of engine:."""
    with pytest.raises(ConfigError) as exc:
        parse_workflow("---\ntracker: {kind: memory}\ncodex: {command: codex app-server}\n---\nx")
    assert "codex" in str(exc.value)


def test_stall_timeout_read_from_agent_block():
    """T007 — stall_timeout_ms is a daemon concern and was misfiled under codex: in 001."""
    cfg = parse_workflow("---\ntracker: {kind: memory}\nagent: {stall_timeout_ms: 42}\n---\nx")
    assert cfg.stall_timeout_ms == 42
    assert parse_workflow(MINIMAL).stall_timeout_ms == 300_000


def test_by_state_overrides_normalized_and_invalid_dropped():
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nagent:\n  max_concurrent_agents_by_state:\n"
        "    ' Rework ': 2\n    Todo: 0\n    Merging: true\n---\nx"
    )
    assert cfg.max_concurrent_agents_by_state == {"rework": 2}  # trimmed+casefolded; 0/bool dropped


def test_nonstring_hook_and_nonpositive_limits_raise():
    with pytest.raises(ConfigError):
        parse_workflow("---\ntracker: {kind: memory}\nhooks: {after_create: [1, 2]}\n---\nx")
    with pytest.raises(ConfigError):
        parse_workflow("---\ntracker: {kind: memory}\nagent: {max_turns: 0}\n---\nx")
    with pytest.raises(ConfigError):
        parse_workflow("---\ntracker: {kind: memory}\nhooks: {timeout_ms: -1}\n---\nx")


def test_holder_survives_arbitrary_parse_crash():
    holder = ConfigHolder()
    assert holder.update(FULL, {"LINEAR_API_KEY": "host-key"})
    good = holder.config
    assert not holder.update("---\nworkspace: notamap\ntracker: {kind: memory}\n---\nx")
    assert holder.config is good  # §6.2 backstop: ANY crash keeps last-known-good


def test_agent_runtime_parsed_with_node_default():
    assert parse_workflow(MINIMAL).agent_runtime == "node"
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nkubernetes: {agent_runtime: dotnet10}\n---\nx"
    )
    assert cfg.agent_runtime == "dotnet10"


def test_leftover_modal_block_fails_loudly():
    """Migration guard: silently ignoring a `modal:` block would drop the sandbox sizing
    and learning policy of a migrated deployment — config theatre, same rule as codex:."""
    with pytest.raises(ConfigError, match="kubernetes"):
        parse_workflow("---\ntracker: {kind: memory}\nmodal: {environment: dev}\n---\nx")


# -- review pipeline states, notifications, human input ------------------------


def test_pipeline_defaults():
    cfg = parse_workflow(MINIMAL)
    assert cfg.tracker_states == {
        "review": "In Review", "rework": "Rework", "merging": "Merging", "done": "Done",
    }
    # Merging belongs to the merger: it is not an implementer work state
    assert cfg.active_states == ("Todo", "In Progress", "Rework")
    assert cfg.investigation_label == "investigation"
    assert cfg.input_timeout_ms == 900_000
    assert cfg.notify_users == {"slack": {}, "discord": {}}


def test_tracker_states_override_per_key_and_keep_the_other_defaults():
    cfg = parse_workflow(
        "---\ntracker:\n  kind: memory\n  active_states: [Todo, Doing, Fix It]\n"
        "  states: {review: Code Review, rework: Fix It}\n---\nx"
    )
    assert cfg.tracker_states == {
        "review": "Code Review", "rework": "Fix It", "merging": "Merging", "done": "Done",
    }


@pytest.mark.parametrize(
    "tracker",
    [
        "states: {approved: Approved}",                       # unknown role
        "states: {review: ''}",                               # empty name
        "states: [In Review]",                                # not a mapping
        "states: {rework: Redo}",                             # rework not an active state
        "active_states: [Todo, In Review, Rework]",           # review would redispatch
        "active_states: [Todo, Merging, Rework]",             # merging would redispatch
        "states: {done: Shipped}",                            # done not terminal
    ],
)
def test_tracker_states_that_break_the_pipeline_are_rejected(tracker):
    with pytest.raises(ConfigError):
        parse_workflow(f"---\ntracker:\n  kind: memory\n  {tracker}\n---\nx")


def test_notify_users_map_github_logins_case_insensitively_per_service():
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nnotify:\n  users:\n"
        "    slack: {Alice-GH: U012ABC, bob: U034DEF}\n"
        "    discord: {Alice-GH: '123456789012345678', bob: 223456789012345678}\n---\nx"
    )
    assert cfg.notify_users == {
        "slack": {"alice-gh": "U012ABC", "bob": "U034DEF"},
        # an unquoted snowflake (a YAML integer) is kept exactly
        "discord": {"alice-gh": "123456789012345678", "bob": "223456789012345678"},
    }
    only_slack = parse_workflow(
        "---\ntracker: {kind: memory}\nnotify: {users: {slack: {carol: U1}}}\n---\nx"
    )
    assert only_slack.notify_users == {"slack": {"carol": "U1"}, "discord": {}}
    for bad in (
        "users: [alice]",
        "users: {alice: U012ABC}",          # the flat map is gone: services are explicit
        "users: {teams: {alice: x}}",
        "users: {slack: [alice]}",
        "users: {slack: {alice: ''}}",
        "users: {slack: {alice: 7}}",
        "users: {discord: {alice: U012ABC}}",  # not a snowflake
        "users: {discord: {alice: true}}",
    ):
        with pytest.raises(ConfigError):
            parse_workflow(f"---\ntracker: {{kind: memory}}\nnotify: {{{bad}}}\n---\nx")


def test_input_timeout_and_investigation_label_are_read():
    cfg = parse_workflow(
        "---\ntracker: {kind: memory, investigation_label: Spike}\n"
        "agent: {input_timeout_ms: 60000}\n---\nx"
    )
    assert cfg.input_timeout_ms == 60_000
    assert cfg.investigation_label == "spike"  # labels are compared casefolded
    with pytest.raises(ConfigError):
        parse_workflow("---\ntracker: {kind: memory}\nagent: {input_timeout_ms: 0}\n---\nx")


def test_the_retired_pre_pr_review_key_fails_loudly():
    """It ran the automated reviewer's CLI; the merger reviews every pull request now, so
    silently accepting it would promise a step that no longer happens."""
    with pytest.raises(ConfigError, match="pre_pr_review"):
        parse_workflow("---\ntracker: {kind: memory}\nkubernetes: {pre_pr_review: true}\n---\nx")


def test_the_repository_workflow_parses_and_validates():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in [root / "WORKFLOW.md", *sorted((root / "docs" / "examples").glob("WORKFLOW.*.md"))]:
        cfg = parse_workflow(path.read_text())
        assert cfg.tracker_kind in ("linear", "github", "memory"), path.name
        assert cfg.polling_interval_ms <= 15_000, path.name  # a safety poll, not the dispatch path


# -- role model precedence (internals §4) ---------------------------------------


def test_role_model_precedence_teamconfig_then_workflow_then_default(redis_client):
    from symphony import teamconfig
    from symphony.config import bind_role_model

    team = teamconfig.TeamConfig(redis_client, "test")
    bare = parse_workflow(MINIMAL)
    with_engine = parse_workflow(
        "---\ntracker: {kind: memory}\nengine: {provider: acme, model: acme-1}\n---\nx"
    )

    # nothing published, no engine model: the built-in default for the role
    bound = bind_role_model(bare, team.get(), "reflector")
    default = teamconfig.DEFAULT["models"]["reflector"]
    assert (bound.engine_provider, bound.engine_model, bound.engine_thinking) == (
        default["provider"], default["model"], default["thinking"],
    )
    # nothing published: WORKFLOW engine.provider/model wins over the default
    bound = bind_role_model(with_engine, team.get(), "implementer")
    assert (bound.engine_provider, bound.engine_model) == ("acme", "acme-1")

    # a publication wins over WORKFLOW.md, per role
    team.publish(
        {"models": {"reflector": {"provider": "amazon-bedrock", "model": "reflector-x",
                                  "thinking": "low"}}},
        expected_generation=0, actor="admin", reason="cheaper retros",
    )
    bound = bind_role_model(with_engine, team.get(), "reflector")
    assert (bound.engine_provider, bound.engine_model, bound.engine_thinking) == (
        "amazon-bedrock", "reflector-x", "low",
    )
    assert with_engine.engine_model == "acme-1"  # binding never mutates the policy
    with pytest.raises(ConfigError):
        bind_role_model(bare, team.get(), "janitor")


# -- seeded memory board (cross-process board for clusters without Linear) -------

SEEDED = """---
tracker:
  kind: memory
  issues:
    - {id: i1, identifier: SYM-1, title: Add health route, labels: [symphony, "repo:acme/api"]}
    - {id: i2, identifier: SYM-2, title: Spike, description: "why slow?", state: Done}
---
x
"""


def test_tracker_issues_seed_the_memory_board_with_defaults():
    cfg = parse_workflow(SEEDED)
    assert cfg.memory_issues == (
        {"id": "i1", "identifier": "SYM-1", "title": "Add health route", "description": None,
         "state": "Todo", "labels": ("symphony", "repo:acme/api")},
        {"id": "i2", "identifier": "SYM-2", "title": "Spike", "description": "why slow?",
         "state": "Done", "labels": ()},
    )
    assert parse_workflow(MINIMAL).memory_issues == ()


@pytest.mark.parametrize(
    "issues",
    [
        "{id: i1}",                                                   # not a list
        "[{id: i1, identifier: A-1}]",                               # no title
        "[{id: '', identifier: A-1, title: t}]",                     # blank id
        "[{id: i1, identifier: A-1, title: t, state: 3}]",           # non-string state
        "[{id: i1, identifier: A-1, title: t, labels: symphony}]",   # labels not a list
        "[{id: i1, identifier: A-1, title: t, priority: 1}]",        # unknown key
        "[{id: i1, identifier: A-1, title: t}, {id: i1, identifier: A-2, title: u}]",  # dup id
        "[{id: i1, identifier: A-1, title: t}, {id: i2, identifier: A-1, title: u}]",  # dup key
    ],
)
def test_malformed_seed_tickets_are_rejected(issues):
    with pytest.raises(ConfigError):
        parse_workflow(f"---\ntracker:\n  kind: memory\n  issues: {issues}\n---\nx")


def test_seed_tickets_are_refused_on_a_real_tracker():
    """A seed on a Linear board would be a second, fake source of truth beside Linear."""
    with pytest.raises(ConfigError, match="memory"):
        parse_workflow(
            "---\ntracker:\n  kind: linear\n  provider: {project_slug: p}\n"
            "  issues: [{id: i1, identifier: A-1, title: t}]\n---\nx"
        )
