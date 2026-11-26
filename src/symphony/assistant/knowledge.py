"""The team knowledge base: repositories (or folders in them) of the team's engineering docs.

- **Bases:** `assistant.knowledge` entries `{repo, paths, name?, when?}`. `ReadOnlyRepos`
  always checks their repositories out and calls `Knowledge.update` after each refresh.
- **Index:** when a base's commit changes, its Markdown files that match `paths` (outside
  `node_modules`, `vendor` and `.git`, at most 1 MB each) are read from the mirror at that
  commit: a page per file (path, title, first paragraph line) and a section per `#`, `##` or
  `###` heading, for BM25. A failed sync or build keeps the last index, marked stale.
- **Prompt:** every role gets a "Team knowledge" section: per base its label, `when`, commit,
  the blob link prefix and the page list (beyond the cap: folders with counts).
- **Search:** `search_knowledge` ranks sections with BM25: lowercase, accents stripped
  (NFKD), words of 2+ characters without English and Portuguese stopwords, heading words
  counted `HEADING_BOOST` times.
"""

from __future__ import annotations

import math
import os
import posixpath
import re
import subprocess
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_PATHS = ("**/*.md",)
MAX_BASES = 10
MAX_PATHS = 20
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 2000  # per base
SKIP_DIRS = frozenset({"node_modules", "vendor", ".git"})
MARKDOWN = (".md", ".markdown")
FIRST_LINE = 160
PROMPT_PAGES = 300  # per base
PROMPT_BYTES = 40_000  # per base
SEARCH_DEFAULT = 8
SEARCH_MAX = 20
SNIPPET = 300
HEADING_BOOST = 3
K1, B = 1.2, 0.75

STOPWORDS = frozenset("""
a an and are as at be been but by can do does for from has have how i if in into is it its
of on or our so than that the their them then there these they this to was we were what when
where which who why will with you your
ao aos as com como da das de do dos ele ela eles em entre essa esse esta este eu foi isso
mais mas na nas nao no nos num numa os ou para pela pelo por qual quando que se sem ser seu
sua sao tem um uma voce
""".split())

