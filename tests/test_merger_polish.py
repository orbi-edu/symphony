"""Polish rounds: an agent-authored PR with actionable nits is sent back once before
going on to CI / approval / hand-off, so the agent can fix them autonomously."""

from __future__ import annotations

import os
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

from symphony.merger.review import ReviewWorker
from symphony.sessions import SessionStore

APP = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(12))

POLICY_POLISH = """---
lanes:
  - key: correctness
    audits: correctness and regressions
  - key: security
    audits: security and the ticket's acceptance criteria
polish_rounds: 1
---
Keep functions small.
"""


@pytest.fixture
def env(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"), policy=POLICY_POLISH)
    world.commit("main", {"src/app.py": APP}, "app: many functions", base="main")
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=tracker, notes=notes)
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, tracker=tracker, notes=notes,
                           worker=ReviewWorker(m, "1"), sessions=SessionStore(redis_client, NS))


def open_pr(env, n=7, branch="eng-12-avg", files=None, title="ENG-12 add avg",
            *, author="symphony-app[bot]", author_type="Bot"):
    env.world.commit(branch, files or {"src/app.py": APP.replace("return 1\n", "return 1 / 0\n", 1)},
                     "app: change f1")
    env.gh.open_pr(n, branch, title, author=author, author_type=author_type)
    env.gh.tick()


def lane(prompt: str) -> str:
    return "correctness" if "audits: correctness and regressions" in prompt else "security"


def nit_review(env):
    """A bot PR with nits (no blockers): the reviewer reports issues but the oracle does not
    grade them blocking, so they become nits."""
    def reviewer(role, prompt, cwd):
        if lane(prompt) == "correctness":
            return [finding("src/app.py:6", "f1 divides by zero", severity="nit", cls="nit"),
                    done_review()]
        return [finding("src/app.py:5", "f1 changes its callers' result", cls="regression", severity="nit"),
                done_review()]

    def oracle(role, prompt, cwd):
        out = []
        for fid in ids_in(prompt):
            out.append(verdict(fid, "confirmed", severity="nit", cls="nit"))
        return [*out, done_verify()]

    env.agents.scripts.update(reviewer=reviewer, oracle=oracle)
    open_pr(env)
    return env.worker.step()


def test_agent_pr_with_actionable_nit_is_sent_back_once_then_handed_on(env):
    """An agent PR gets a polish round for its nits, then the re-review hands it off."""
    result = nit_review(env)
    assert result == "#7 polish"
    pr = env.gh.prs[7]
    assert pr["draft"] is True
    # The polish comment is posted
    comments = env.gh.comments[7]
    polish_comment = next(c for c in comments if "automated author can fix" in c["body"])
    assert "src/app.py:" in polish_comment["body"]
    # The ticket moves to rework
    assert ("ENG-12", "Rework") in env.tracker.moves
    # The ticket comment mentions the nits
    ticket_comment = next(body for (ident, body) in env.tracker.comments if ident == "ENG-12")
    assert "polish" in ticket_comment.lower()
    # Notification sent
    assert env.notes[-1][0] == "back_to_author"
    # Polish round count incremented, rejection rounds unchanged
    assert env.m.store.polish_rounds(7) == 1
    baseline = env.m.store.baseline(7)
    assert baseline is not None
    assert baseline["rounds"] == 0  # no rejection round used
    assert baseline["outcome"] == "polish"

    # Now the author pushes a fix and the re-review hands it off
    env.world.commit("eng-12-avg", {"src/app.py": APP + "# fixed\n"}, "app: fix nits")
    env.gh.prs[7]["draft"] = False
    env.gh.tick()
    env.agents.scripts.update(reviewer=lambda r, p, c: [done_review()])
    result2 = env.worker.step()
    assert result2 == "#7 reviewed"
    assert env.m.store.rec(7)["state"] == "reviewed"


