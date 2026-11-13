"""STRICT Liquid (upstream spec §6): unknown variable or filter ⇒ failure — plus the branches
of the real WORKFLOW.md prompt. The WORKFLOW assertions check structure and contract terms
(tool names, configured state names, repository paths), never wording."""

from pathlib import Path

import pytest

from symphony.config import parse_workflow
from symphony.prompt import PromptError, render_prompt
from symphony.trackers.memory import MemoryTracker


@pytest.fixture
def issue():
    return MemoryTracker().add_issue("i1", "ENG-1", title="Fix bug")


def test_renders_issue_and_attempt(issue):
    out = render_prompt("{{ issue.identifier }}#{{ attempt }}: {{ issue.title }}", issue, 2)
    assert out == "ENG-1#2: Fix bug"


def test_unknown_variable_fails(issue):
    with pytest.raises(PromptError):
        render_prompt("{{ nao_existe }}", issue, 1)


def test_unknown_filter_fails(issue):
    with pytest.raises(PromptError):
        render_prompt("{{ issue.title | filtro_inexistente }}", issue, 1)


def test_extra_guidance_is_appended_not_templated(issue):
    out = render_prompt("base", issue, 1, extra_guidance="previous failure summary: X")
    assert out.startswith("base")
    assert "previous failure summary: X" in out


def test_investigation_flag_follows_the_configured_label():
    tracker = MemoryTracker()
    spike = tracker.add_issue("i1", "ENG-1", labels=("symphony", "spike"))
    body = "{% if issue.investigation %}I{% else %}C{% endif %}"
    assert render_prompt(body, spike, 1) == "C"  # default label is `investigation`
    assert render_prompt(body, spike, 1, investigation_label="Spike") == "I"
    plain = tracker.add_issue("i2", "ENG-2", labels=("symphony", "investigation"))
    assert render_prompt(body, plain, 1) == "I"


# ---- the real WORKFLOW.md ----------------------------------------------------

CFG = parse_workflow(Path(__file__).resolve().parents[1].joinpath("WORKFLOW.md").read_text())
REVIEW = CFG.tracker_states["review"]


def _render(state="Todo", attempt=1, labels=("symphony",), prompt=None):
    issue = MemoryTracker(default_repo="acme/api").add_issue(
        "i1", "ENG-1", state=state, title="Fix bug", description="details", labels=labels
    )
    return render_prompt(
        prompt if prompt is not None else CFG.prompt_body,
        issue,
        attempt,
        investigation_label=CFG.investigation_label,
    )


@pytest.mark.parametrize("state", [*CFG.active_states, REVIEW, CFG.tracker_states["merging"]])
@pytest.mark.parametrize("labels", [("symphony",), ("symphony", "investigation"),
                                    ("symphony", "repo:acme/api", "repo:acme/web")])
@pytest.mark.parametrize("attempt", [1, 4])
def test_every_branch_renders_under_strict_liquid(state, labels, attempt):
    assert _render(state, attempt, labels)
    assert _render(state, attempt, labels, prompt=CFG.answer_prompt)


def test_the_prompt_names_the_host_tools_and_the_workpad():
    out = _render()
    for term in ("linear_graphql", "symphony_ask", "Workpad", "ENG-1", "details"):
        assert term in out


def test_an_implementation_hands_over_to_the_configured_review_state():
    assert REVIEW in _render()


def test_an_investigation_renders_its_own_objective_without_the_review_handover():
    investigation = _render(labels=("symphony", CFG.investigation_label))
    implementation = _render()
    assert investigation != implementation
    assert REVIEW not in investigation  # no pull request, so nothing for the merger
    assert CFG.tracker_states["done"] in investigation
    assert "linear_graphql" in investigation


def test_rework_adds_a_section_that_points_at_the_merger_report():
    """The merger's report is the comment headed by its marker; the implementer has to be
    told where to find it, or it re-plans from scratch."""
    from symphony.merger.comments import VERSION

    rework = _render(state=CFG.tracker_states["rework"])
    in_progress = _render(state="In Progress")
    assert len(rework) > len(in_progress)
    assert VERSION in rework
    assert REVIEW in rework  # back to review once fixed


def test_a_multi_repo_ticket_maps_every_repository_to_its_workspace_path():
    out = _render(labels=("symphony", "repo:acme/api", "repo:acme/web", "repo:acme/docs"))
    assert "acme/api" in out and "`/workspace/repos/web`" in out and "`/workspace/repos/docs`" in out
    single = _render(labels=("symphony", "repo:acme/api"))
    assert "acme/api" in single and "/workspace/repos/" not in single.replace("<name>", "")


def test_a_later_run_names_its_run_and_adds_guidance():
    first, later = _render(attempt=1), _render(attempt=3)
    assert "3" not in first.split("\n", 1)[0] and "3" in later.split("\n", 1)[0]
    assert len(later) > len(first)
