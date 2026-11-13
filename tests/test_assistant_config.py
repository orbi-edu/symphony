"""The `assistant:` WORKFLOW section and the assistant's model roles."""

from __future__ import annotations

import json

import pytest

from symphony import teamconfig
from symphony.config import ConfigError, parse_workflow, role_model
from symphony.teamconfig import TeamConfig


def parse(section: str, extra: str = ""):
    return parse_workflow(f"---\ntracker: {{kind: memory}}\n{extra}{section}---\nx")


def test_the_assistant_section_parses():
    cfg = parse(
        "assistant:\n"
        "  enabled: true\n"
        "  slack: {channels: [C0123ABCD]}\n"
        "  discord: {channels: [123456789012345678]}\n"
        "  repositories: [acme/app]\n"
        "  max_concurrent: 2\n"
        "  turn_timeout_s: 120\n"
        "  ticket_state: Triage\n"
        "  ticket_labels: [from-chat]\n"
    )
    assert cfg.assistant_enabled is True
    assert cfg.assistant_slack_channels == ("C0123ABCD",)
    assert cfg.assistant_discord_channels == ("123456789012345678",)  # an unquoted snowflake
    assert cfg.assistant_repositories == ("acme/app",)
    assert (cfg.assistant_max_concurrent, cfg.assistant_turn_timeout_s) == (2, 120)
    assert cfg.assistant_ticket_state == "Triage"
    assert cfg.assistant_ticket_labels == ("from-chat",)


def test_no_section_means_disabled_with_defaults():
    cfg = parse("")
    assert cfg.assistant_enabled is False
    assert cfg.assistant_repositories is None
    assert cfg.assistant_max_concurrent == 3 and cfg.assistant_turn_timeout_s == 600
    assert cfg.assistant_ticket_state == "Backlog" and cfg.assistant_ticket_labels == ()


@pytest.mark.parametrize("section, message", [
    ("assistant: {enabled: true, channels: [C1]}\n", "unknown keys channels"),
    ("assistant: {slack: {channels: [C1], bots: true}}\n", "assistant.slack has unknown keys"),
    ("assistant: {slack: {channels: C1}}\n", "list of channel ids"),
    ("assistant: {discord: {channels: [general]}}\n", "a Discord channel id is a number"),
    ("assistant: {repositories: [app]}\n", "owner/name"),
    ("assistant: {max_concurrent: 0}\n", "max_concurrent must be positive"),
    ("assistant: {turn_timeout_s: -1}\n", "turn_timeout_s must be positive"),
    ("assistant: {enabled: yes-please}\n", "must be bool"),
    ("assistant: {ticket_state: ''}\n", "ticket_state"),
    ("assistant: [enabled]\n", "must be a mapping"),
])
def test_invalid_sections_are_refused(section, message):
    with pytest.raises(ConfigError, match=message):
        parse(section)


def test_ticket_labels_never_start_a_ticket():
    assert parse("assistant: {ticket_labels: [Symphony]}\n").assistant_ticket_labels == ("Symphony",)
    text = ("---\ntracker: {kind: memory, required_labels: [symphony]}\n"
            "assistant: {ticket_labels: [Symphony]}\n---\nx")
    with pytest.raises(ConfigError, match="required label 'Symphony'"):
        parse_workflow(text)


def test_the_repository_workflow_parses():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "WORKFLOW.md").read_text()
    cfg = parse_workflow(text, {"LINEAR_API_KEY": "x"})
    assert cfg.assistant_enabled is False


def test_the_assistant_roles_have_defaults():
    models = teamconfig.DEFAULT["models"]
    assert {"assistant", "researcher", "planner"} <= set(teamconfig.ROLES)
    assert models["assistant"] == {"provider": "amazon-bedrock",
                                   "model": "global.anthropic.claude-haiku-5-5", "thinking": "low"}
    assert models["researcher"] == {"provider": "amazon-bedrock",
                                    "model": "global.moonshotai.kimi-k3.1", "thinking": "high"}
    assert models["planner"] == {"provider": "amazon-bedrock",
                                 "model": "global.anthropic.claude-haiku-5-5", "thinking": "medium"}
    teamconfig.validate(teamconfig.DEFAULT)
    cfg = parse("")
    assert role_model(cfg, None, "planner") == models["planner"]


def test_a_document_published_before_the_new_roles_still_names_every_role(redis_client):
    old = json.loads(json.dumps(teamconfig.DEFAULT))
    for role in ("assistant", "researcher", "planner"):
        del old["models"][role]
    old["generation"] = 4
    redis_client.set("sym:t:teamconfig", json.dumps(old))
    team = TeamConfig(redis_client, "t")
    assert team.model("researcher") == teamconfig.DEFAULT["models"]["researcher"]
    doc = team.publish({"models": {"planner": {"thinking": "high"}}}, expected_generation=4,
                       actor="admin", reason="tune the planner")
    assert doc["models"]["planner"]["thinking"] == "high"
    assert set(doc["models"]) == set(teamconfig.ROLES)
    with pytest.raises(teamconfig.ConfigError, match="thinking"):
        team.publish({"models": {"assistant": {"thinking": "loud"}}}, expected_generation=5,
                     actor="admin", reason="typo")
