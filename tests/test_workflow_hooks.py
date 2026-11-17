"""The shipped `after_create` hook clones each repository at its base branch override."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from symphony.config import parse_workflow

ROOT = Path(__file__).resolve().parent.parent
HOOKS = [ROOT / "WORKFLOW.md", *sorted((ROOT / "docs" / "examples").glob("WORKFLOW.*.md"))]
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
       "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True,
                          check=True).stdout.strip()


def _remote(server: Path, name: str) -> None:
    """A bare `acme/<name>` on `main`, with a second branch `develop`."""
    bare, dev = server / "acme" / f"{name}.git", server / f"dev-{name}"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=server)
    _git("clone", "-q", str(bare), str(dev), cwd=server)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=dev)
    _git("push", "-q", "origin", "main", cwd=dev)
    _git("switch", "-q", "-c", "develop", cwd=dev)
    _git("commit", "-q", "--allow-empty", "-m", "dev", cwd=dev)
    _git("push", "-q", "origin", "develop", cwd=dev)


def _clone_part(script: str) -> str:
    """The hook up to its first line after the clones (examples go on to build the project)."""
    lines = script.splitlines()
    end = next((i for i, line in enumerate(lines) if line.lstrip().startswith("cd /workspace")),
               len(lines))
    return "\n".join(lines[:end]) + "\n"


@pytest.mark.parametrize("workflow", HOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize(("overrides", "app", "lib"), [
    ("", "main", "main"),
    ("acme/lib=develop", "main", "develop"),
    ("acme/app=develop acme/lib=develop", "develop", "develop"),
])
def test_after_create_clones_each_repository_at_its_base_branch(
    tmp_path, workflow, overrides, app, lib
):
    if shutil.which("sh") is None:
        pytest.skip("no POSIX sh")
    server, ws = tmp_path / "srv", tmp_path / "ws"
    server.mkdir()
    for name in ("app", "lib"):
        _remote(server, name)
    hook = parse_workflow(workflow.read_text()).hooks["after_create"]
    script = _clone_part(hook).replace("/workspace", str(ws))
    env = {**ENV, "GITHUB_SERVER_URL": f"file://{server}", "SYMPHONY_TARGET_REPO": "acme/app",
           "SYMPHONY_TARGET_REPOS": "acme/app acme/lib", "SYMPHONY_BASE_BRANCHES": overrides}
    proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for path, branch in ((ws, app), (ws / "repos" / "lib", lib)):
        assert _git("branch", "--show-current", cwd=path) == branch
        # The prompt opens the pull request against origin/HEAD.
        assert _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=path) == f"origin/{branch}"
