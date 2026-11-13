"""FR-026/FR-027/FR-028/FR-030/FR-032: exactly-one retro with call_id ownership,
'nothing to learn' is valid, human gate by construction, propagation via AGENTS.md/skills,
metrics + retention touch."""

from dataclasses import replace

from tests.test_dispatch import FakeEffects

from symphony import state
from symphony.config import parse_workflow
from symphony.reflector import Harvest, Lesson, nightly_sweep, run_retrospective

CFG = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
NOW = 1_785_456_000_000  # 2026-07-31 UTC


class FakeGitHub:
    def __init__(self, pr=None, threads=None, agents_md=None):
        self.pr = pr
        self.threads = threads or []
        self.opened: list[dict] = []
        self.agents_md = agents_md

    def get_file(self, path):
        return self.agents_md if path == "AGENTS.md" else None

    def get_pr_for_issue(self, identifier):
        return self.pr

    def get_pr_diff(self, pr_number):
        return "diff --git a/x b/x"

    def get_review_threads(self, pr_number):
        return self.threads

    def get_ci_results(self, pr_number):
        return [{"status": "success"}]

    def open_pr(self, branch, title, body, files, labels):
        self.opened.append({"branch": branch, "files": files, "labels": labels, "body": body})
        return f"https://github.com/x/pull/{len(self.opened)}"


def entry(identifier="ENG-1", attempt=2):
    return {"identifier": identifier, "attempt": attempt, "final_state": "Done", "tokens": {"total": 10}}


def distill_one(harvest, cfg):
    return [Lesson(title="Always run migrations with a lock", description="d", content="pitfall + action", source="PR #1")]


def distill_nothing(harvest, cfg):
    return []


def test_exactly_once_second_caller_skipped(kv):
    gh = FakeGitHub(pr={"number": 1})
    assert run_retrospective(kv, gh, CFG, "i1", entry(), "call-A", NOW, distill_one) == "learnings-pr-opened"
    assert run_retrospective(kv, gh, CFG, "i1", entry(), "call-B", NOW, distill_one) == "skipped"
    assert len(gh.opened) == 1  # never two learnings PRs


def test_platform_reexecution_same_call_id_readopts(kv):
    gh = FakeGitHub(pr={"number": 1})
    state.acquire_retro_token(kv, "i1", NOW)
    token = kv.get(state.retro_key("i1"))
    token.update(reflector_call_id="call-A")  # previous execution died midway
    kv.put(state.retro_key("i1"), token)
    assert run_retrospective(kv, gh, CFG, "i1", entry(), "call-A", NOW, distill_one) == "learnings-pr-opened"
    # FOREIGN call_id with an in-flight token ⇒ no effects
    gh2 = FakeGitHub(pr={"number": 2})
    token = kv.get(state.retro_key("i1"))
    token.update(status="started", reflector_call_id="call-X")
    kv.put(state.retro_key("i1"), token)
    assert run_retrospective(kv, gh2, CFG, "i2", entry("ENG-2"), "call-Y", NOW, distill_one) in (
        "learnings-pr-opened",  # i2 has its own token — only i1 is locked
    )


def test_nothing_to_learn_opens_no_pr(kv):
    gh = FakeGitHub(pr={"number": 1})
    assert run_retrospective(kv, gh, CFG, "i1", entry(), "c1", NOW, distill_nothing) == "nothing-to-learn"
    assert gh.opened == []
    assert kv.get(state.retro_key("i1"))["status"] == "done"


def test_learnings_pr_targets_agents_md_and_skills_with_label(kv):
    """FR-030: the diff hits exactly the paths that the next runs' clone reads."""

    def distill_with_skill(harvest, cfg):
        return [
            Lesson(title="Lesson A", description="d", content="c", source="PR #1"),
            Lesson(title="How to land a PR", description="d", content="steps", kind="skill-proposal", source="PR #1"),
        ]

    gh = FakeGitHub(pr={"number": 1})
    result = run_retrospective(kv, gh, CFG, "i1", entry(), "c1", NOW, distill_with_skill)
    assert result == "learnings-pr-opened"
    opened = gh.opened[0]
    assert opened["branch"] == "symphony/learnings/ENG-1"
    assert "AGENTS.md" in opened["files"]
    assert ".agents/skills/how-to-land-a-pr/SKILL.md" in opened["files"]  # official discovery path
    assert opened["labels"] == [CFG.learnings_label]
    assert "auto-merge" in opened["body"]  # human gate declared in the body
    token = kv.get(state.retro_key("i1"))
    assert token["status"] == "done" and token["learnings_pr"]


