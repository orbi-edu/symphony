"""Local git for the merger: one mirror clone of the repository and a worktree per task.

Everything that decides a push or a merge reads the remote itself (`ls_remote`), not GitHub's
API, which can lag a push by seconds. Pushes always carry a lease on the head the merger last
saw (`--force-with-lease`), so an author's push in between is never overwritten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable

IDENTITY = ("symphony-merger", "symphony-merger@users.noreply.github.com")
HELPER = '!f() { echo username=x-access-token; echo "password=$SYMPHONY_GIT_TOKEN"; }; f'


class GitError(Exception):
    def __init__(self, args: list[str], code: int, output: str) -> None:
        tail = output.strip()[-4000:]
        super().__init__(f"git {' '.join(args)} -> exit {code}\n{tail}")
        self.code = code
        self.output = output


class Mirror:
    def __init__(
        self,
        root: str,
        remote_url: str,
        *,
        main: str = "main",
        token: Callable[[], str] | None = None,
    ) -> None:
        self.root = root
        self.path = os.path.join(root, "mirror")
        self.wt_root = os.path.join(root, "wt")
        self.remote_url = remote_url
        self.main = main
        self._token = token
        self._fetch_lock = threading.Lock()

    # -- running git ------------------------------------------------------------
    def _env(self, extra: dict | None = None) -> dict:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", IDENTITY[0]),
            "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", IDENTITY[1]),
            "GIT_COMMITTER_NAME": IDENTITY[0],
            "GIT_COMMITTER_EMAIL": IDENTITY[1],
        }
        if self._token is not None:
            env["SYMPHONY_GIT_TOKEN"] = self._token()
        return {**env, **(extra or {})}

    def run(
        self, *args: str, cwd: str | None = None, check: bool = True, env: dict | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess:
        argv = ["git"]
        if self._token is not None:
            argv += ["-c", "credential.helper=", "-c", f"credential.helper={HELPER}"]
        argv += list(args)
        proc = subprocess.run(
            argv, cwd=cwd or self.path, env=self._env(env), capture_output=True, text=True,
            input=input,
        )
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, proc.stderr or proc.stdout)
        return proc

    def out(self, *args: str, cwd: str | None = None) -> str:
        return self.run(*args, cwd=cwd).stdout.strip()

    def ok(self, *args: str, cwd: str | None = None) -> bool:
        return self.run(*args, cwd=cwd, check=False).returncode == 0

    # -- the mirror ----------------------------------------------------------------
    def ensure(self) -> None:
        if os.path.isdir(os.path.join(self.path, ".git")):
            return
        os.makedirs(self.root, exist_ok=True)
        tmp = self.path + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        self.run("clone", "-q", "--no-checkout", self.remote_url, tmp, cwd=self.root)
        os.replace(tmp, self.path)

    def fetch(self) -> None:
        with self._fetch_lock:
            self.run("fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")

    @property
    def origin_main(self) -> str:
        return f"origin/{self.main}"

    def main_sha(self) -> str:
        return self.out("rev-parse", self.origin_main)

    def ls_remote(self, branch: str) -> str:
        """The branch's head on the remote itself, "" when it is gone."""
        line = self.out("ls-remote", "origin", f"refs/heads/{branch}")
        return line.split()[0] if line else ""

    def rev(self, ref: str, cwd: str | None = None) -> str:
        return self.out("rev-parse", ref, cwd=cwd)

    def exists(self, sha: str) -> bool:
        return self.ok("cat-file", "-e", f"{sha}^{{commit}}")

    def merge_base(self, a: str, b: str) -> str:
        proc = self.run("merge-base", a, b, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def tree(self, rev: str, cwd: str | None = None) -> str:
        return self.out("rev-parse", f"{rev}^{{tree}}", cwd=cwd)

    def count(self, rng: str) -> int:
        return int(self.out("rev-list", "--count", rng) or 0)

    def merges(self, rng: str) -> bool:
        return bool(self.out("rev-list", "--merges", rng))

    def show(self, rev: str, path: str) -> str | None:
        proc = self.run("show", f"{rev}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def size(self, rev: str, path: str) -> int:
        proc = self.run("cat-file", "-s", f"{rev}:{path}", check=False)
        return int(proc.stdout.strip()) if proc.returncode == 0 else 0

    def diff(self, base: str, head: str, *, exact: bool = False, cwd: str | None = None) -> str:
        """`git diff -U0` from base (its merge base with head unless exact) to head."""
        start = base if exact else (self.merge_base(base, head) or base)
        return self.run("diff", "--no-color", "--no-ext-diff", "-U0", start, head, cwd=cwd).stdout

    def changed(self, base: str, head: str) -> list[str]:
        return [p for p in self.out("diff", "--name-only", base, head).split("\n") if p]

    def shortstat(self, base: str, head: str) -> int:
        total = 0
        for line in self.out("diff", "--numstat", base, head).split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                total += sum(int(x) for x in parts[:2] if x.isdigit())
        return total

    def replay(self, base: str, rng: str) -> str:
        """The commits of `rng` replayed onto `base` without a worktree (merge-tree), or ""
        when one doesn't replay cleanly."""
        cur = base
        for commit in [c for c in self.out("rev-list", "--reverse", rng).split("\n") if c]:
            tree = self.run(
                "merge-tree", "--write-tree", "--no-messages", f"--merge-base={commit}^", cur, commit,
                check=False,
            )
            if tree.returncode != 0:
                return ""
            cur = self.out("commit-tree", tree.stdout.strip().split("\n")[0], "-p", cur, "-m", "replay")
        return cur

    # -- worktrees -------------------------------------------------------------------
    def wt_path(self, name: str) -> str:
        return os.path.join(self.wt_root, name)

    def worktree(self, name: str, rev: str) -> str:
        """A fresh worktree `name`, detached at rev; one left by a stopped task is replaced."""
        path = self.wt_path(name)
        self.remove_worktree(name)
        os.makedirs(self.wt_root, exist_ok=True)
        self.run("worktree", "add", "-q", "--force", "--detach", path, rev)
        return path

    def remove_worktree(self, name: str) -> None:
        path = self.wt_path(name)
        if os.path.isdir(path):
            self.run("rebase", "--abort", cwd=path, check=False)
            self.run("worktree", "remove", "--force", path, check=False)
            shutil.rmtree(path, ignore_errors=True)
        self.run("worktree", "prune", check=False)
        if self.ok("rev-parse", "-q", "--verify", f"refs/heads/{name}"):
            self.run("branch", "-D", name, check=False)

    def rebase(self, wt: str, onto: str, *, upstream: str | None = None, keep: bool = False) -> list[str]:
        """Rebases the worktree's branch onto `onto` (only the commits after `upstream`, when
        given): [] once done, else the conflicted files, the rebase aborted unless `keep`. Any
        other failure raises with the rebase aborted."""
        args = ["rebase", "-q"] + (["--onto", onto, upstream] if upstream else [onto])
        proc = self.run(*args, cwd=wt, check=False)
        if proc.returncode == 0:
            return []
        stopped = self.in_rebase(wt)
        files = []
        if stopped:
            files = [f for f in self.out("diff", "--name-only", "--diff-filter=U", cwd=wt).split("\n") if f]
        if not files or not keep:
            self.run("rebase", "--abort", cwd=wt, check=False)
        if not files:
            raise GitError(args, proc.returncode, proc.stderr or proc.stdout)
        return files

    def in_rebase(self, wt: str) -> bool:
        """A rebase is told by its directory, not REBASE_HEAD (git leaves that behind)."""
        for d in ("rebase-merge", "rebase-apply"):
            p = self.out("rev-parse", "--git-path", d, cwd=wt)
            if os.path.isdir(p if os.path.isabs(p) else os.path.join(wt, p)):
                return True
        return False

    def rebase_finished(self, wt: str) -> bool:
        """No rebase or cherry-pick in progress and nothing uncommitted in tracked files.
        Untracked files (a test run's `__pycache__`, `.pytest_cache`) are never pushed, so
        they don't count."""
        if self.out("status", "--porcelain", "--untracked-files=no", cwd=wt) or self.in_rebase(wt):
            return False
        return not self.ok("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", cwd=wt)

    def push_lease(self, wt: str, local: str, branch: str, expected: str) -> None:
        self.run(
            "push", "-q", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin",
            f"{local}:refs/heads/{branch}", cwd=wt,
        )

    def push_sha(self, sha: str, branch: str, expected: str) -> None:
        self.run("push", "-q", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin",
                 f"{sha}:refs/heads/{branch}")

    def delete_remote_branch(self, branch: str, expected: str) -> None:
        """Deletes the PR branch only while it still holds `expected` (the merged head)."""
        if self.ls_remote(branch) == expected:
            self.run("push", "-q", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin",
                     "--delete", branch, check=False)

    def sh(self, command: str, cwd: str, env: dict | None = None, timeout: float = 1800) -> tuple[int, str]:
        """A repository command (policy rule checks, overlap): exit code and combined output."""
        try:
            proc = subprocess.run(
                command, shell=True, cwd=cwd, env=self._env(env), capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return 124, f"timed out after {timeout:.0f}s\n{exc.stdout or ''}"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
