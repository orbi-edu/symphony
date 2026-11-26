"""Inline review comments (`merger/inline.py`): after the report, one COMMENT review puts the
blocking and confirmed findings on their diff lines; re-reviews neither repeat nor leave them
open. End to end over the in-memory GitHub, plus the placement rules on their own."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs
from tests.test_merger_support import (
    FakeGitHub,
    FakeTracker,
    ScriptedAgents,
    World,
    done_review,
    done_verify,
    finding,
    ids_in,
    make_merger,
    verdict,
)

from symphony.merger import inline as ir
from symphony.merger.approval import is_approved
from symphony.merger.github import GitHubError
from symphony.merger.policy import PolicyError, parse
from symphony.merger.review import ReviewWorker

APP = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(12))  # 48 lines; f1 returns on line 6
ONE_LANE = "---\nlanes:\n  - key: correctness\n    audits: correctness and regressions\n{extra}---\nRules.\n"
BUG = "f1 divides by zero"


def make_env(redis_client, tmp_path, extra: str = ""):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"), policy=ONE_LANE.format(extra=extra))
    world.commit("main", {"src/app.py": APP}, "app: many functions", base="main")
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=FakeTracker())
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, worker=ReviewWorker(m, "1"))


@pytest.fixture
def env(redis_client, tmp_path):
    return make_env(redis_client, tmp_path)


def script(env, findings: list[tuple], verdicts: dict[str, tuple]) -> None:
    """The reviewer reports `findings`; the oracle answers `verdicts[id]`, else a confirmed nit."""
    env.agents.scripts.update(
        reviewer=lambda r, p, c: [*findings, done_review()],
        oracle=lambda r, p, c: [*(verdicts.get(i) or verdict(i, severity="nit", cls="nit") for i in ids_in(p)),
                                done_verify()],
    )


def open_pr(env):
    env.world.commit("eng-12-avg", {"src/app.py": APP.replace("return 1\n", "return 1 / 0\n", 1)}, "app: f1")
    env.gh.open_pr(7, "eng-12-avg", "ENG-12 add avg")
    env.gh.tick()


def push_again(env):
    """The author changes f8 (line 34), far from f1, and marks the PR ready again."""
    text = APP.replace("return 1\n", "return 1 / 0\n", 1).replace("return 8\n", "return 80\n", 1)
    env.world.commit("eng-12-avg", {"src/app.py": text}, "app: f8")
    env.gh.prs[7]["draft"] = False
    env.gh.prs[7]["ready_at"] = "2026-11-02T10:00:00Z"
    env.gh.tick()


def blocked_with_a_bug(env) -> None:
    script(env, [finding("src/app.py:6", BUG)], {"A": verdict("A")})
    open_pr(env)
    assert env.worker.step() == "#7 blocked"


def test_blocking_and_confirmed_findings_on_the_diff_get_one_comment_review(env):
    script(env, [finding("src/app.py:6", BUG),
                 finding("src/app.py:4-7", "f0's spacing hides f1", cls="nit", severity="nit"),
                 finding("src/app.py:30", "f7 has no docstring", cls="nit", severity="nit"),
                 finding("PR body", "the body names no test", cls="nit", severity="nit")],
           {"A": verdict("A")})
    open_pr(env)
    assert env.worker.step() == "#7 blocked"
    (summary,) = env.gh.comments[7]
    assert "### C: nit, confirmed" in summary["body"] and "### D: nit, confirmed" in summary["body"]
    (review,) = env.gh.posted_reviews
    assert (review["pr"], review["event"], review["commit_id"]) == (7, "COMMENT", env.world.head("eng-12-avg"))
    assert summary["url"] in review["body"]
    first, second = review["comments"]
    assert {k: first[k] for k in ("path", "line", "side")} == {"path": "src/app.py", "line": 6, "side": "RIGHT"}
    assert "start_line" not in first
    assert {k: second[k] for k in ("path", "line", "start_line", "side", "start_side")} == {
        "path": "src/app.py", "line": 7, "start_line": 4, "side": "RIGHT", "start_side": "RIGHT"}
    assert first["body"].startswith(f"**A** (blocking) — {BUG}\n\nFix:\n- [ ] fix it")
    assert second["body"].startswith("**B** (nit) — f0's spacing hides f1")
    assert all(f"[Review report]({summary['url']})" in c["body"] for c in review["comments"])
    threads = env.m.store.inline(7)["threads"]
    assert sorted(t["id"] for t in threads.values()) == ["A", "B"]
    assert {t["comment"] for t in threads.values()} == {c["id"] for c in review["comments"]}


def test_a_replacement_becomes_a_suggestion_unless_it_is_large(env):
    script(env, [finding("src/app.py:6", "f1 should return 1", cls="nit", severity="nit",
                         replacement="    return 1\n"),
                 finding("src/app.py:7-8", "the gap after f1 is odd", cls="nit", severity="nit",
                         replacement="\n" * 31 + "x\n")], {})
    open_pr(env)
    assert env.worker.step() == "#7 reviewed"
    (review,) = env.gh.posted_reviews
    small, large = review["comments"]
    assert "```suggestion\n    return 1\n```" in small["body"]
    assert "suggestion" not in large["body"]


def test_a_re_review_does_not_repeat_a_commented_finding(env):
    blocked_with_a_bug(env)
    push_again(env)
    script(env, [finding("src/app.py:6", BUG)], {"A": verdict("A")})
    assert env.worker.step() == "#7 reviewed"  # outside the once-only scope now: a nit
    assert len(env.gh.posted_reviews) == 1 and env.gh.resolved == []
    assert list(env.m.store.inline(7)["threads"].values())[0]["head"] == env.world.head("eng-12-avg")


def test_a_finding_that_reopens_the_commented_one_is_not_commented_again(env):
    blocked_with_a_bug(env)
    push_again(env)
    script(env, [finding("src/app.py:6", "f1 still raises ZeroDivisionError", reopens="A")],
           {"A": verdict("A", severity="nit", cls="nit")})
    assert env.worker.step() == "#7 reviewed"
    assert len(env.gh.posted_reviews) == 1 and env.gh.resolved == []


def test_a_fixed_finding_has_its_thread_resolved_once(env):
    blocked_with_a_bug(env)
    (comment,) = env.gh.posted_reviews[0]["comments"]
    push_again(env)
    script(env, [], {})
    assert env.worker.step() == "#7 reviewed"
    assert env.gh.resolved == [f"PRRT_{comment['id']}"]
    assert len(env.gh.posted_reviews) == 1
    assert env.m.store.inline(7) is None  # nothing left to resolve a second time


def test_a_rejected_review_post_leaves_the_round_and_its_report(env):
    env.gh.fail_next["post_review"] = GitHubError("POST /pulls/7/reviews: 422 line must be part of the diff", 422)
    script(env, [finding("src/app.py:6", BUG)], {"A": verdict("A")})
    open_pr(env)
    with capture_logs() as logs:
        assert env.worker.step() == "#7 blocked"
    (summary,) = env.gh.comments[7]
    assert summary["body"].startswith("**Not merged**")
    assert env.gh.posted_reviews == [] and env.m.store.inline(7) is None
    (warning,) = [e for e in logs if e["event"] == "merger_inline_post_failed"]
    assert (warning["log_level"], warning["status"]) == ("warning", 422)


def test_the_comment_review_is_no_approval(redis_client, tmp_path):
    env = make_env(redis_client, tmp_path, "require_approval: true\n")
    script(env, [finding("src/app.py:6", "f1 should return 1", cls="nit", severity="nit")], {})
    open_pr(env)
    assert env.worker.step() == "#7 waiting"
    (review,) = env.gh.posted_reviews
    assert review["event"] == "COMMENT"
    # Even listed as a review on the PR, a COMMENT review never approves.
    env.gh.pr_reviews[7] = [{"user": "symphony-merger[bot]", "state": "COMMENTED",
                             "submitted_at": "2026-11-01T12:00:00Z"}]
    assert not is_approved(env.m, 7)
    env.worker.step()
    assert env.m.store.parked(7)["waiting"] == "approval" and env.m.store.rec(7) is None


def test_the_policy_turns_inline_comments_off(redis_client, tmp_path):
    env = make_env(redis_client, tmp_path, "inline_comments: false\n")
    script(env, [finding("src/app.py:6", BUG)], {"A": verdict("A")})
    open_pr(env)
    assert env.worker.step() == "#7 blocked"
    assert env.gh.comments[7] and env.gh.posted_reviews == []
    assert "post_review" not in env.gh.calls


def test_placement_takes_only_lines_github_can_comment_on():
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -8,7 +8,8 @@ def f():\n"
            "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-x\n-y\n")
    hunks = ir.commentable(diff)
    assert hunks == {"a.py": [(8, 15)]}
    assert ir.place("a.py:10", "", hunks) == ({"path": "a.py", "line": 10, "side": "RIGHT"}, True)
    assert ir.place("/wt/pr-7/a.py:9-12", "/wt/pr-7", hunks) == (
        {"path": "a.py", "line": 12, "side": "RIGHT", "start_line": 9, "start_side": "RIGHT"}, True)
    # a range starting above its hunk is clamped to it: no longer exact, so no suggestion
    assert ir.place("a.py:2-9", "", hunks) == (
        {"path": "a.py", "line": 9, "side": "RIGHT", "start_line": 8, "start_side": "RIGHT"}, False)
    assert ir.place("a.py:12-20", "", hunks) is None  # the end is outside the diff
    assert ir.place("gone.py:1", "", hunks) is None
    assert ir.place("a.py", "", hunks) is None
    assert ir.place("PR body", "", hunks) is None


def test_a_suggestion_needs_a_confirmed_exact_replacement():
    f = {"verdict": "confirmed", "replacement": "a = 1\n"}
    assert ir.suggestion(f, True) == "```suggestion\na = 1\n```"
    assert ir.suggestion(f, False) == ""
    assert ir.suggestion({**f, "verdict": "partial"}, True) == ""
    assert ir.suggestion({**f, "replacement": "```\ncode\n```"}, True).startswith("````suggestion\n")


def test_inline_comments_must_be_a_boolean():
    assert parse(None).inline_comments is True
    assert parse("---\ninline_comments: false\n---\n").inline_comments is False
    with pytest.raises(PolicyError, match="inline_comments"):
        parse("---\ninline_comments: maybe\n---\n")