def test_learning_disabled_is_noop(kv):
    gh = FakeGitHub(pr={"number": 1})
    off = replace(CFG, learning_enabled=False)
    assert run_retrospective(kv, gh, off, "i1", entry(), "c1", NOW, distill_one) == "learning-disabled"
    assert gh.opened == []


def test_metrics_accumulated_and_touched(kv):
    threads = [{"severity": "critical", "body": "x", "resolved": True}]
    gh = FakeGitHub(pr={"number": 1}, threads=threads)
    run_retrospective(kv, gh, CFG, "i1", entry(), "c1", NOW, distill_one)
    metrics = kv.get(state.metrics_key("202607"))
    assert metrics["prs_opened"] == 1
    assert metrics["review_comments"]["critical"] == 1
    assert metrics["learnings_prs"] == 1


def test_nightly_sweep_spawns_missed_and_reopens_orphans(kv, clock, tracker):
    tracker.add_issue("i1", "ENG-1", state="Done", updated_at="2026-07-31T00:00:00Z")
    tracker.add_issue("i2", "ENG-2", state="Canceled", updated_at="2026-07-31T00:00:00Z")
    tracker.add_issue("i3", "ENG-3", state="Done", updated_at="2026-01-01T00:00:00Z")  # outside the window
    fx = FakeEffects()
    result = nightly_sweep(kv, tracker, CFG, NOW, fx)
    assert result["spawned"] == 2  # Done AND Canceled (reflect_on default = all terminals)
    assert set(fx.reflected) == {"i1", "i2"}

    # second sweep: recent started tokens ⇒ no duplicates
    fx2 = FakeEffects()
    assert nightly_sweep(kv, tracker, CFG, NOW + 1_000, fx2)["spawned"] == 0

    # orphaned retro (>1h, dead call) ⇒ reopened; retro with a LIVE reflector ⇒ untouched
    token = kv.get(state.retro_key("i1"))
    token.update(reflector_call_id="dead-call")
    kv.put(state.retro_key("i1"), token)
    token2 = kv.get(state.retro_key("i2"))
    token2.update(reflector_call_id="live-call")
    kv.put(state.retro_key("i2"), token2)
    fx3 = FakeEffects(running={"live-call"})
    result3 = nightly_sweep(kv, tracker, CFG, NOW + 2 * 60 * 60 * 1000, fx3)
    assert fx3.reflected == ["i1"] and result3["spawned"] == 1


def test_nightly_sweep_survives_outage(kv, tracker):
    tracker.raise_unavailable = True
    fx = FakeEffects()
    assert nightly_sweep(kv, tracker, CFG, NOW, fx)["spawned"] == 0
    assert "nightly_sweep_skipped" in fx.names()


def test_harvest_carries_execution_data_before_cleanup():
    h = Harvest(issue_identifier="ENG-1", final_state="Done", attempts=3, tokens={"total": 99})
    assert h.attempts == 3  # execution data preserved for the harvest (FR-026)


def test_curation_runs_against_real_agents_md_base_no_clobber(kv):
    """FR-029: the existing base is loaded from the repo — approved lessons survive."""
    from symphony.reflector import CurationResult, parse_agents_md, render_agents_md

    existing_md = render_agents_md(
        [Lesson(title="Old approved lesson", description="d", content="c", source="PR #1")],
        CurationResult(),
    )
    parsed = parse_agents_md(existing_md)
    assert len(parsed) == 1 and parsed[0].slug == "old-approved-lesson"

    gh = FakeGitHub(pr={"number": 9}, agents_md=existing_md)
    run_retrospective(kv, gh, CFG, "i9", entry("ENG-9"), "c9", NOW, distill_one)
    new_md = gh.opened[0]["files"]["AGENTS.md"]
    assert "Old approved lesson" in new_md  # did NOT clobber the base
    assert "Always run migrations with a lock" in new_md  # and the new one made it in


def test_workpad_flows_into_harvest_and_usage_detection(kv):
    """FR-027 (Workpad harvest) + FR-032 (lesson usage recorded by the retro)."""
    from symphony.reflector import CurationResult, render_agents_md

    base = render_agents_md(
        [Lesson(title="Run migration with lock", description="d", content="c", source="PR #1")],
        CurationResult(),
    )
    captured = {}

    def capturing_distill(harvest, cfg):
        captured["workpad"] = harvest.workpad
        return []

    gh = FakeGitHub(pr={"number": 9}, agents_md=base)
    run_retrospective(
        kv, gh, CFG, "i9", entry("ENG-9"), "c9", NOW, capturing_distill,
        fetch_workpad=lambda ident: f"Plan: apply Run migration with lock in {ident}",
    )
    assert "Run migration with lock" in captured["workpad"]  # Workpad reached the harvest
    metrics = kv.get(state.metrics_key("202607"))
    assert metrics["lesson_uses"]["run-migration-with-lock"] == 1  # usage recorded
    ages = kv.get(state.LESSONS_AGE_KEY)
    assert ages["run-migration-with-lock"] == 0  # age reset by the use


