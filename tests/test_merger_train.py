"""Stage 2 end to end: the train rebases, pushes with lease, waits for CI and merges into a real
local remote; batches, red CI, conflicts, crash resume and the train lease."""

from __future__ import annotations

import os
import subprocess
import threading
from types import SimpleNamespace

import pytest
from tests.test_merger_support import (
    ENV,
    FakeGitHub,
    FakeTracker,
    ScriptedAgents,
    World,
    make_merger,
    sh,
)

from symphony.merger import comments as cm
from symphony.merger.review import ReviewWorker
from symphony.merger.train import Train, TrainBusy


class Crash(Exception):
    """A process dying at that exact point."""


@pytest.fixture
def env(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"))
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=tracker, notes=notes)
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, tracker=tracker, notes=notes, train=Train(m))


def handed(env, n: int, files: dict, base: str = "main") -> str:
    """PR n, reviewed and handed to the train on its head (stage 1 is tested on its own)."""
    branch = f"eng-{n}"
    head = env.world.commit(branch, files, f"feat: change {n}", base=base)
    env.gh.open_pr(n, branch, f"ENG-{n} change", author=f"dev{n}")
    env.gh.prs[n]["labels"].append(cm.REVIEWED)
    env.m.store.put_rec({"pr": n, "branch": branch, "author": f"dev{n}", "title": f"ENG-{n} change",
                         "issue": f"ENG-{n}", "head_sha": head, "reviewed_at": env.m.clock(),
                         "ready_at": "2026-11-01T10:00:00Z", "started_at": env.m.clock(),
                         "policy": cm.VERSION, "state": "reviewed",
                         "publication": {"report": "r", "done": ["comment", "labels", "ticket"]}})
    return head


def run_until_idle(train: Train, limit: int = 12) -> list[str]:
    outs = []
    for _ in range(limit):
        out = train.step()
        if out is None:
            return outs
        outs.append(out)
    raise AssertionError(f"the train never idled: {outs}")


def test_the_train_ranks_by_overlap_with_main_then_distance_then_size(env):
    """T2: the PR that shares least with what entered main since its fork goes first; a
    repository command can name what main's changes select instead of the touched packages."""
    handed(env, 7, {"lib/a.py": "A = 1\n"})  # oldest hand-off, but main changed lib since
    handed(env, 8, {"src/b.py": "B = 1\n"})
    handed(env, 9, {"docs/c.md": "one\ntwo\nthree\n"})  # no overlap either, a larger diff
    env.world.commit("main", {"lib/util.py": "def util():\n    return 3\n"}, "lib: main moves", base="main")
    queue = sorted(env.m.store.recs().values(), key=lambda r: r["reviewed_at"])
    env.train.policy = env.m.policy()
    assert [r["pr"] for r in env.train.order(queue)] == [8, 9, 7]
    from symphony.merger.policy import parse

    env.train.policy = parse("---\noverlap: echo src\n---\n")  # main's changes select src
    assert [r["pr"] for r in env.train.order(queue)] == [7, 9, 8]  # then size: 7 is smaller than 9
    env.train.policy = parse("---\noverlap: exit 1\n---\n")  # a failing planner: touched packages
    assert [r["pr"] for r in env.train.order(queue)] == [8, 9, 7]


def test_a_single_pr_is_merged_on_the_head_ci_passed_on(env):
    head = handed(env, 7, {"src/a.py": "A = 1\n"})
    out = env.train.step()
    assert out.startswith("#7 merged")
    assert env.gh.merges == [(7, head, "rebase")]
    assert env.world.show("main", "src/a.py") == "A = 1\n"
    assert env.world.head("eng-7") == ""  # the merged branch is deleted
    assert env.m.store.rec(7) is None and env.m.store.baseline(7) is None
    assert env.gh.comments[7][-1]["body"].startswith("**Merged**")
    assert not {cm.REVIEWED, cm.WAIT_CI, cm.TRAIN_CI} & set(env.gh.prs[7]["labels"])
    assert env.train.step() is None


def test_main_moving_while_ci_runs_rebases_and_pushes_again(env):
    handed(env, 7, {"src/a.py": "A = 1\n"})
    env.world.commit("main", {"lib/x.py": "X = 1\n"}, "lib: x", base="main")
    moved = []

    def pause(seconds):
        if not moved:
            moved.append(1)
            env.world.commit("main", {"lib/y.py": "Y = 1\n"}, "lib: y", base="main")
        env.gh.tick()

    env.m.pause = pause
    assert env.train.step().startswith("#7 merged")
    with open(env.world.pushlog) as fh:
        pushes = [line for line in fh if line.rstrip().endswith("refs/heads/eng-7") and " 0000000000" not in line]
    assert len(pushes) == 3  # the author's, then a rebase per main (the merged branch's deletion aside)
    assert env.world.log() == ["feat: change 7", "lib: y", "lib: x", "init"]  # on top of the latest main
    assert {"lib/x.py", "lib/y.py", "src/a.py"} <= set(sh("git", "ls-tree", "-r", "--name-only", "main",
                                                           cwd=env.world.remote).split())


