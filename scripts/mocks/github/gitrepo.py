"""Git plumbing over the mock's bare repositories: refs, merges (merge/squash/rebase as GitHub
does them), comparisons, file reads and single-file commits. Everything is real git."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

GITHUB = ("GitHub", "noreply@github.com")
ZERO = "0" * 40


class GitFailure(Exception):
    pass


class Conflict(Exception):
    pass


def git(path: str, *args: str, input: bytes | None = None, env: dict | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=path, input=input, capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
             "HOME": os.environ.get("HOME", "/tmp"), **(env or {})},
    )
    if check and proc.returncode != 0:
        raise GitFailure(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace')[-2000:]}")
    return proc


def out(path: str, *args: str, **kw) -> str:
    return git(path, *args, **kw).stdout.decode(errors="replace").strip()


def init_bare(path: str, default_branch: str) -> None:
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "--bare", "-b", default_branch)
    git(path, "config", "http.receivepack", "true")
    git(path, "config", "uploadpack.allowFilter", "true")
    git(path, "config", "receive.advertisePushOptions", "true")


def seed(path: str, default_branch: str, source: str | None, name: str) -> None:
    """The first commit: the source repository's HEAD history when it is a git repository,
    otherwise a snapshot of its files (or a README) committed at a fixed instant."""
    env = {"GIT_AUTHOR_NAME": "Mock Seed", "GIT_AUTHOR_EMAIL": "seed@mock.invalid",
           "GIT_COMMITTER_NAME": "Mock Seed", "GIT_COMMITTER_EMAIL": "seed@mock.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    if source and os.path.isdir(os.path.join(source, ".git")):
        git(source, "push", "-q", path, f"HEAD:refs/heads/{default_branch}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        git(tmp, "init", "-q", "-b", default_branch)
        if source:
            subprocess.run(["cp", "-R", f"{source.rstrip('/')}/.", tmp], check=True)
        else:
            with open(os.path.join(tmp, "README.md"), "w") as fh:
                fh.write(f"# {name}\n")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-q", "--allow-empty", "-m", "Initial commit", env=env)
        git(tmp, "push", "-q", path, f"HEAD:refs/heads/{default_branch}")


def refs(path: str) -> dict[str, str]:
    text = out(path, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in text.splitlines() if line)


def branch_sha(path: str, branch: str) -> str | None:
    proc = git(path, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}", check=False)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def resolve(path: str, rev: str) -> str | None:
    """A commit for a branch name, tag, `heads/x` or (abbreviated) SHA."""
    for candidate in (f"refs/heads/{rev}", f"refs/tags/{rev}", f"refs/{rev}", rev):
        proc = git(path, "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}", check=False)
        if proc.returncode == 0:
            return proc.stdout.decode().strip()
    return None


def object_type(path: str, sha: str) -> str | None:
    proc = git(path, "cat-file", "-t", sha, check=False)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def update_ref(path: str, ref: str, new: str, old: str | None = None) -> None:
    git(path, "update-ref", ref, new, *([old] if old else []))


def delete_ref(path: str, ref: str) -> None:
    git(path, "update-ref", "-d", ref)


def is_ancestor(path: str, a: str, b: str) -> bool:
    return git(path, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def merge_base(path: str, a: str, b: str) -> str | None:
    proc = git(path, "merge-base", a, b, check=False)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def merge_tree(path: str, base: str, head: str, merge_base_rev: str | None = None) -> str:
    """The tree of merging `head` into `base`; `Conflict` when git cannot."""
    args = ["merge-tree", "--write-tree", "--no-messages"]
    if merge_base_rev:
        args.append(f"--merge-base={merge_base_rev}")
    proc = git(path, *args, base, head, check=False)
    if proc.returncode == 1:
        raise Conflict(head)
    if proc.returncode != 0:
        raise GitFailure(proc.stderr.decode(errors="replace"))
    return proc.stdout.decode().split("\n", 1)[0].strip()


def mergeable(path: str, base: str, head: str) -> bool:
    try:
        merge_tree(path, base, head)
        return True
    except Conflict:
        return False


@dataclass
class CommitInfo:
    sha: str
    tree: str
    parents: list[str]
    author: tuple[str, str, str]
    committer: tuple[str, str, str]
    message: str


def commit_info(path: str, sha: str) -> CommitInfo:
    fmt = "%H%x00%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"
    parts = out(path, "show", "-s", f"--format={fmt}", sha).split("\x00")
    return CommitInfo(parts[0], parts[1], parts[2].split(), (parts[3], parts[4], parts[5]),
                      (parts[6], parts[7], parts[8]), parts[9].rstrip("\n"))


def commit_tree(path: str, tree: str, parents: list[str], message: str,
                author: tuple[str, str, str | None] | None = None,
                committer: tuple[str, str] = GITHUB) -> str:
    env = {"GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
    a = author or (committer[0], committer[1], None)
    env.update(GIT_AUTHOR_NAME=a[0], GIT_AUTHOR_EMAIL=a[1])
    if a[2]:
        env["GIT_AUTHOR_DATE"] = a[2]
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    return out(path, *args, input=message.encode(), env=env)


def range_commits(path: str, base: str, head: str) -> list[str]:
    """Commits on `head` not on `base`, oldest first (GitHub's PR commit list)."""
    text = out(path, "rev-list", "--reverse", "--topo-order", f"{base}..{head}")
    return [s for s in text.splitlines() if s]


def merge(path: str, base_sha: str, head_sha: str, method: str, title: str, message: str,
          author: tuple[str, str]) -> str:
    """The new base head after merging `head_sha` into `base_sha` with GitHub's method."""
    if method == "merge":
        tree = merge_tree(path, base_sha, head_sha)
        return commit_tree(path, tree, [base_sha, head_sha], f"{title}\n\n{message}".rstrip() + "\n",
                           author=(author[0], author[1], None))
    if method == "squash":
        tree = merge_tree(path, base_sha, head_sha)
        return commit_tree(path, tree, [base_sha], f"{title}\n\n{message}".rstrip() + "\n",
                           author=(author[0], author[1], None))
    # rebase: replay every non-merge commit of the head onto the base; committer is GitHub,
    # authorship is kept — new SHAs even when a fast-forward would do, as on GitHub.
    current = base_sha
    for sha in out(path, "rev-list", "--reverse", "--no-merges", f"{base_sha}..{head_sha}").splitlines():
        info = commit_info(path, sha)
        parent = info.parents[0] if info.parents else None
        tree = merge_tree(path, current, sha, merge_base_rev=parent) if parent else info.tree
        current = commit_tree(path, tree, [current], info.message + "\n", author=info.author)
    return current


def diff(path: str, base: str, head: str) -> str:
    return git(path, "diff", "--no-color", "-M", f"{base}...{head}").stdout.decode(errors="replace")


def format_patch(path: str, base: str, head: str) -> str:
    return git(path, "format-patch", "--stdout", "--no-color", f"{base}..{head}").stdout.decode(errors="replace")


@dataclass
class FileChange:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str
    previous_filename: str | None
    sha: str | None


STATUS = {"A": "added", "D": "removed", "M": "modified", "R": "renamed", "C": "copied", "T": "changed"}


def files(path: str, base: str, head: str) -> list[FileChange]:
    """GitHub's per-file diff entries for `base...head`."""
    mb = merge_base(path, base, head) or base
    status = out(path, "diff", "--name-status", "-M", "-z", mb, head)
    numstat = out(path, "diff", "--numstat", "-M", "-z", mb, head)
    counts: dict[str, tuple[int, int]] = {}
    tokens = numstat.split("\x00")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        add, dele, name = (tok.split("\t") + ["", "", ""])[:3]
        if name == "":  # rename: the two names follow
            name = tokens[i + 2]
            i += 3
        else:
            i += 1
        counts[name] = (int(add) if add.isdigit() else 0, int(dele) if dele.isdigit() else 0)
    out_list: list[FileChange] = []
    tokens = status.split("\x00")
    i = 0
    while i < len(tokens):
        code = tokens[i]
        if not code:
            i += 1
            continue
        letter = code[0]
        if letter in "RC":
            prev, name = tokens[i + 1], tokens[i + 2]
            i += 3
        else:
            prev, name = None, tokens[i + 1]
            i += 2
        patch = git(path, "diff", "--no-color", "-M", mb, head, "--", *([prev] if prev else []), name).stdout
        text = patch.decode(errors="replace")
        hunk = text.find("\n@@")
        add, dele = counts.get(name, (0, 0))
        blob = blob_sha(path, head, name) if letter != "D" else None
        out_list.append(FileChange(name, STATUS.get(letter, "modified"), add, dele,
                                   text[hunk + 1:] if hunk >= 0 else "", prev, blob))
    return out_list


def blob_sha(path: str, rev: str, file: str) -> str | None:
    proc = git(path, "rev-parse", "--verify", "-q", f"{rev}:{file}", check=False)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def ls(path: str, rev: str, file: str) -> list[tuple[str, str, str, str]] | None:
    """`(mode, type, sha, name)` for `file` (one entry) or the entries of a directory."""
    target = file.strip("/")
    kind = object_type(path, f"{rev}:{target}") if target else "tree"
    if kind is None:
        return None
    if kind == "tree":
        text = out(path, "ls-tree", "-z", f"{rev}:{target}" if target else rev)
    else:
        text = out(path, "ls-tree", "-z", rev, "--", target)
    entries = []
    for line in text.split("\x00"):
        if not line:
            continue
        meta, name = line.split("\t", 1)
        mode, typ, sha = meta.split()
        entries.append((mode, typ, sha, name if kind != "tree" or not target else f"{target}/{name}"))
    return entries


def read_blob(path: str, sha: str) -> bytes:
    return git(path, "cat-file", "blob", sha).stdout


def write_file(path: str, parent: str, file: str, content: bytes | None, message: str,
               author: tuple[str, str], committer: tuple[str, str]) -> str:
    """A commit on `parent` that writes (or, with None, deletes) one file."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        git(path, "read-tree", parent, env=env)
        if content is None:
            # mode 0 drops the entry; `--remove` would want a work tree, which a bare repo lacks
            git(path, "update-index", "--index-info", input=f"0 {ZERO}\t{file}\n".encode(), env=env)
        else:
            blob = out(path, "hash-object", "-w", "--stdin", input=content)
            git(path, "update-index", "--add", "--cacheinfo", f"100644,{blob},{file}", env=env)
        tree = out(path, "write-tree", env=env)
    return commit_tree(path, tree, [parent], message.rstrip() + "\n",
                       author=(author[0], author[1], None), committer=committer)


def archive(path: str, sha: str, dest: str) -> None:
    data = git(path, "archive", "--format=tar", sha).stdout
    subprocess.run(["tar", "-x", "-C", dest], input=data, check=True)
