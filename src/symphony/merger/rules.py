"""Deterministic precheck: the generic rules a script can check, plus the repository's own
commands (`rule_checks`). Its breaks block like a confirmed finding and go in the same report.

`breaks` are rule breaks in what merges (code, docs, commit messages): each blocks. `body` are
suggestions on the PR title and body: reported, never blocking (the body doesn't merge). `leads`
are data for the reviewer (markers the PR adds), not findings.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

MACHINE_LOCAL = re.compile(r"/Users/[A-Za-z][\w.-]*/|/home/[a-z][\w.-]*/|[A-Za-z]:\\Users\\")
SECRETS = (
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("Linear API key", re.compile(r"\blin_api_[A-Za-z0-9]{32,}\b")),
)
BAD_SUBJECT = re.compile(r"^(?:fixup!|squash!|amend!|wip\b)", re.IGNORECASE)
MAX_SUBJECT = 100
MARKERS = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b")
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass
class Line:
    path: str
    line: int
    text: str


@dataclass
class Hunk:
    path: str
    start: int
    end: int


@dataclass
class Diff:
    added: list[Line] = field(default_factory=list)
    removed: list[Line] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class RuleResult:
    breaks: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    leads: list[str] = field(default_factory=list)


def parse_diff(diff: str) -> Diff:
    """Parses `git diff -U0`. File headers are read only between `diff --git` and the first
    hunk, so a removed line starting with `--` isn't taken for one."""
    out = Diff()
    old_path = new_path = ""
    old_line = new_line = 0
    header = False
    for raw in diff.split("\n"):
        if raw.startswith("diff --git "):
            header = True
        elif header and raw.startswith("--- "):
            old_path = raw[4:].removeprefix("a/")
        elif header and raw.startswith("+++ "):
            new_path = raw[4:].removeprefix("b/")
        elif raw.startswith("@@"):
            header = False
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
            if not m:
                continue
            old_line, new_line = int(m.group(1)), int(m.group(3))
            count = int(m.group(4)) if m.group(4) is not None else 1
            path = new_path if new_path != "/dev/null" else old_path
            start = new_line if count else max(new_line, 1)
            out.hunks.append(Hunk(path, start, start + max(count, 1) - 1))
        elif not header and raw.startswith("+"):
            out.added.append(Line(new_path, new_line, raw[1:]))
            new_line += 1
        elif not header and raw.startswith("-"):
            out.removed.append(Line(old_path, old_line, raw[1:]))
            old_line += 1
    return out


def location_of(location: str) -> tuple[str, int | None]:
    m = re.match(r"^\s*(?:\./)?([^\s:]+)(?::(\d+))?", location)
    if not m:
        return "", None
    return m.group(1), int(m.group(2)) if m.group(2) else None


def near_hunks(location: str, hunks: list[Hunk], near: int = 10) -> bool:
    """Whether a finding's `path:line` is within `near` lines of a hunk (a path alone: any
    hunk of that file)."""
    path, line = location_of(location)
    for h in hunks:
        if h.path != path:
            continue
        if line is None or h.start - near <= line <= h.end + near:
            return True
    return False


def _capped(items: list[str], limit: int = 15) -> str:
    shown = items[:limit]
    more = f"\n  … and {len(items) - limit} more" if len(items) > limit else ""
    return "\n".join(f"  - {x}" for x in shown) + more


def commit_breaks(git, base: str, head: str) -> list[str]:
    raw = git.out("log", "--format=%H%x00%s%x1e", f"{base}..{head}")
    bad = []
    for entry in raw.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        sha, _, subject = entry.partition("\x00")
        if not subject.strip():
            bad.append(f"{sha[:9]}: empty subject")
        elif BAD_SUBJECT.match(subject):
            bad.append(f"{sha[:9]}: {subject!r} is a fixup or work-in-progress commit")
        elif len(subject) > MAX_SUBJECT:
            bad.append(f"{sha[:9]}: subject longer than {MAX_SUBJECT} characters")
    return bad


def rule_checks(git, policy, wt: str, base: str, head: str, pr: dict) -> RuleResult:
    """The precheck on the PR's own diff (from its fork point with `base`)."""
    res = RuleResult()
    fork = git.merge_base(base, head) or base
    diff = parse_diff(git.diff(fork, head, exact=True))

    commits = commit_breaks(git, fork, head)
    if commits:
        res.breaks.append("commit messages: rewrite these subjects:\n" + _capped(commits))

    limit = policy.max_file_kib * 1024
    large = sorted({
        f"{p} ({git.size(head, p) // 1024} KiB)"
        for p in git.changed(fork, head)
        if git.size(head, p) > limit
    })
    if large:
        res.breaks.append(f"large files: files over {policy.max_file_kib} KiB:\n" + _capped(large))

    local = [f"{x.path}:{x.line}" for x in diff.added if MACHINE_LOCAL.search(x.text)]
    if local:
        res.breaks.append("machine-local paths: replace them with repository-relative ones:\n" + _capped(local))

    secrets = [
        f"{x.path}:{x.line} ({name})"
        for x in diff.added
        for name, pattern in SECRETS
        if pattern.search(x.text)
    ]
    if secrets:
        res.breaks.append("secrets: remove them and rotate each one:\n" + _capped(secrets))

    broken = []
    for x in diff.added:
        if not x.path.endswith(".md"):
            continue
        for target in LINK.findall(x.text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("#"):
                continue
            rel = target.split("#", 1)[0]
            if not rel:
                continue
            path = posixpath.normpath(
                rel.lstrip("/") if rel.startswith("/") else posixpath.join(posixpath.dirname(x.path), rel)
            )
            if not git.ok("cat-file", "-e", f"{head}:{path}"):
                broken.append(f"{x.path}:{x.line} -> {target}")
    if broken:
        res.breaks.append("doc links: these relative links resolve to nothing at the head:\n" + _capped(broken))

    env = {"SYMPHONY_BASE": fork, "SYMPHONY_HEAD": head}
    for check in policy.rule_checks:
        code, output = git.sh(check["run"], cwd=wt, env=env)
        if code != 0:
            tail = "\n".join(output.strip().split("\n")[-20:])
            res.breaks.append(f"{check['name']}: `{check['run']}` exited {code}:\n```\n{tail}\n```")

    if not (pr.get("body") or "").strip():
        res.body.append("the PR body is empty: say what changes, why, and how it was tested")

    markers = [f"{x.path}:{x.line}: {x.text.strip()[:120]}" for x in diff.added if MARKERS.search(x.text)]
    if markers:
        res.leads.append("markers the PR adds (check each is tracked):\n" + _capped(markers))
    return res