def test_a_batch_of_three_merges_on_the_top_ci_only(env):
    heads = [handed(env, n, {f"pkg{n}/f.py": f"V = {n}\n"}) for n in (7, 8, 9)]
    out = env.train.step()
    assert out.startswith("batch:") and all(f"#{n} merged" in out for n in (7, 8, 9))
    assert [x[0] for x in env.gh.merges] == [7, 8, 9]
    assert env.m.store.batch() is None and env.m.store.recs() == {}
    assert heads[0] in env.gh.cancelled  # lower members' CI cancelled: the top holds them all
    files = set(sh("git", "ls-tree", "-r", "--name-only", "main", cwd=env.world.remote).split())
    assert {"pkg7/f.py", "pkg8/f.py", "pkg9/f.py"} <= files
    assert "CI passed in a batch with #8 #9" in env.gh.comments[7][-1]["body"]


def test_a_batch_on_a_protected_main_merges_each_member_once_its_required_checks_pass(env):
    """Regression (sandbox certification, main requiring the `test` check for admins too): the
    second member, pushed onto the first's merge, had its run cancelled, so GitHub refused it
    ('Required status check "test" is cancelled') and the batch fell apart into extra pushes."""
    env.gh.required_checks = True
    handed(env, 7, {"pkg7/f.py": "V = 7\n"})
    handed(env, 8, {"pkg8/f.py": "V = 8\n"})
    out = env.train.step()
    assert out.startswith("batch:") and "#7 merged" in out and "#8 merged" in out
    pushed = env.gh.merges[1][1]
    assert [x[0] for x in env.gh.merges] == [7, 8] and env.gh.runs[pushed]["conclusion"] == "success"
    assert "back to reviewed" not in out and env.m.store.recs() == {}


def test_a_red_batch_reruns_once_then_bisects_until_the_culprit_is_alone(env):
    def plan(sha, attempt):
        tree = sh("git", "ls-tree", "-r", "--name-only", sha, cwd=env.world.remote).split()
        return "failure" if "bad/bad.py" in tree else "success"

    env.gh.plan = plan
    handed(env, 7, {"good7/f.py": "A = 1\n"})
    handed(env, 8, {"bad/bad.py": "B = 1\n"})
    handed(env, 9, {"good9/f.py": "C = 1\n"})
    outs = run_until_idle(env.train)
    assert sorted(n for n, _, _ in env.gh.merges) == [7, 9]
    assert len(env.gh.reruns) == 2  # one rerun of the failed jobs per red batch, no more
    assert [e for e, *_ in env.notes].count("batch_split") == 2
    assert env.gh.prs[8]["draft"] is True and env.gh.prs[8]["state"] == "open"
    assert ("ENG-8", "Rework") in env.tracker.moves
    assert "CI failed" in env.gh.comments[8][-1]["body"]
    assert env.m.store.recs() == {} and env.m.store.caps() == {}
    assert any(o.startswith("batch:") for o in outs)


def test_a_conflict_with_main_is_resolved_by_agents_and_merged(env):
    handed(env, 7, {"src/app.py": "def run():\n    return 100\n"})
    env.world.commit("main", {"src/app.py": "def run():\n    return 10\n"}, "app: ten", base="main")
    out = env.train.step()
    assert "resolve" in out and env.m.store.rec(7)["state"] == "resolve"
    assert env.m.store.rec(7)["conflict"]["files"] == ["src/app.py"]

    def developer(role, prompt, cwd):
        with open(os.path.join(cwd, "src/app.py"), "w") as fh:
            fh.write("def run():\n    return 10 + 90\n")
        sh("git", "add", "src/app.py", cwd=cwd)
        subprocess.run(["git", "rebase", "--continue"], cwd=cwd, env={**ENV, "GIT_EDITOR": "true"},
                       capture_output=True, check=True)
        # Regression (sandbox certification): running the tests leaves untracked caches, which
        # made a finished rebase look unfinished.
        os.makedirs(os.path.join(cwd, ".pytest_cache"), exist_ok=True)
        with open(os.path.join(cwd, ".pytest_cache", "README.md"), "w") as fh:
            fh.write("cache\n")
        return [("submit_resolution", {"resolved": True, "files": ["src/app.py"],
                                       "note": "kept main's 10 and the PR's +90", "checks": "python -c ok"})]

    env.agents.scripts["developer"] = developer
    seen = {}
    env.agents.scripts["resolve_review"] = lambda r, p, c: seen.setdefault("p", p) and [
        ("submit_review", {"approved": True, "issues": ""})]
    worker = ReviewWorker(env.m, "1")
    assert worker.step().startswith("#7 resolved")
    rec = env.m.store.rec(7)
    assert rec["state"] == "reviewed" and rec["head_sha"] == env.world.head("eng-7")
    assert "range_diff" in seen["p"] and "pr_change" in seen["p"]
    assert env.gh.comments[7][-1]["body"].startswith("**Rebased onto main**")
    assert [r for r in env.agents.roles()] == ["developer", "resolve_review"]
    assert env.train.step().startswith("#7 merged")
    assert env.world.show("main", "src/app.py") == "def run():\n    return 10 + 90\n"