_HEADING = re.compile(r"(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"(`{3,}|~{3,})")
_LIST = re.compile(r"(?:[-*+]|\d+[.)])[ \t]+")
_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class KnowledgeBase:
    """One `assistant.knowledge` entry."""
    repo: str
    paths: tuple[str, ...] = DEFAULT_PATHS
    name: str | None = None
    when: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.repo

    def as_entry(self) -> dict:
        """The WORKFLOW.md entry this base was parsed from (unset fields omitted)."""
        entry: dict = {"repo": self.repo, "paths": list(self.paths)}
        if self.name:
            entry["name"] = self.name
        if self.when:
            entry["when"] = self.when
        return entry

    def wants(self, path: str) -> bool:
        """A Markdown file under one of `paths` (a folder, a file or a glob)."""
        if not path.lower().endswith(MARKDOWN) or SKIP_DIRS & set(path.split("/")[:-1]):
            return False
        return any(_matcher(p)(path) for p in self.paths)


@lru_cache(maxsize=256)
def _matcher(pattern: str) -> Callable[[str], bool]:
    if not _GLOB_CHARS & set(pattern):
        folder = pattern.strip("/")
        return lambda path: path == folder or path.startswith(folder + "/")
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2:]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1:end]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body).replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    regex = re.compile("".join(out) + r"\Z")
    return lambda path: regex.match(path) is not None


# -- text --------------------------------------------------------------------------------
def fold(text: str) -> str:
    """Lowercase without accents: `Implantação` → `implantacao`."""
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c)).casefold()


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W_]+", fold(text)) if len(w) >= 2 and w not in STOPWORDS]


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    first: str


@dataclass(frozen=True)
class Section:
    path: str
    heading: str  # the heading path: "Releases > Hotfixes"
    start: int  # 1-based, inclusive
    end: int
    text: str
    tf: Mapping[str, int]
    length: int


def _body_lines(lines: list[str]) -> list[tuple[int, str, bool]]:
    """(1-based line number, line, inside a code fence) after the YAML front matter."""
    begin = 0
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
        if close is not None:
            begin = close + 1
    out, fence = [], None
    for i in range(begin, len(lines)):
        line = lines[i]
        m = _FENCE.match(line.lstrip())
        if m and (fence is None or m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)):
            out.append((i + 1, line, True))
            fence = None if fence is not None else m.group(1)
            continue
        out.append((i + 1, line, fence is not None))
    return out


def _first_line(body: list[tuple[int, str, bool]]) -> str:
    for _n, line, fenced in body:
        text = line.strip()
        if fenced or not text or text.startswith(("#", "<!--", "|", "![", "[![")) \
                or re.fullmatch(r"([-*_])(\s*\1){2,}", text):
            continue
        text = _LIST.sub("", text.lstrip("> ").strip(), count=1).strip()
        if text:
            return text if len(text) <= FIRST_LINE else text[:FIRST_LINE - 1].rstrip() + "…"
    return ""


def parse_page(path: str, content: str) -> tuple[Page, list[Section]]:
    lines = content.splitlines()
    body = _body_lines(lines)
    headings = [(n, len(m.group(1)), m.group(2).strip()) for n, line, fenced in body
                if not fenced and (m := _HEADING.match(line))]
    title = next((t for _n, level, t in headings if level == 1), None) \
        or posixpath.splitext(posixpath.basename(path))[0]
    page = Page(path, title, _first_line(body))
    cuts = [(n, level, t) for n, level, t in headings if level <= 3]
    sections: list[Section] = []
    first = body[0][0] if body else 1
    stack: list[tuple[int, str]] = []
    bounds = [(first, None)] + [(n, (level, t)) for n, level, t in cuts]
    for i, (start, head) in enumerate(bounds):
        end = bounds[i + 1][0] - 1 if i + 1 < len(bounds) else len(lines)
        if head is not None:
            level, name = head
            stack = [s for s in stack if s[0] < level] + [(level, name)]
        elif start > end or not any(lines[j].strip() for j in range(start - 1, end)):
            continue  # nothing before the first heading
        while start < end and not lines[start - 1].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        names = [t for _level, t in stack]
        if not stack or stack[0][0] != 1:
            names.insert(0, title)
        heading = " > ".join(names)
        text = "\n".join(lines[start - 1:end])
        tf = Counter(tokens(text))
        for word in tokens(heading):
            tf[word] += HEADING_BOOST
        sections.append(Section(path, heading, start, end, text, tf, sum(tf.values())))
    return page, sections


# -- the index ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Index:
    """One base at one commit."""
    base: KnowledgeBase
    sha: str
    pages: tuple[Page, ...]
    sections: tuple[Section, ...]
    df: Mapping[str, int]
    avgdl: float

    def search(self, terms: Sequence[str]) -> list[tuple[float, Section]]:
        n = len(self.sections)
        idf = {t: math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
               for t in terms if self.df.get(t)}
        found = []
        for s in self.sections:
            score = 0.0
            for t, weight in idf.items():
                tf = s.tf.get(t, 0)
                if tf:
                    score += weight * tf * (K1 + 1) / (tf + K1 * (1 - B + B * s.length / self.avgdl))
            if score > 0:
                found.append((score, s))
        return found


def _git(git_dir: str, *args: str, data: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", *args], cwd=git_dir, input=data, capture_output=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} exit {proc.returncode}: "
                           f"{proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


def build_index(git_dir: str, sha: str, base: KnowledgeBase) -> Index:
    """Reads `base`'s Markdown files at `sha` from the git objects of `git_dir`."""
    wanted: list[tuple[str, str]] = []  # (blob, path)
    for entry in _git(git_dir, "ls-tree", "-r", "-l", "-z", sha).split(b"\0"):
        if not entry:
            continue
        meta, _, raw = entry.partition(b"\t")
        mode, kind, obj, size = meta.decode().split()
        path = raw.decode(errors="replace")
        if kind == "blob" and mode != "120000" and int(size) <= MAX_FILE_BYTES and base.wants(path):
            wanted.append((obj, path))
    wanted.sort(key=lambda w: w[1])
    wanted = wanted[:MAX_FILES]
    out = _git(git_dir, "cat-file", "--batch", data="".join(f"{o}\n" for o, _ in wanted).encode())
    pages, sections, at = [], [], 0
    for _obj, path in wanted:
        header_end = out.index(b"\n", at)
        size = int(out[at:header_end].split()[2])
        content = out[header_end + 1:header_end + 1 + size].decode("utf-8", errors="replace")
        at = header_end + 1 + size + 1
        page, parts = parse_page(path, content)
        pages.append(page)
        sections.extend(parts)
    df: Counter = Counter()
    for s in sections:
        df.update(s.tf.keys())
    avgdl = (sum(s.length for s in sections) / len(sections)) if sections else 1.0
    return Index(base, sha, tuple(pages), tuple(sections), df, avgdl or 1.0)


# -- the bases -------------------------------------------------------------------------------
KNOWLEDGE_INSTRUCTIONS = (
    "Team knowledge: the team's engineering docs (process, environments, releases, playbooks, "
    "decision records, the stack), checked out read-only in your working directory. For a "
    "question about how the team works, consult the knowledge base FIRST: find the pages in "
    "the index below or with search_knowledge, read them before you answer, and prefer them "
    "over other repositories. When the knowledge base does not cover the question, say so "
    "instead of guessing. Cite every knowledge page you use as a Markdown link to the file at "
    "the commit you read: the base's link prefix followed by the path, adding #L<a>-L<b> when "
    "you read a line range, for example [Releases](<prefix>process/releases.md#L10-L24)."
)


def _snippet(text: str, terms: Sequence[str]) -> str:
    folded, where = [], []
    for i, ch in enumerate(text):
        f = fold(ch)
        folded.append(f)
        where.extend([i] * len(f))
    flat = "".join(folded)
    pos = 0
    for term in terms:  # the rarest term first
        m = re.search(r"(?<![^\W_])" + re.escape(term), flat)
        if m:
            pos = where[m.start()]
            break
    start = max(0, pos - 80)
    while start > 0 and not text[start - 1].isspace() and pos - start < 120:
        start -= 1
    raw = " ".join(text[start:start + SNIPPET * 2].split())
    lead = "…" if start > 0 else ""
    room = SNIPPET - len(lead)
    if len(raw) > room or start + SNIPPET * 2 < len(text):
        raw = raw[:room - 1].rstrip() + "…"
    return lead + raw


class Knowledge:
    """The indexes of the configured bases, updated by `ReadOnlyRepos.refresh`."""

    def __init__(self, bases: Callable[[], Sequence[KnowledgeBase]], *, server_url: str,
                 logger=None) -> None:
        self.bases = lambda: tuple(bases() or ())
        self.server_url = server_url.rstrip("/")
        self.log = logger
        self._lock = threading.Lock()
        self._indexes: dict[str, Index] = {}
        self._stale: set[str] = set()

    def repos(self) -> list[str]:
        return list(dict.fromkeys(b.repo.lower() for b in self.bases()))

    def update(self, synced: Mapping[str, tuple[str, str]]) -> None:
        """`synced`: repository → (git dir, commit) of the repositories this refresh synced.
        A base whose repository is missing, or whose build fails, keeps its last index."""
        with self._lock:
            old = dict(self._indexes)
        indexes: dict[str, Index] = {}
        stale: set[str] = set()
        for base in self.bases():
            label, prev = base.label, old.get(base.label)
            got = synced.get(base.repo.lower())
            if got is not None and prev is not None and prev.sha == got[1] and prev.base == base:
                indexes[label] = prev
                continue
            if got is not None:
                try:
                    indexes[label] = index = build_index(got[0], got[1], base)
                    if self.log is not None:
                        self.log.info("assistant_knowledge_indexed", base=label, repo=base.repo,
                                      commit=got[1], pages=len(index.pages),
                                      sections=len(index.sections))
                    continue
                except Exception as exc:  # noqa: BLE001 — keep the last index
                    if self.log is not None:
                        self.log.warning("assistant_knowledge_failed", base=label,
                                         error=type(exc).__name__, detail=str(exc)[:200])
            if prev is not None:
                indexes[label] = prev
            stale.add(label)
        with self._lock:
            self._indexes, self._stale = indexes, stale

    def index(self, label: str) -> Index | None:
        with self._lock:
            return self._indexes.get(label)

    def find(self, name: str) -> KnowledgeBase | None:
        """The base named `name` (its label, or its repository when only one base has it)."""
        wanted = name.strip().casefold()
        bases = self.bases()
        by_label = [b for b in bases if b.label.casefold() == wanted]
        by_repo = [b for b in bases if b.repo.casefold() == wanted]
        return by_label[0] if by_label else by_repo[0] if len(by_repo) == 1 else None

    def prefix(self, index: Index) -> str:
        return f"{self.server_url}/{index.base.repo}/blob/{index.sha}/"

    def url(self, index: Index, path: str, lines: tuple[int, int] | None = None) -> str:
        anchor = f"#L{lines[0]}-L{lines[1]}" if lines else ""
        return self.prefix(index) + path + anchor

    def prompt(self) -> str:
        """The instructions' "Team knowledge" section; "" without bases."""
        bases = self.bases()
        if not bases:
            return ""
        with self._lock:
            indexes, stale = dict(self._indexes), set(self._stale)
        parts = [KNOWLEDGE_INSTRUCTIONS]
        for base in bases:
            index = indexes.get(base.label)
            head = f"Knowledge base {base.label} ({base.repo}"
            when = f"\nIt answers: {base.when}" if base.when else ""
            if index is None:
                parts.append(f"{head}): not available now, its repository could not be fetched. "
                             f"Say so when a question needs it.{when}")
                continue
            lines = [f"{head} at ./{base.repo.lower()}, commit {index.sha}; link prefix "
                     f"{self.prefix(index)}).{when}"]
            if base.label in stale:
                lines.append("The last sync of this base failed: it may be stale. Say so when "
                             "you answer from it.")
            lines += _page_lines(index, base)
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def search(self, query: str, base: str | None = None,
               limit: int = SEARCH_DEFAULT) -> list[dict]:
        terms = list(dict.fromkeys(tokens(query)))
        chosen = [self.find(base)] if base else list(self.bases())
        with self._lock:
            indexes = [self._indexes[b.label] for b in chosen if b and b.label in self._indexes]
        found = []
        for index in indexes:
            rare = sorted((t for t in terms if index.df.get(t)), key=lambda t: index.df[t])
            for score, s in index.search(terms):
                found.append((score, index, s, rare))
        found.sort(key=lambda f: (-f[0], f[2].path, f[2].start))
        return [{
            "base": index.base.label, "path": s.path, "heading": s.heading,
            "lines": [s.start, s.end], "score": round(score, 3),
            "snippet": _snippet(s.text, [t for t in rare if s.tf.get(t)]),
            "url": self.url(index, s.path, (s.start, s.end)),
        } for score, index, s, rare in found[:limit]]


def _page_lines(index: Index, base: KnowledgeBase) -> list[str]:
    lines = [f"- {p.path} — {p.title}" + (f" — {p.first}" if p.first else "") for p in index.pages]
    if len(lines) <= PROMPT_PAGES and len("\n".join(lines).encode()) <= PROMPT_BYTES:
        return lines or ["(no pages)"]
    folders = Counter(posixpath.dirname(p.path) or "." for p in index.pages)
    out = [f"{len(index.pages)} pages, too many to list. Folders (pages): use search_knowledge "
           f"with base {base.label!r}, or glob and grep under ./{base.repo.lower()}."]
    size = 0
    for i, (folder, count) in enumerate(sorted(folders.items())):
        line = f"- {folder}/ ({count})"
        size += len(line.encode()) + 1
        if size > PROMPT_BYTES:
            out.append(f"- … {len(folders) - i} more folders")
            break
        out.append(line)
    return out
