"""Stage 1 end to end: a local bare remote, the in-memory GitHub, and scripted agents that
answer through host_tool_call frames, so the real agent runtime, store and git paths run."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest
from tests.test_merger_support import (
    NS,
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

from symphony.merger import comments as cm
from symphony.merger.review import ReviewWorker
from symphony.sessions import SessionStore

APP = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(12))  # 48 lines


@pytest.fixture
def env(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"))
    world.commit("main", {"src/app.py": APP}, "app: many functions", base="main")
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=tracker, notes=notes)
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, tracker=tracker, notes=notes,
                           worker=ReviewWorker(m, "1"), sessions=SessionStore(redis_client, NS))


def open_pr(env, n=7, branch="eng-12-avg", files=None, title="ENG-12 add avg"):
    env.world.commit(branch, files or {"src/app.py": APP.replace("return 1\n", "return 1 / 0\n", 1)},
                     "app: change f1")
    env.gh.open_pr(n, branch, title)
    env.gh.tick()


def lane(prompt: str) -> str:
    return "correctness" if "audits: correctness and regressions" in prompt else "security"


def blocked_first_review(env):
    """#7's first review: lane 1 reports a bug, lane 2 a regression; the oracle confirms both but
    grades only the bug blocking, so only it blocks."""
    def reviewer(role, prompt, cwd):
        if lane(prompt) == "correctness":
            return [finding("src/app.py:6", "f1 divides by zero"), done_review()]
        return [finding("src/app.py:5", "f1 changes its callers' result", cls="regression"), done_review()]

    def oracle(role, prompt, cwd):
        out = []
        for fid in ids_in(prompt):
            out.append(verdict(fid) if fid == "A" else verdict(fid, severity="nit", cls="nit"))
        return [*out, done_verify()]

    env.agents.scripts.update(reviewer=reviewer, oracle=oracle)
    open_pr(env)
    return env.worker.step()


def test_first_review_runs_two_lanes_and_blocks_only_what_reviewer_and_oracle_agree_on(env):
    assert blocked_first_review(env) == "#7 blocked"
    pr = env.gh.prs[7]
    assert pr["draft"] is True
    assert cm.REVIEW_REQUESTED in pr["labels"] and cm.IN_AUDIT not in pr["labels"]
    (comment,) = env.gh.comments[7]
    assert comment["body"].startswith("**Not merged**")
    assert "### A: blocking (bug), confirmed" in comment["body"]
    assert "### B: nit, confirmed" in comment["body"]
    assert "back to draft" in comment["body"]
    assert env.agents.roles() == ["reviewer", "reviewer", "oracle"]
    assert all("--tools" in argv and "read,grep" in argv for _, _, argv in env.agents.prompts)
    assert ("ENG-12", "Rework") in env.tracker.moves
    assert env.notes[-1][0] == "back_to_author" and "went back to the author" in env.notes[-1][1]
    baseline = env.m.store.baseline(7)
    assert (baseline["rounds"], baseline["outcome"]) == (1, "blocked")
    assert env.m.store.skips()[7]["stop"] == "blocked"
    # every agent was a live session, with its events and transcript
    # session ids carry the repository: the same PR number elsewhere never shares one
    kinds = sorted(s["kind"] for s in env.sessions.list(limit=50) if s["sid"].startswith("merger:acme:app:7:"))
    assert kinds == ["oracle", "review", "review"]
    sid = next(s["sid"] for s in env.sessions.list(limit=50) if s["sid"].startswith("merger:acme:app:7:oracle"))
    assert {k for _, k, _ in env.sessions.read_events(sid)} >= {"symphony.state", "agent_start", "agent_end"}
    assert env.sessions.transcript(sid) and env.sessions.meta(sid)["state"] == "ended"
    assert env.m.store.reviewing() == {}


def test_a_finding_the_oracle_refutes_does_not_block_and_the_pr_is_handed_off(env):
    env.agents.scripts.update(
        reviewer=lambda r, p, c: ([finding("src/app.py:6", "f1 divides by zero")] if lane(p) == "correctness" else []) + [done_review()],
        oracle=lambda r, p, c: [verdict("A", "refuted", severity="nit", cls="nit"), done_verify()],
    )
    open_pr(env)
    assert env.worker.step() == "#7 reviewed"
    rec = env.m.store.rec(7)
    assert rec["state"] == "reviewed" and rec["head_sha"] == env.world.head("eng-12-avg")
    (comment,) = env.gh.comments[7]
    assert comment["body"].startswith("**Reviewed**") and "### A: nit (bug), refuted" not in comment["body"]
    assert "### A: nit, refuted" in comment["body"] and "Rejected findings" in comment["body"]
    assert cm.REVIEWED in env.gh.prs[7]["labels"] and env.gh.prs[7]["draft"] is False
    assert ("ENG-12", "Merging") in env.tracker.moves
    assert env.m.store.baseline(7)["outcome"] == "approved"


def _push_second_round(env, line_text="    return 35 / 0\n"):
    """The author fixes f1 and, far below it, changes f8 (lines 33-34)."""
    text = APP.replace("def f8():\n    return 8\n", "def f8():\n" + line_text, 1)
    env.world.commit("eng-12-avg", {"src/app.py": text}, "app: fix f1, change f8")
    env.gh.prs[7]["draft"] = False
    env.gh.prs[7]["ready_at"] = "2026-11-02T10:00:00Z"
    env.gh.tick()


def test_once_only_a_new_blocker_goes_to_the_arbiter_then_to_a_maintainer(env):
    blocked_first_review(env)
    _push_second_round(env)
    seen = {}

    def reviewer(role, prompt, cwd):
        seen["prompt"] = prompt
        return [finding("src/app.py:34", "f8 divides by zero"),
                finding("src/app.py:2", "f0 should validate input", cls="bug"), done_review()]

    env.agents.scripts.update(
        reviewer=reviewer,
        oracle=lambda r, p, c: [verdict("A"), verdict("B"), done_verify()],
        arbiter=lambda r, p, c: [("submit_arbitration", {"rulings": [
            {"id": "A", "ruling": "uphold", "reason": "a crash on every call"}], "summary": "real bug"})],
    )
    assert env.worker.step() == "#7 escalated"
    assert "ONCE ONLY" in seen["prompt"] and "RE-REVIEW" in seen["prompt"]
    assert env.agents.roles()[3:] == ["reviewer", "oracle", "arbiter"]
    pr = env.gh.prs[7]
    assert cm.NEEDS_MAINTAINER in pr["labels"] and pr["draft"] is False
    body = env.gh.comments[7][-1]["body"]
    assert "needs a maintainer" in body and "### A: blocking (bug), confirmed" in body
    assert "### B: nit (bug), confirmed" in body  # outside the once-only scope: reported, not blocking
    assert env.m.store.skips()[7]["stop"] == "escalated"
    assert env.notes[-1][0] == "needs_maintainer"
    assert ("ENG-12", "Rework") in env.tracker.moves and env.tracker.moves.count(("ENG-12", "Rework")) == 1
    # a maintainer removing the label makes the PR eligible again
    assert env.worker.step() is None
    env.gh.prs[7]["labels"].remove(cm.NEEDS_MAINTAINER)
    env.agents.scripts.update(reviewer=lambda r, p, c: [done_review()])
    assert env.worker.step() == "#7 reviewed"


def test_the_arbiter_dismissing_the_blocker_releases_the_pr(env):
    blocked_first_review(env)
    _push_second_round(env)
    env.agents.scripts.update(
        reviewer=lambda r, p, c: [finding("src/app.py:34", "f8 divides by zero"), done_review()],
        oracle=lambda r, p, c: [verdict("A"), done_verify()],
        arbiter=lambda r, p, c: [("submit_arbitration", {"rulings": [
            {"id": "A", "ruling": "dismiss", "reason": "f8 is dead code"}], "summary": "not harmful"})],
    )
    assert env.worker.step() == "#7 reviewed"
    body = env.gh.comments[7][-1]["body"]
    assert body.startswith("**Reviewed**") and "dismissed by the arbiter" in body
    assert "Arbiter dismissed A: f8 is dead code" in body
    assert env.m.store.rec(7)["state"] == "reviewed"


def test_a_re_review_after_the_author_rebased_covers_only_the_delta(env):
    blocked_first_review(env)
    env.world.commit("main", {"lib/util.py": "def util():\n    return 3\n"}, "lib: main moves", base="main")
    env.world.rebase("eng-12-avg", {"src/app.py": APP}, "app: fix f1")
    env.gh.prs[7]["draft"] = False
    env.gh.prs[7]["ready_at"] = "2026-11-02T10:00:00Z"
    env.gh.tick()
    seen = {}

    def reviewer(role, prompt, cwd):
        seen["prompt"] = prompt
        return [done_review()]

    env.agents.scripts.update(reviewer=reviewer)
    assert env.worker.step() == "#7 reviewed"
    prompt = seen["prompt"]
    assert "RE-REVIEW" in prompt and "replayed onto" in prompt
    delta = prompt.split("DELTA (git diff", 1)[1].split("PRECHECK", 1)[0]
    assert "return 1 / 0" in delta and "lib/util.py" not in delta  # main's own change stays out
    assert env.agents.roles()[3:] == ["reviewer"]  # one reviewer, no finding, no oracle


def test_a_review_that_ends_before_ci_is_parked_and_ci_settles_the_same_comment(env):
    env.world.commit("eng-12-avg", {"src/app.py": APP + "# tidy\n"}, "app: tidy")
    env.gh.open_pr(7, "eng-12-avg", "ENG-12 tidy")
    env.gh.check_runs(env.world.head("eng-12-avg"))  # CI started, still running
    assert env.worker.step() == "#7 waiting"
    (comment,) = env.gh.comments[7]
    assert comment["body"].startswith("**Waiting for CI**") and "still running" in comment["body"]
    assert env.m.store.parked(7)["kind"] == "clean" and env.m.store.rec(7) is None
    assert env.worker.step() is None  # still running: nothing to do, nothing reviewed twice
    env.gh.tick()
    assert env.worker.step() is None  # the settle hands it off
    assert env.m.store.parked(7) is None and env.m.store.rec(7)["state"] == "reviewed"
    (comment,) = env.gh.comments[7]
    assert comment["edits"] == 1 and comment["body"].startswith("**Reviewed**") and "CI passed" in comment["body"]


def test_a_parked_review_whose_ci_fails_goes_back_to_its_author(env):
    env.world.commit("eng-12-avg", {"src/app.py": APP + "# tidy\n"}, "app: tidy")
    env.gh.open_pr(7, "eng-12-avg", "ENG-12 tidy")
    env.gh.check_runs(env.world.head("eng-12-avg"))
    assert env.worker.step() == "#7 waiting"
    env.gh.plan = lambda sha, attempt: "failure"
    env.gh.tick()
    env.worker.step()
    (comment,) = env.gh.comments[7]
    assert comment["body"].startswith("**Not merged**") and "CI failed" in comment["body"]
    assert env.gh.prs[7]["draft"] is True and env.m.store.parked(7) is None
    assert env.m.store.baseline(7)["rounds"] == 1
    assert ("ENG-12", "Rework") in env.tracker.moves


def test_an_interrupted_hand_off_is_finished_by_reconcile_without_a_second_comment(env):
    env.agents.scripts.update(reviewer=lambda r, p, c: [done_review()])
    open_pr(env)
    real = env.gh.add_labels
    failed = []

    def flaky(n, labels):
        if labels == [cm.REVIEWED] and not failed:
            failed.append(1)
            raise RuntimeError("GitHub 502")
        real(n, labels)

    env.gh.add_labels = flaky
    assert env.worker.step() == "#7 reviewed"
    rec = env.m.store.rec(7)
    assert rec["state"] == "publishing" and rec["publication"]["done"] == ["comment"]
    env.worker.step()
    rec = env.m.store.rec(7)
    assert rec["state"] == "reviewed" and rec["publication"]["done"] == ["comment", "labels", "ticket"]
    assert len(env.gh.comments[7]) == 1 and cm.REVIEWED in env.gh.prs[7]["labels"]


def test_an_author_push_during_the_review_hands_nothing_off(env):
    pushed = threading.Lock()

    def reviewer(role, prompt, cwd):
        if pushed.acquire(blocking=False):  # the lanes run in parallel: the author pushes once
            env.world.commit("eng-12-avg", {"README.md": "# app v2\n"}, "docs: readme")
        return [done_review()]

    env.agents.scripts.update(reviewer=reviewer)
    open_pr(env)
    assert env.worker.step() == "#7 gone"
    assert env.m.store.rec(7) is None and env.gh.prs[7]["draft"] is False
    assert env.m.store.baseline(7)["outcome"] == "interrupted"
    assert "during the review" in env.gh.comments[7][-1]["body"]


def test_a_failed_run_posts_no_comment_and_alerts(env):
    def reviewer(role, prompt, cwd):
        return []  # never calls submit_review, not even after the reminder

    env.agents.scripts.update(reviewer=reviewer)
    open_pr(env)
    assert env.worker.step() == "#7 error"
    assert env.gh.comments[7] == [] and cm.IN_AUDIT not in env.gh.prs[7]["labels"]
    assert env.notes[-1][0] == "run_error"
    assert env.m.store.skips()[7]["stop"] == "error"


def test_bypass_hands_off_without_a_review_and_first(env):
    open_pr(env)
    env.world.commit("eng-13", {"lib/util.py": "def util():\n    return 9\n"}, "lib: util")
    env.gh.open_pr(8, "eng-13", "ENG-13 util", labels=(cm.BYPASS,), ready_at="2026-11-05T00:00:00Z")
    assert env.worker.step() == "#8 bypassed"
    assert env.m.store.rec(8)["policy"] == "bypass"
    assert env.gh.comments[8][0]["body"].startswith("**Review bypassed**")
    assert env.agents.roles() == []


def test_operator_actions_requeue_and_release(env):
    blocked_first_review(env)
    env.gh.prs[7]["draft"] = False
    assert env.worker.step() is None  # blocked on this head: skipped
    env.m.store.request_action(7, "requeue", actor="maintainer", reason="rerun please")
    env.agents.scripts.update(reviewer=lambda r, p, c: [done_review()])
    assert env.worker.step() == "#7 reviewed"
    env.m.store.claim(9, "review:ghost")
    env.m.store.request_action(9, "release", actor="maintainer", reason="stuck")
    env.worker.step()
    assert env.m.store.holder("pr:9") is None


def test_workers_of_two_processes_never_share_a_claim(env):
    """Regression (sandbox certification): owners were `review:<worker id>`, so worker 1 of a
    second merger process took worker 1's claims of the first for its own."""
    open_pr(env)
    pod_a, pod_b = ReviewWorker(env.m, "1", instance="podA"), ReviewWorker(env.m, "1", instance="podB")
    assert env.m.store.claim(7, pod_a.owner)
    env.agents.scripts.update(reviewer=lambda r, p, c: [done_review()])
    assert pod_b.step() is None  # passed over: the PR is pod A's
    assert env.m.store.holder("pr:7") == pod_a.owner and env.gh.comments[7] == []
    env.m.store.unclaim(7, pod_a.owner)
    assert pod_b.step() == "#7 reviewed"