def test_an_unapproved_resolution_sends_the_pr_back(env):
    handed(env, 7, {"src/app.py": "def run():\n    return 100\n"})
    env.world.commit("main", {"src/app.py": "def run():\n    return 10\n"}, "app: ten", base="main")
    env.train.step()
    env.agents.scripts["developer"] = lambda r, p, c: [("submit_resolution", {
        "resolved": False, "files": [], "note": "both sides change the same value", "checks": ""})]
    ReviewWorker(env.m, "1").step()
    assert env.m.store.rec(7)["state"] == "finalizing"
    assert env.train.step().startswith("#7 draft")
    assert env.gh.prs[7]["draft"] is True and ("ENG-7", "Rework") in env.tracker.moves


def test_tools_down_during_a_resolution_back_off_without_spending_its_tries(env):
    """Regression (sandbox certification): the developer's probe failed (omp refused its tool
    list) inside the resolution, each failure counted as a try, and two of them, two seconds
    apart, sent a resolvable PR back to its author."""
    from symphony.merger.review import ProbeFailed

    handed(env, 7, {"src/app.py": "def run():\n    return 100\n"})
    env.world.commit("main", {"src/app.py": "def run():\n    return 10\n"}, "app: ten", base="main")
    env.train.step()
    env.agents.scripts["probe"] = lambda r, p, c: [("submit_review", {"summary": "tools down"})]
    worker = ReviewWorker(env.m, "1")
    for _ in range(3):
        with pytest.raises(ProbeFailed):
            worker.step()
    rec = env.m.store.rec(7)
    assert rec["state"] == "resolve" and rec["conflict"]["tries"] == 0
    assert env.m.store.holder("pr:7") is None and env.m.store.holder("fixslot:1") is None
    assert env.gh.prs[7]["draft"] is False and env.gh.comments[7] == []


def test_a_crash_after_the_push_resumes_on_the_pushed_head(env):
    handed(env, 7, {"src/a.py": "A = 1\n"})
    env.world.commit("main", {"lib/x.py": "X = 1\n"}, "lib: x", base="main")
    real = env.m.git.push_lease

    def push_then_die(*args):
        real(*args)
        raise Crash()

    env.m.git.push_lease = push_then_die
    with pytest.raises(Crash):
        env.train.step()
    rec = env.m.store.rec(7)
    pushed = env.world.head("eng-7")
    assert rec["state"] == "train" and rec["pushing"] == pushed and rec["head_sha"] != pushed
    assert env.m.store.holder("train") is None  # the lease went with the step
    env.m.git.push_lease = real
    assert Train(env.m).step().startswith("#7 merged")
    assert env.gh.merges == [(7, pushed, "rebase")]


def test_a_crash_while_finalizing_applies_only_what_is_left(env):
    handed(env, 7, {"src/a.py": "A = 1\n"})
    real = env.gh.comment
    died = []

    def comment(n, body):
        if body.startswith("**Merged**") and not died:
            died.append(1)
            raise Crash()
        return real(n, body)

    env.gh.comment = comment
    with pytest.raises(Crash):
        env.train.step()
    rec = env.m.store.rec(7)
    assert rec["state"] == "finalizing" and rec["outcome"]["done"] == ["draft", "labels"]
    removes = env.gh.calls.count("remove_label")
    assert Train(env.m).step().startswith("#7 merged")
    assert [c["body"][:10] for c in env.gh.comments[7]] == ["**Merged**"]
    assert env.gh.calls.count("remove_label") == removes  # the labels step is not repeated
    assert env.m.store.rec(7) is None


