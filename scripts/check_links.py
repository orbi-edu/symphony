#!/usr/bin/env python3
"""Check the relative links and anchors in README.md and docs/ (CI runs it; so can you).

    python3 scripts/check_links.py            # from the repository root; exit 1 on a bad link

A link is bad when its file does not exist, or when its `#anchor` matches no heading and no
`id`/`name` attribute of the target Markdown file. External links (`https:`, `mailto:` …) are
not checked. Code blocks and inline code are skipped. Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`+[^`]*`+")
# [text](target "title") and ![alt](target); the text may hold one level of brackets.
INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'(][^)]*)?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
HTML_ATTR = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""")
HTML_ID = re.compile(r"""<[a-zA-Z][^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def sources(root: Path) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


def prose_lines(text: str, *, strip_code: bool = True) -> list[tuple[int, str]]:
    """The lines outside fenced code blocks; inline code removed unless `strip_code` is off."""
    lines, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            if fence is None:
                fence = match.group(1)
            elif fence == match.group(1):
                fence = None
            continue
        if fence is None:
            lines.append((number, INLINE_CODE.sub("", line) if strip_code else line))
    return lines


def slug(heading: str) -> str:
    """GitHub's heading anchor: the rendered text, lowercased, punctuation dropped, spaces to `-`."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link renders as its text
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").replace("*", "")
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path, cache: dict[Path, set[str]]) -> set[str]:
    if path not in cache:
        found: set[str] = set()
        seen: dict[str, int] = {}
        text = path.read_text(encoding="utf-8")
        for _, line in prose_lines(text):
            found.update(HTML_ID.findall(line))
        for match in (HEADING.match(line) for _, line in prose_lines(text, strip_code=False)):
            if match:
                base = slug(match.group(2))
                count = seen.get(base, 0)
                seen[base] = count + 1
                found.add(base if count == 0 else f"{base}-{count}")
        cache[path] = found
    return cache[path]


def targets(line: str) -> list[str]:
    found = [m.group(1) for m in INLINE_LINK.finditer(line)]
    found += [m.group(1) for m in REFERENCE.finditer(line)]
    found += HTML_ATTR.findall(line)
    return [target.strip("<>") for target in found]


def check(root: Path) -> list[str]:
    problems: list[str] = []
    cache: dict[Path, set[str]] = {}
    for source in sources(root):
        for number, line in prose_lines(source.read_text(encoding="utf-8")):
            for target in targets(line):
                if SCHEME.match(target) or target.startswith("//"):
                    continue
                where = f"{source.relative_to(root)}:{number}: {target}"
                path_part, _, anchor = target.partition("#")
                path_part = unquote(path_part.split("?", 1)[0])
                if not path_part:
                    path = source
                elif path_part.startswith("/"):
                    path = root / path_part.lstrip("/")
                else:
                    path = (source.parent / path_part).resolve()
                if not path.exists():
                    problems.append(f"{where}: no such file")
                elif anchor and path.suffix == ".md" and unquote(anchor) not in anchors(path, cache):
                    problems.append(f"{where}: no such anchor")
    return problems


def main() -> int:
    problems = check(ROOT)
    for problem in problems:
        print(problem)
    print(f"{len(problems)} broken link(s)" if problems else "links ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