def test_a_probe_failure_backs_off_before_touching_the_pr(env):
    env.agents.scripts["probe"] = lambda r, p, c: [("submit_review", {"summary": "wrong"})]
    open_pr(env)
    from symphony.merger.review import ProbeFailed

    with pytest.raises(ProbeFailed):
        env.worker.step()
    assert env.gh.comments[7] == [] and cm.IN_AUDIT not in env.gh.prs[7]["labels"]
    assert env.m.store.holder("pr:7") is None and 7 not in env.m.store.skips()


def test_workers_probing_the_same_role_at_once_each_read_their_own_nonce(env):
    """Regression (sandbox certification): two workers probed the reviewer at the same time and
    shared one PROBE.txt, so one read the other's nonce and backed off for nothing."""
    from tests.test_merger_support import probe_script

    from symphony.merger.review import probe

    both = threading.Barrier(2, timeout=10)

    def slow_probe(role, prompt, cwd):
        both.wait()  # both files are written before either agent reads
        return probe_script(role, prompt, cwd)

    env.agents.scripts["probe"] = slow_probe
    policy, team = env.m.policy(), env.m.team.get()
    errors = []

    def run():
        try:
            probe(env.m, policy, team, ["reviewer"])
        except Exception as exc:  # noqa: BLE001 — collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == [] and env.m.store.probe("reviewer")["ok"] is True
    assert os.listdir(os.path.join(env.m.git.root, "probe")) == []


