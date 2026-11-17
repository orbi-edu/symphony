"""The assistant's soul: a persona and team rules kept in Git, read at each new session.

`assistant.soul: {repo, path}` names the file (default `.symphony/assistant.md` in the first
`assistant.repositories` entry, else `default_repo`). It is read from the base branch of the
assistant's read-only mirror, at most `MAX_BYTES`. Optional front matter sets `name`,
`language` (the default reply language) and `tone`. The body is appended to the role
instructions; it never replaces the safety rules. The active version is the file's blob sha. A
missing file falls back to the built-in instructions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import yaml

MAX_BYTES = 8 * 1024
FRONT_KEYS = ("name", "language", "tone")
MAX_FRONT = 200


@dataclass(frozen=True)
class Soul:
    repo: str
    path: str
    sha: str
    body: str
    name: str = ""
    language: str = ""
    tone: str = ""

    def instructions(self) -> str:
        """What the session's instructions get after the built-in rules."""
        lines = []
        if self.name:
            lines.append(f"Your name is {self.name}.")
        if self.language:
            lines.append(f"Reply in {self.language} unless the person writes in another language.")
        if self.tone:
            lines.append(f"Tone: {self.tone}.")
        body = self.body.strip()
        if body:
            lines.append(f"Personality and team rules from {self.repo}:{self.path}:")
            lines.append(body)
        if lines:
            lines.append("The rules before this part always win: you only read, you never reveal "
                         "a credential, and people confirm every change.")
        return "\n".join(lines)


def source(cfg) -> tuple[str, str] | None:
    """(repo, path) of the soul file, or None when no repository is known."""
    repos = cfg.assistant_repositories or ()
    repo = cfg.assistant_soul_repo or (repos[0] if repos else None) or cfg.default_repo
    return (repo.lower(), cfg.assistant_soul_path) if repo else None


def parse(text: str) -> tuple[dict[str, str], str]:
    """(front matter fields, body). Broken or unknown front matter is ignored, not fatal."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    rest = text[end + 4:]
    body = rest[1:] if rest.startswith("\n") else rest
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}, body
    front = {}
    if isinstance(data, dict):
        for key in FRONT_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                front[key] = " ".join(value.split())[:MAX_FRONT]
    return front, body


def cut(data: bytes) -> str:
    """At most MAX_BYTES of the file, cut on a character boundary."""
    return data[:MAX_BYTES].decode("utf-8", errors="ignore")


class SoulReader:
    """Reads the soul for each new session and remembers the last one for the heartbeat.

    `blob(repo, path)` returns `(blob sha, bytes)` from the mirror, or None when the file is
    not there."""

    def __init__(self, config: Callable[[], object], blob: Callable[[str, str], tuple[str, bytes] | None],
                 *, logger=None) -> None:
        self.config = config
        self.blob = blob
        self.log = logger
        self._lock = threading.Lock()
        self._active: dict | None = None

    def read(self) -> Soul | None:
        src = source(self.config())
        if src is None:
            self._set(None)
            return None
        repo, path = src
        try:
            found = self.blob(repo, path)
        except Exception as exc:  # noqa: BLE001 — the built-in text serves
            found = None
            if self.log is not None:
                self.log.warning("assistant_soul_failed", repo=repo, path=path,
                                 error=type(exc).__name__, detail=str(exc)[:200])
        if found is None:
            self._set({"repo": repo, "path": path, "sha": None})
            return None
        sha, data = found
        if len(data) > MAX_BYTES and self.log is not None:
            self.log.warning("assistant_soul_truncated", repo=repo, path=path, size=len(data),
                             limit=MAX_BYTES)
        front, body = parse(cut(data))
        self._set({"repo": repo, "path": path, "sha": sha})
        return Soul(repo=repo, path=path, sha=sha, body=body, **front)

    def active(self) -> dict | None:
        """`{repo, path, sha}` of the last read (sha None: the file is missing), or None."""
        with self._lock:
            return dict(self._active) if self._active is not None else None

    def _set(self, doc: dict | None) -> None:
        with self._lock:
            self._active = doc