def test_human_pr_is_never_polished(env):
    """A human-authored PR with nits is never sent back for a polish round."""
    def reviewer(role, prompt, cwd):
        return [finding("src/app.py:6", "f1 divides by zero", severity="nit", cls="nit"),
                done_review()]

    def oracle(role, prompt, cwd):
        out = []
        for fid in ids_in(prompt):
            out.append(verdict(fid, "confirmed", severity="nit", cls="nit"))
        return [*out, done_verify()]

    env.agents.scripts.update(reviewer=reviewer, oracle=oracle)
    # Open a human-authored PR (default User type)
    open_pr(env, author="alice", author_type="User")
    result = env.worker.step()
    # Should hand off (reviewed), not polished
    assert result == "#7 reviewed"
    assert env.m.store.polish_rounds(7) == 0


def test_refuted_deferred_and_dismissed_nits_do_not_trigger_polish(env):
    """Only confirmed, partial or not-verified nits trigger a polish round."""
    def reviewer(role, prompt, cwd):
        return [finding("src/app.py:6", "f1 divides by zero", severity="nit", cls="nit"),
                done_review()]

    def oracle(role, prompt, cwd):
        out = []
        for fid in ids_in(prompt):
            out.append(verdict(fid, "refuted", severity="nit", cls="nit"))
        return [*out, done_verify()]

    env.agents.scripts.update(reviewer=reviewer, oracle=oracle)
    open_pr(env)
    result = env.worker.step()
    # Refuted nits should not trigger polish
    assert result == "#7 reviewed"
    assert env.m.store.polish_rounds(7) == 0


def test_polish_round_does_not_change_rejection_rounds_or_escalation(env):
    """A polish round never increments rejection rounds, so a later real block is still a
    first rejection and escalation still needs two blocks."""
    # First: the agent PR gets polished (nits only, no blockers)
    nit_review(env)
    assert env.m.store.baseline(7)["rounds"] == 0

    # The author pushes with a different change that has a new blocker
    env.world.commit("eng-12-avg",
                     {"src/app.py": APP.replace("return 3\n", "return 3 / 0\n", 1)},
                     "app: push fix with bug")
    env.gh.prs[7]["draft"] = False
    env.gh.tick()

    def reviewer2(role, prompt, cwd):
        return [finding("src/app.py:14", "f3 divides by zero"), done_review()]

    env.agents.scripts.update(
        reviewer=reviewer2,
        oracle=lambda r, p, c: [verdict("A"), done_verify()],
    )

    result = env.worker.step()
    # This should be a first rejection (blocked), not escalated
    assert result == "#7 blocked"
    baseline = env.m.store.baseline(7)
    assert baseline["rounds"] == 1  # first rejection
    assert baseline["outcome"] == "blocked"


def test_polish_rounds_zero_keeps_today_behaviour(env):
    """With polish_rounds: 0 (or absent), agent PRs with nits are handed off as today."""
    # Override policy to have polish_rounds=0
    env.world.commit("main", {".symphony/merger.md": """---
lanes:
  - key: correctness
    audits: correctness and regressions
  - key: security
    audits: security and the ticket's acceptance criteria
polish_rounds: 0
---
Keep functions small.
"""}, "policy: no polish")
    env.m.git.fetch()

    def reviewer(role, prompt, cwd):
        return [finding("src/app.py:6", "f1 divides by zero", severity="nit", cls="nit"),
                done_review()]

    def oracle(role, prompt, cwd):
        out = []
        for fid in ids_in(prompt):
            out.append(verdict(fid, "confirmed", severity="nit", cls="nit"))
        return [*out, done_verify()]

    env.agents.scripts.update(reviewer=reviewer, oracle=oracle)
    open_pr(env)
    result = env.worker.step()
    assert result == "#7 reviewed"
    assert env.m.store.polish_rounds(7) == 0


def test_ticket_moves_to_rework_with_nit_list(env):
    """The ticket comment on a polish round lists the actionable nits with locations."""
    nit_review(env)
    # Find the ticket comment for ENG-12
    ticket_comments = [(ident, body) for ident, body in env.tracker.comments if ident == "ENG-12"]
    assert len(ticket_comments) >= 1
    body = ticket_comments[-1][1]
    assert "automated author can fix" in body
    assert "src/app.py:" in body
    assert ("ENG-12", "Rework") in env.tracker.moves