def test_the_report_lists_the_tickets_evidence(env):
    """internals §3: the implementer's proof of work (`meta.evidence[]` of its sessions and the
    ticket's attachments on the tracker) is listed in the merger's report, each once."""
    env.sessions.open("issue-uuid:1", kind="implement", identifier="ENG-12", title="add avg")
    env.sessions.update("issue-uuid:1", evidence=[
        {"url": "https://uploads.linear.app/a/shot.png", "title": "avg in the UI", "kind": "screenshot"}])
    env.sessions.open("other:1", kind="implement", identifier="ENG-99", title="other")
    env.sessions.update("other:1", evidence=[{"url": "https://x/other.log", "title": "not ours", "kind": "log"}])
    env.tracker.attached["ENG-12"] = [
        {"title": "avg in the UI", "url": "https://uploads.linear.app/a/shot.png", "kind": "screenshot"},
        {"title": "test run", "url": "https://uploads.linear.app/a/tests.log", "kind": "log"}]
    env.agents.scripts.update(reviewer=lambda r, p, c: [done_review()])
    open_pr(env)
    assert env.worker.step() == "#7 reviewed"
    body = env.gh.comments[7][-1]["body"]
    section = body.split("**Evidence from the ticket:**", 1)[1].split("\n\n", 2)[1]
    assert section.split("\n") == ["- [avg in the UI](https://uploads.linear.app/a/shot.png) (screenshot)",
                                   "- [test run](https://uploads.linear.app/a/tests.log) (log)"]