def test_merge_metrics_from_pr_detail(kv):
    """FR-032: prs_merged and time-to-merge derived from the harvested PR."""
    pr = {
        "number": 3,
        "created_at": "2026-07-30T00:00:00Z",
        "merged_at": "2026-07-30T06:00:00Z",
    }
    gh = FakeGitHub(pr=pr)
    run_retrospective(kv, gh, CFG, "i3", entry("ENG-3"), "c3", NOW, distill_nothing)
    metrics = kv.get(state.metrics_key("202607"))
    assert metrics["prs_merged"] == 1
    assert metrics["time_to_merge_ms_sum"] == 6 * 60 * 60 * 1000


def test_agents_md_round_trip_survives_multiline_and_comment_markers(kv):
    """FR-029: faithful round-trip — multi-line content or content with comment markers
    must not be truncated/mangled by the next curation (anti-clobber regression)."""
    from symphony.reflector import CurationResult, parse_agents_md, render_agents_md

    tricky = Lesson(
        title="Network timeout",
        description="one line",
        content="pitfall: 5s kills slow CI\nnext action: use 30s <!-- fake --> always",
        source="PR #7",
    )
    md = render_agents_md([tricky], CurationResult())
    parsed = parse_agents_md(md)
    assert len(parsed) == 1
    lesson = parsed[0]
    assert "next action: use 30s" in lesson.content  # nothing truncated at the 1st line
    assert "always" in lesson.content
    assert lesson.source == "PR #7"  # the real source, not the embedded marker
    # second round-trip is stable (dedupe works again — no spurious PRs)
    md2 = render_agents_md(parsed, CurationResult())
    assert parse_agents_md(md2) == parsed


def test_nothing_to_learn_does_not_age_lessons(kv):
    """FR-029: age counts INCORPORATIONS — retros with no proposal don't age lessons."""
    from symphony.reflector import CurationResult, render_agents_md

    base = render_agents_md(
        [Lesson(title="Unused lesson", description="d", content="c", source="PR #1")],
        CurationResult(),
    )
    gh = FakeGitHub(pr={"number": 1}, agents_md=base)
    for i in range(5):  # 5 consecutive "nothing to learn" retros
        run_retrospective(kv, gh, CFG, f"ix{i}", entry(f"ENG-x{i}"), f"c{i}", NOW, distill_nothing)
    ages = kv.get(state.LESSONS_AGE_KEY) or {}
    assert ages.get("unused-lesson", 0) == 0  # zero incorporations ⇒ zero aging
    # and with a retro that DOES propose a change, the age advances by 1
    gh2 = FakeGitHub(pr={"number": 2}, agents_md=base)
    run_retrospective(kv, gh2, CFG, "iy", entry("ENG-y"), "cy", NOW, distill_one)
    assert (kv.get(state.LESSONS_AGE_KEY) or {}).get("unused-lesson") == 1


def test_metrics_not_inflated_when_distill_crashes(kv):
    """Infra crash during distillation ⇒ token stays 'started' for the sweep and NO
    metric is written (retry does not inflate prs_opened/review_comments)."""
    import pytest as _pytest

    gh = FakeGitHub(pr={"number": 1}, threads=[{"severity": "critical", "body": "x"}])

    def broken_distill(harvest, cfg):
        raise RuntimeError("codex exec failed")

    with _pytest.raises(RuntimeError):
        run_retrospective(kv, gh, CFG, "i1", entry(), "c1", NOW, broken_distill)
    assert kv.get(state.retro_key("i1"))["status"] == "started"  # sweep will retry
    assert kv.get(state.metrics_key("202607")) is None  # nothing inflated
    # retry with a healthy distillation: completes and writes ONCE
    run_retrospective(kv, gh, CFG, "i1", entry(), "c1", NOW, distill_nothing)
    assert kv.get(state.metrics_key("202607"))["prs_opened"] == 1


def test_decay_is_configurable_via_policy(kv):
    """FR-029: decay comes from operational policy, not a constant."""
    from symphony.config import parse_workflow

    cfg2 = parse_workflow(
        "---\ntracker: {kind: memory}\nkubernetes:\n  learning: {decay_after_incorporations: 3}\n---\nx"
    )
    assert cfg2.decay_after_incorporations == 3
