"""The WORKFLOW.md policy around the merger: no CodeRabbit protocol left, examples delegate
to the root policy explicitly, and the clone hook lays out multi-repo workspaces the way the
prompt describes them."""

import os
import pathlib
import subprocess

from symphony.config import parse_workflow

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "WORKFLOW.md"
CFG = parse_workflow(WORKFLOW.read_text())
EXAMPLES = sorted((ROOT / "docs" / "examples").glob("WORKFLOW.*.md"))


def test_no_policy_mentions_the_retired_automated_reviewer():
    """The merger replaced it; a leftover `@coderabbitai` instruction would have the agent
    wait for a review that never comes."""
    for path in [WORKFLOW, *EXAMPLES]:
        assert "coderabbit" not in path.read_text().lower(), path.name


def test_review_and_merge_states_are_not_implementer_work_states():
    active = {s.casefold() for s in CFG.active_states}
    assert CFG.tracker_states["rework"].casefold() in active
    assert CFG.tracker_states["review"].casefold() not in active
    assert CFG.tracker_states["merging"].casefold() not in active


def test_the_review_watch_is_on_with_its_own_answer_prompt():
    assert CFG.review_watch_enabled and CFG.answer_prompt.strip()
    assert CFG.answer_prompt.strip() != CFG.prompt_body.strip()


def test_examples_delegate_the_protocol_explicitly():
    """The per-repo examples point at the root policy rather than duplicating it, so it has
    one definition. The delegation must stay EXPLICIT: an example that silently omitted it
    would deploy an agent that never hands over to the merger."""
    assert EXAMPLES, "expected example policies to exist"
    for path in EXAMPLES:
        cfg = parse_workflow(path.read_text())
        assert "workflow.md" in cfg.prompt_body.lower(), f"{path.name} must point at the root policy"
        assert cfg.tracker_states == CFG.tracker_states, path.name
        assert "SYMPHONY_TARGET_REPOS" in cfg.hooks["after_create"], path.name


def _run_after_create(tmp_path, repos: str) -> tuple[list[list[str]], pathlib.Path]:
    """Run the real hook against a fake `git` that records its calls and materializes the
    clone directories, with the workspace root relocated into `tmp_path`."""
    workspace = tmp_path / "workspace"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "git.log"
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'if [ "$1" = clone ]; then\n'
        '  for last; do :; done\n'
        '  mkdir -p "$last/.git/info"\n'
        "fi\n"
    )
    (bin_dir / "git").chmod(0o755)
    script = CFG.hooks["after_create"].replace("/workspace", str(workspace))
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SYMPHONY_TARGET_REPOS": repos,
           "SYMPHONY_TARGET_REPO": repos.split()[0]}
    subprocess.run(["sh", "-c", script], check=True, env=env, timeout=30)
    calls = [line.split() for line in log.read_text().splitlines()]
    return calls, workspace


def test_after_create_clones_the_primary_at_the_root_and_the_rest_under_repos(tmp_path):
    calls, workspace = _run_after_create(tmp_path, "acme/api acme/web acme/docs")
    clones = {c[-2]: c[-1] for c in calls if c[0] == "clone"}
    assert clones == {
        "https://github.com/acme/api.git": str(workspace),
        "https://github.com/acme/web.git": str(workspace / "repos" / "web"),
        "https://github.com/acme/docs.git": str(workspace / "repos" / "docs"),
    }
    # every clone also fetches the other heads, so in-flight branches are visible
    fetched = {c[1] for c in calls if c[0] == "-C" and "fetch" in c}
    assert fetched == {str(workspace), str(workspace / "repos" / "web"), str(workspace / "repos" / "docs")}
    # the secondary clones can never be committed into the primary
    assert "/repos/" in (workspace / ".git" / "info" / "exclude").read_text().split()


def test_after_create_with_one_repository_clones_only_the_root(tmp_path):
    calls, workspace = _run_after_create(tmp_path, "acme/api")
    assert [c[-1] for c in calls if c[0] == "clone"] == [str(workspace)]
    assert not (workspace / "repos").exists()
