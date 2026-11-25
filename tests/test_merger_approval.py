"""Tests for the human approval gate (require_approval) and the preview announcement."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from unittest import mock

import pytest
from tests.test_merger_support import (
    FakeGitHub,
    FakeTracker,
    ScriptedAgents,
    World,
    make_merger,
)

from symphony import preview_links
from symphony.merger import comments as cm
from symphony.merger.approval import (
    button_id,
    check_preview,
    check_previews_for_open_prs,
    is_approved,
)
from symphony.merger.github import GitHubError
from symphony.merger.policy import parse
from symphony.merger.train import Train

NS = "approval"

APPROVAL_POLICY = """\
---
require_approval: true
merge_method: squash
---
Review rules.
"""

PREVIEW_POLICY = """\
---
preview:
  artifact: ios-simulator-preview
  platform: ios
---
Review rules.
"""


@pytest.fixture
def env(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"))
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=tracker, notes=notes)
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, tracker=tracker,
                           notes=notes, train=Train(m))


@pytest.fixture
def env_approval(redis_client, tmp_path):
    """An env where the policy on main requires approval."""
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"))
    # Write the policy to the remote's main before any PR branch is forked.
    world.commit("main", {".symphony/merger.md": APPROVAL_POLICY}, "add policy")
    gh = FakeGitHub(world)
    agents = ScriptedAgents()
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, agents, tracker=tracker, notes=notes)
    return SimpleNamespace(world=world, gh=gh, agents=agents, m=m, tracker=tracker,
                           notes=notes, train=Train(m))


def handed(env, n: int, files: dict, base: str = "main") -> str:
    """PR n, reviewed and handed to the train on its head."""
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


# ---------------------------------------------------------------------------
# Approval gate: blocks without approval
# ---------------------------------------------------------------------------


def test_approval_gate_blocks_without_approval(env_approval):
    """A reviewed PR with require_approval stays in reviewed when no approval exists."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    # No reviews on the PR — the train should send it back.
    result = env_approval.train.step()
    assert result is not None
    assert "waiting for human approval" in result
    # PR should have the wait-approval label.
    assert cm.WAIT_APPROVAL in env_approval.gh.prs[7]["labels"]
    # The record should be back to reviewed state.
    rec = env_approval.m.store.rec(7)
    assert rec is not None
    assert rec["state"] == "reviewed"


def test_approval_gate_passes_with_approval(env_approval):
    """A reviewed PR with an APPROVED review proceeds to the train."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    # Add an APPROVED review.
    env_approval.gh.pr_reviews[7] = [
        {"user": "reviewer1", "state": "APPROVED", "submitted_at": "2026-11-01T12:00:00Z"},
    ]
    result = env_approval.train.step()
    assert result is not None
    assert "merged" in result.lower()
    assert env_approval.m.store.rec(7) is None  # merged and gone


def test_approval_gate_resets_on_changes_requested(env_approval):
    """An APPROVED then CHANGES_REQUESTED blocks the PR again."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    # First: APPROVED, then CHANGES_REQUESTED by the same reviewer.
    env_approval.gh.pr_reviews[7] = [
        {"user": "reviewer1", "state": "APPROVED", "submitted_at": "2026-11-01T12:00:00Z"},
        {"user": "reviewer1", "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-01T13:00:00Z"},
    ]
    result = env_approval.train.step()
    assert result is not None
    assert "waiting for human approval" in result
    rec = env_approval.m.store.rec(7)
    assert rec is not None
    assert rec["state"] == "reviewed"


def test_approval_dismissed_after_handoff_sends_back(env_approval):
    """A second PR where approval is dismissed is sent back to reviewed."""
    # PR 7 is approved and merged.
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.gh.pr_reviews[7] = [
        {"user": "reviewer1", "state": "APPROVED", "submitted_at": "2026-11-01T12:00:00Z"},
    ]
    result = env_approval.train.step()
    assert "merged" in result.lower()
    # PR 8: approved then dismissed.
    handed(env_approval, 8, {"src/b.py": "B = 2\n"})
    env_approval.gh.pr_reviews[8] = [
        {"user": "reviewer1", "state": "APPROVED", "submitted_at": "2026-11-01T12:00:00Z"},
        {"user": "reviewer1", "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-01T14:00:00Z"},
    ]
    result = env_approval.train.step()
    assert result is not None
    assert "waiting for human approval" in result
    rec = env_approval.m.store.rec(8)
    assert rec is not None
    assert rec["state"] == "reviewed"