def test_a_crash_mid_batch_is_resumed_before_anything_else(env):
    handed(env, 7, {"pkg7/f.py": "V = 7\n"})
    handed(env, 8, {"pkg8/f.py": "V = 8\n"})
    real = env.gh.merge

    def merge(n, sha, method):
        result = real(n, sha, method)
        if n == 8:
            raise Crash()  # the merge went through, its answer is lost with the process
        return result

    env.gh.merge = merge
    with pytest.raises(Crash):
        env.train.step()
    batch = env.m.store.batch()
    assert [x.get("merged", False) for x in batch["members"]] == [True, False]
    assert env.m.store.rec(8)["state"] == "train"
    handed(env, 9, {"pkg9/f.py": "V = 9\n"})  # newer work waits behind the stopped batch
    env.gh.merge = real
    out = Train(env.m).step()
    assert out.startswith("batch:") and "#8 merged" in out and "#9" not in out
    assert env.m.store.batch() is None and env.m.store.rec(8) is None
    assert [c["body"][:10] for c in env.gh.comments[8]] == ["**Merged**"]
    assert Train(env.m).step().startswith("#9 merged")


def test_the_lease_keeps_a_second_train_loop_out(env):
    handed(env, 7, {"src/a.py": "A = 1\n"})
    gate = threading.Event()
    waiting = threading.Event()

    def pause(seconds):
        waiting.set()
        gate.wait(10)
        env.gh.tick()

    env.m.pause = pause
    first = Train(env.m, owner="train:a")
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("out", first.step()))
    t.start()
    assert waiting.wait(10)
    with pytest.raises(TrainBusy, match="train:a"):
        Train(env.m, owner="train:b").step()
    gate.set()
    t.join(20)
    assert result["out"].startswith("#7 merged")
    assert Train(env.m, owner="train:b").step() is None


def test_an_author_push_after_the_hand_off_drops_the_pr_from_the_train(env):
    handed(env, 7, {"src/a.py": "A = 1\n"})
    env.world.commit("eng-7", {"src/a.py": "A = 2\n"}, "feat: more")
    out = env.train.step()
    assert out.startswith("#7 gone") and "author pushed" in out
    assert env.m.store.rec(7) is None and env.gh.merges == []
    assert env.gh.comments[7] == []  # gone: no comment, its next review covers the push


def test_merged_outside_train_moves_ticket_to_done_via_take(env):
    """A PR merged on GitHub while in reviewed state (not yet in train) moves
    the ticket to Done."""
    handed(env, 7, {"src/a.py": "A = 1\n"})
    # Simulate human merging on GitHub
    env.gh.prs[7]["state"] = "merged"
    out = env.train.step()
    assert "merged on GitHub outside the merge train" in out
    assert ("ENG-7", "Done") in env.tracker.moves
    comment_texts = [c for _, c in env.tracker.comments if _ == "ENG-7"]
    assert any("merged on GitHub outside the merge train" in c for c in comment_texts)


def test_merged_outside_train_parked_moves_ticket_to_done(env):
    """A parked PR that is merged on GitHub moves the ticket to Done."""
    from symphony.merger.review import settle_parked

    branch = "eng-7"
    head = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 change", author="dev7")
    env.m.store.park({
        "pr": 7, "branch": branch, "author": "dev7", "title": "ENG-7 change",
        "issue": "ENG-7", "head_sha": head, "ready_at": "2026-11-01T10:00:00Z",
        "started_at": env.m.clock(), "parked_at": env.m.clock(), "findings": [],
        "rounds": 0, "kind": "clean", "outcome": "", "body": "", "drafted": False,
        "comment": "",
    })
    # Mark PR as merged
    env.gh.prs[7]["state"] = "merged"
    policy = env.m.policy()
    settle_parked(env.m, policy, "worker-1")
    assert ("ENG-7", "Done") in env.tracker.moves
    comment_texts = [c for _, c in env.tracker.comments if _ == "ENG-7"]
    assert any("merged on GitHub outside the merge train" in c for c in comment_texts)


def test_learnings_pr_sent_back_does_not_move_any_ticket(env):
    """Regression: a learnings PR names a ticket in its title but must not attribute
    to it — sending it back must not move the source ticket."""
    branch = "symphony/learnings/ENG-99"
    head = env.world.commit(branch, {"AGENTS.md": "# learnings\n"}, "learnings: ENG-99")
    env.gh.open_pr(10, branch, "learnings: ENG-99", author="dev1")
    env.gh.prs[10]["labels"].append(cm.REVIEWED)
    env.m.store.put_rec({
        "pr": 10, "branch": branch, "author": "dev1", "title": "learnings: ENG-99",
        "issue": None, "head_sha": head, "reviewed_at": env.m.clock(),
        "ready_at": "2026-11-01T10:00:00Z", "started_at": env.m.clock(),
        "policy": cm.VERSION, "state": "reviewed",
        "publication": {"report": "r", "done": ["comment", "labels", "ticket"]},
    })
    # Verify ticket_of returns None for learnings branch
    pr = env.gh.pr(10)
    assert env.m.ticket_of(pr) is None
    # Step puts it through the train — it merges fine
    result = env.train.step()
    assert result is not None
    # No ticket was moved to Rework
    assert all(ident != "ENG-99" for ident, _ in env.tracker.moves)