def test_new_review_moves_it_on(env_approval):
    """A PR blocked by approval proceeds once a new APPROVED review arrives."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    # No reviews — blocked.
    result = env_approval.train.step()
    assert "waiting for human approval" in result
    # Add an APPROVED review.
    env_approval.gh.pr_reviews[7] = [
        {"user": "reviewer1", "state": "APPROVED", "submitted_at": "2026-11-01T15:00:00Z"},
    ]
    result = env_approval.train.step()
    assert result is not None
    assert "merged" in result.lower()


def test_no_approval_required_proceeds_normally(env):
    """Without require_approval, the train works as before."""
    handed(env, 7, {"src/a.py": "A = 1\n"})
    result = env.train.step()
    assert result is not None
    assert "merged" in result.lower()


def test_approval_notifies_once_per_head(env_approval):
    """The needs_approval notification fires once per (PR, head sha)."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    # First attempt: should notify.
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1
    # Second attempt: no duplicate notification.
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1


def test_multiple_reviewers_all_must_approve(env_approval):
    """With two reviewers, one APPROVED and one CHANGES_REQUESTED blocks."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.gh.pr_reviews[7] = [
        {"user": "reviewer1", "state": "APPROVED", "submitted_at": "2026-11-01T12:00:00Z"},
        {"user": "reviewer2", "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-01T13:00:00Z"},
    ]
    result = env_approval.train.step()
    assert "waiting for human approval" in result


def test_is_approved_with_no_reviews(env):
    """No reviews means not approved."""
    env.gh.open_pr(7, "eng-7", "ENG-7 test")
    assert not is_approved(env.m, 7)


def test_is_approved_with_only_commented(env):
    """COMMENTED reviews are not decision states, so the PR is not approved."""
    env.gh.open_pr(7, "eng-7", "ENG-7 test")
    env.gh.pr_reviews[7] = [
        {"user": "reviewer1", "state": "COMMENTED", "submitted_at": "2026-11-01T12:00:00Z"},
    ]
    assert not is_approved(env.m, 7)


# ---------------------------------------------------------------------------
# Preview announcements
# ---------------------------------------------------------------------------


def test_preview_announced_once_per_sha(env):
    """A preview announcement posts once per (PR, head sha)."""
    branch = "eng-7"
    head = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    env.gh.tick()  # syncs the push and completes CI
    policy = parse(PREVIEW_POLICY)
    # tick created and completed a run for head; add the artifact to it.
    run = env.gh.runs[head]
    env.gh.artifacts[run["id"]] = [{"name": "ios-simulator-preview", "expired": False}]
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        pr = env.gh.pr(7)
        check_preview(env.m, policy, pr)
        # Second call: dedup.
        check_preview(env.m, policy, pr)
    assert len(posted) == 1
    assert posted[0]["button_action_id"] == f"preview:acme/app:7:{head[:12]}"
    assert posted[0]["button_label"] == "Open in simulator"


def test_preview_new_sha_gets_new_announcement(env):
    """A new sha gets a new announcement."""
    branch = "eng-7"
    head1 = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    env.gh.tick()  # completes CI for head1
    policy = parse(PREVIEW_POLICY)
    # The tick already created and completed a run for head1. Add the artifact.
    run1 = env.gh.runs[head1]
    env.gh.artifacts[run1["id"]] = [{"name": "ios-simulator-preview", "expired": False}]
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        pr = env.gh.pr(7)
        check_preview(env.m, policy, pr)
    assert len(posted) == 1
    # New commit — push creates a new run, tick completes it.
    head2 = env.world.commit(branch, {"src/a.py": "A = 2\n"}, "feat: a v2")
    env.gh.tick()  # syncs the push and completes CI for head2
    run2 = env.gh.runs[head2]
    env.gh.artifacts[run2["id"]] = [{"name": "ios-simulator-preview", "expired": False}]
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        pr = env.gh.pr(7)
        check_preview(env.m, policy, pr)
    assert len(posted) == 2
    assert posted[1]["button_action_id"] == f"preview:acme/app:7:{head2[:12]}"


LINK_SECRET = "test-link-secret-0123456789"


@pytest.fixture
def links_on(monkeypatch):
    monkeypatch.setenv("SYMPHONY_PUBLIC_URL", "https://symphony.example.com")
    monkeypatch.setenv(preview_links.SECRET_ENV, LINK_SECRET)


def _build(env, branch: str, files: dict, message: str) -> str:
    """A push to `branch` whose CI run carries the preview artifact."""
    head = env.world.commit(branch, files, message)
    env.gh.tick()
    env.gh.artifacts[env.gh.runs[head]["id"]] = [{"name": "ios-simulator-preview",
                                                  "expired": False}]
    return head


def test_the_pr_comment_lists_each_builds_link_newest_first_edited_in_place(env, links_on):
    """One Preview comment per PR: a new head with a build adds its signed link on top, the
    older builds stay (an older sha keeps opening that version)."""
    env.gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    head1 = _build(env, "eng-7", {"src/a.py": "A = 1\n"}, "feat: a")
    policy = parse(PREVIEW_POLICY)
    with mock.patch("symphony.notify.post_with_button", return_value=True):
        check_preview(env.m, policy, env.gh.pr(7))
        [comment] = env.gh.comments[7]
        assert comment["body"].startswith("**Preview**\n\n**symphony-merger**")
        assert cm.OURS.match(comment["body"])
        link1 = preview_links.url("https://symphony.example.com", LINK_SECRET, "acme/app", 7,
                                  head1[:12])
        assert link1.startswith("https://symphony.example.com/v1/previews/open?repo=acme%2Fapp"
                                f"&pr=7&sha={head1[:12]}&sig=")
        assert f"- `{head1[:12]}` (newest): [Open in Seiji]({link1})" in comment["body"]
        check_preview(env.m, policy, env.gh.pr(7))  # the same head: nothing to change
        assert comment["edits"] == 0

        head2 = _build(env, "eng-7", {"src/a.py": "A = 2\n"}, "feat: a v2")
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(env.gh.comments[7]) == 1 and comment["edits"] == 1
    body = comment["body"]
    assert body.index(head2[:12]) < body.index(head1[:12])
    assert f"- `{head2[:12]}` (newest): [Open in Seiji](" in body
    assert f"- `{head1[:12]}`: [Open in Seiji]({link1})" in body
    shas = [b["sha12"] for b in preview_links.builds(env.m.store.client, env.m.store.ns,
                                                       "acme/app", 7)]
    assert shas == [head2[:12], head1[:12]]


def test_the_pr_comment_keeps_the_last_five_builds(env, links_on):
    env.gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    policy = parse(PREVIEW_POLICY)
    heads = []
    with mock.patch("symphony.notify.post_with_button", return_value=True):
        for i in range(7):
            heads.append(_build(env, "eng-7", {"src/a.py": f"A = {i}\n"}, f"feat: a {i}"))
            check_preview(env.m, policy, env.gh.pr(7))
    [comment] = env.gh.comments[7]
    assert [h[:12] for h in reversed(heads[2:])] == re.findall(r"`([0-9a-f]{12})`", comment["body"])


def test_a_deleted_preview_comment_is_posted_again(env, links_on):
    env.gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    policy = parse(PREVIEW_POLICY)
    with mock.patch("symphony.notify.post_with_button", return_value=True):
        _build(env, "eng-7", {"src/a.py": "A = 1\n"}, "feat: a")
        check_preview(env.m, policy, env.gh.pr(7))
        head2 = _build(env, "eng-7", {"src/a.py": "A = 2\n"}, "feat: a v2")
        env.gh.fail_next["edit_comment"] = GitHubError("Not Found", 404)
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(env.gh.comments[7]) == 2
    assert f"`{head2[:12]}` (newest)" in env.gh.comments[7][1]["body"]


def test_a_failed_comment_is_tried_again_and_slack_still_posts_once(env, links_on):
    env.gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    policy = parse(PREVIEW_POLICY)
    posted = []
    with mock.patch("symphony.notify.post_with_button",
                    side_effect=lambda *a, **kw: posted.append(kw) or True):
        head = _build(env, "eng-7", {"src/a.py": "A = 1\n"}, "feat: a")
        env.gh.fail_next["comment"] = GitHubError("Server Error", 502)
        check_preview(env.m, policy, env.gh.pr(7))
        assert env.gh.comments[7] == [] and len(posted) == 1
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(posted) == 1
    assert f"`{head[:12]}` (newest)" in env.gh.comments[7][0]["body"]


def test_without_the_link_secret_the_link_is_unsigned(env, monkeypatch):
    """Behind Cloudflare Access the person is the credential: no secret to provision."""
    monkeypatch.setenv("SYMPHONY_PUBLIC_URL", "https://symphony.example.com")
    monkeypatch.delenv(preview_links.SECRET_ENV, raising=False)
    env.gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    with mock.patch("symphony.notify.post_with_button", return_value=True):
        head = _build(env, "eng-7", {"src/a.py": "A = 1\n"}, "feat: a")
        check_preview(env.m, parse(PREVIEW_POLICY), env.gh.pr(7))
    [comment] = env.gh.comments[7]
    assert ("(https://symphony.example.com/v1/previews/open?repo=acme%2Fapp&pr=7"
            f"&sha={head[:12]})") in comment["body"]


def test_no_pr_comment_without_the_public_url(env, monkeypatch):
    monkeypatch.delenv("SYMPHONY_PUBLIC_URL", raising=False)
    monkeypatch.setenv(preview_links.SECRET_ENV, LINK_SECRET)
    env.gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    posted = []
    with mock.patch("symphony.notify.post_with_button",
                    side_effect=lambda *a, **kw: posted.append(kw) or True):
        _build(env, "eng-7", {"src/a.py": "A = 1\n"}, "feat: a")
        check_preview(env.m, parse(PREVIEW_POLICY), env.gh.pr(7))
    assert len(posted) == 1 and env.gh.comments[7] == []


def test_no_announcement_without_artifact(env):
    """No artifact means no announcement."""
    branch = "eng-7"
    head = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    env.gh.tick()
    policy = parse(PREVIEW_POLICY)
    run_id = 5001
    env.gh.runs[head] = {
        "id": run_id, "name": "CI", "sha": head, "status": "completed",
        "conclusion": "success", "attempt": 1, "branch": branch,
        "event": "pull_request", "url": f"https://ci/{head[:7]}", "title": head[:7],
    }
    env.gh.artifacts[run_id] = []
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(posted) == 0


def test_no_announcement_for_stale_artifact(env):
    """An expired artifact is ignored."""
    branch = "eng-7"
    head = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    env.gh.tick()
    policy = parse(PREVIEW_POLICY)
    run_id = 5001
    env.gh.runs[head] = {
        "id": run_id, "name": "CI", "sha": head, "status": "completed",
        "conclusion": "success", "attempt": 1, "branch": branch,
        "event": "pull_request", "url": f"https://ci/{head[:7]}", "title": head[:7],
    }
    env.gh.artifacts[run_id] = [{"name": "ios-simulator-preview", "expired": True}]
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(posted) == 0


def test_no_announcement_for_draft_pr(env):
    """A draft PR gets no preview announcement."""
    branch = "eng-7"
    head = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice", draft=True)
    env.gh.tick()
    policy = parse(PREVIEW_POLICY)
    run_id = 5001
    env.gh.runs[head] = {
        "id": run_id, "name": "CI", "sha": head, "status": "completed",
        "conclusion": "success", "attempt": 1, "branch": branch,
        "event": "pull_request", "url": f"https://ci/{head[:7]}", "title": head[:7],
    }
    env.gh.artifacts[run_id] = [{"name": "ios-simulator-preview", "expired": False}]
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(posted) == 0


def test_no_announcement_without_preview_policy(env):
    """Without preview in policy, no announcement."""
    branch = "eng-7"
    env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    env.gh.tick()
    policy = parse("---\n---\nRules.")
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        check_preview(env.m, policy, env.gh.pr(7))
    assert len(posted) == 0


# ---------------------------------------------------------------------------
# Reviewer fallback (contract §4)
# ---------------------------------------------------------------------------


def test_reviewer_fallback_to_author(env):
    """With no mapped reviewer, falls back to the PR author."""
    from symphony.merger.approval import _mapped_reviewers

    branch = "eng-7"
    env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    pr = env.gh.pr(7)
    reviewers = _mapped_reviewers(env.m, pr)
    assert reviewers == ["alice"]


def test_reviewer_includes_submitted_reviews(env):
    """Submitted reviewers are included even if not in requested_reviewers."""
    from symphony.merger.approval import _mapped_reviewers

    branch = "eng-7"
    env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice", requested_reviewers=["bob"])
    env.gh.pr_reviews[7] = [
        {"user": "charlie", "state": "APPROVED", "submitted_at": "2026-11-01T12:00:00Z"},
    ]
    pr = env.gh.pr(7)
    reviewers = _mapped_reviewers(env.m, pr)
    # None are mapped, so falls back to author.
    assert "alice" in reviewers


# ---------------------------------------------------------------------------
# Button id format
# ---------------------------------------------------------------------------


def test_button_id_format():
    """The button id format matches the contract."""
    bid = button_id("acme/app", 42, "abc123def456789")
    assert bid == "preview:acme/app:42:abc123def456"


# ---------------------------------------------------------------------------
# Policy parsing
# ---------------------------------------------------------------------------


def test_policy_parses_require_approval():
    policy = parse(APPROVAL_POLICY)
    assert policy.require_approval is True
    assert policy.merge_method == "squash"


def test_policy_parses_preview():
    policy = parse(PREVIEW_POLICY)
    assert policy.preview == {"artifact": "ios-simulator-preview", "platform": "ios"}


def test_policy_default_no_approval():
    policy = parse(None)
    assert policy.require_approval is False
    assert policy.preview == {}


def test_policy_bad_preview_raises():
    from symphony.merger.policy import PolicyError

    with pytest.raises(PolicyError):
        parse("---\npreview: true\n---\nRules.")


def test_policy_preview_missing_artifact_raises():
    from symphony.merger.policy import PolicyError

    with pytest.raises(PolicyError):
        parse("---\npreview:\n  platform: ios\n---\nRules.")


# ---------------------------------------------------------------------------
# check_previews_for_open_prs
# ---------------------------------------------------------------------------


def test_check_previews_scans_open_prs(env):
    """check_previews_for_open_prs scans open non-draft PRs."""
    branch = "eng-7"
    head = env.world.commit(branch, {"src/a.py": "A = 1\n"}, "feat: a")
    env.gh.open_pr(7, branch, "ENG-7 add a", author="alice")
    env.gh.tick()
    policy = parse(PREVIEW_POLICY)
    run_id = 5001
    env.gh.runs[head] = {
        "id": run_id, "name": "CI", "sha": head, "status": "completed",
        "conclusion": "success", "attempt": 1, "branch": branch,
        "event": "pull_request", "url": f"https://ci/{head[:7]}", "title": head[:7],
    }
    env.gh.artifacts[run_id] = [{"name": "ios-simulator-preview", "expired": False}]
    posted = []
    with mock.patch("symphony.notify.post_with_button", side_effect=lambda *a, **kw: posted.append(kw) or True):
        check_previews_for_open_prs(env.m, policy)
    assert len(posted) == 1


# ---------------------------------------------------------------------------
# Approval notice: PR title, multi-reviewer, no reviewer, dedup, reminder
# ---------------------------------------------------------------------------


def test_approval_notice_carries_pr_title_not_waiting_title(env_approval):
    """Regression: the approval notice must show the PR title, not 'Waiting for CI'."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1
    text = approval_notes[0][1]
    assert "ENG-7 change" in text
    assert "Waiting for CI" not in text


def test_approval_notice_mentions_two_mapped_reviewers(env_approval):
    """One message mentions both mapped reviewers."""
    from symphony import notify
    notify.configure({"slack": {"reviewer1": "U1", "reviewer2": "U2"}})
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.gh.prs[7]["requested_reviewers"] = ["reviewer1", "reviewer2"]
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1
    logins = approval_notes[0][3]  # the github_logins list
    assert "reviewer1" in logins
    assert "reviewer2" in logins
    text = approval_notes[0][1]
    assert "reviewer1" in text and "reviewer2" in text
    notify.reset()


def test_approval_notice_no_reviewer_says_so(env_approval):
    """When no reviewer is requested, the message says so."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.gh.prs[7]["requested_reviewers"] = []
    # Remove the default author fallback by setting author to empty
    env_approval.gh.prs[7]["author"] = ""
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1
    text = approval_notes[0][1]
    assert "no reviewer is requested" in text


def test_approval_same_head_twice_sends_one_message(env_approval):
    """Same (PR, head) fires only one notice."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.train.step()
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1


def test_approval_new_head_gets_new_message(env_approval):
    """A new head sha gets a fresh notice."""
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.train.step()
    # Push a new head
    head2 = env_approval.world.commit("eng-7", {"src/a.py": "A = 2\n"}, "feat: update")
    env_approval.gh.prs[7]["head_sha"] = head2
    # The train sees the push and drops the PR
    env_approval.train.step()
    # Re-hand it with new head
    env_approval.m.store.put_rec({
        "pr": 7, "branch": "eng-7", "author": "dev7", "title": "ENG-7 change",
        "issue": "ENG-7", "head_sha": head2, "reviewed_at": env_approval.m.clock(),
        "ready_at": "2026-11-01T10:00:00Z", "started_at": env_approval.m.clock(),
        "policy": "symphony-merger", "state": "reviewed",
        "publication": {"report": "r", "done": ["comment", "labels", "ticket"]},
    })
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 2


def test_approval_reminder_after_24h(env_approval, monkeypatch):
    """A reminder fires once after 24 h."""
    import time as _time
    real_time = _time.time()
    clock = [real_time]
    monkeypatch.setattr(_time, "time", lambda: clock[0])
    handed(env_approval, 7, {"src/a.py": "A = 1\n"})
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 1
    # Advance 24h + 1
    clock[0] = real_time + 86401
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 2
    assert "Reminder:" in approval_notes[1][1]
    # No second reminder
    clock[0] = real_time + 86401 * 2
    env_approval.train.step()
    approval_notes = [n for n in env_approval.notes if n[0] == "needs_approval"]
    assert len(approval_notes) == 2
