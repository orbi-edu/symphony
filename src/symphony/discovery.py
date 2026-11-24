"""Repository discovery: the repositories the GitHub App reaches, and the workspace each needs.

The daemon refreshes it in the background every `REFRESH_EVERY_MS` into `DISCOVERY_KEY`: for
each installed repository, its default branch, whether it is archived or public, whether it
carries a merger policy, and — from the file tree of the default branch — the runtime and,
for a monorepo, the components it needs.

What WORKFLOW.md configures always wins. Dispatch uses a detection only for a repository the
workflow says nothing about (`effective`): no `repo_runtimes` entry means the detected runtime,
no `components` entry means the detected components. The API shows both side by side on
`/v1/repos`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace

import httpx
import structlog

from .config import Config, known_runtimes
from .endpoints import github_api_url

log = structlog.get_logger()

DISCOVERY_KEY = "repos:discovery"
REFRESH_EVERY_MS = 900_000
MAX_REPOS = 200
MAX_READS_PER_REPO = 6
TIMEOUT_S = 10.0
# Top-level directories that hold no deployable project of their own.
NOT_COMPONENTS = frozenset({"docs", "doc", "scripts", "tools", "test", "tests", "examples",
                            "infra", "deploy", "charts", "terraform", "vendor", "third_party"})

_DOTNET_FILE = re.compile(r"\.(csproj|fsproj|vbproj|sln|slnx)$")
_PYTHON_FILES = frozenset({"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"})
_OTHER_FILES = frozenset({"go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
                          "Gemfile", "composer.json"})
_SDK_VERSION = re.compile(r'"version"\s*:\s*"(\d+)\.')
_TARGET_FRAMEWORK = re.compile(r"<TargetFrameworks?>\s*net(\d+)\.\d")


def _ecosystem(name: str) -> str | None:
    if name == "package.json":
        return "node"
    if name == "global.json" or _DOTNET_FILE.search(name):
        return "dotnet"
    if name in _PYTHON_FILES:
        return "python"
    if name in _OTHER_FILES:
        return "other"
    return None


def _markers(paths: Iterable[str], prefix: str, depth: int) -> dict[str, list[str]]:
    """Ecosystem → marker paths among files at most `depth` directories below `prefix`."""
    found: dict[str, list[str]] = {}
    for path in paths:
        if prefix and not path.startswith(prefix + "/"):
            continue
        rest = path[len(prefix) + 1:] if prefix else path
        parts = rest.split("/")
        if len(parts) - 1 > depth or any(p.startswith(".") or p == "node_modules" for p in parts[:-1]):
            continue
        kind = _ecosystem(parts[-1])
        if kind:
            found.setdefault(kind, []).append(path)
    return found


def _runtime(found: dict[str, list[str]], read: Callable[[str], str | None],
             runtimes: frozenset[str]) -> str | None:
    def pick(name: str) -> str | None:
        return name if name in runtimes else ("polyglot" if "polyglot" in runtimes else None)

    kinds = set(found)
    if not kinds or len(kinds) > 1 or kinds & {"python", "other"}:
        return pick("polyglot")
    if kinds == {"node"}:
        return pick("node")
    # .NET: the SDK pinned in global.json, else the first project's target framework.
    version = None
    for path in sorted(found["dotnet"], key=lambda p: (not p.endswith("global.json"), len(p))):
        text = read(path) or ""
        match = (_SDK_VERSION if path.endswith("global.json") else _TARGET_FRAMEWORK).search(text)
        if match:
            version = match.group(1)
            break
    if version and f"dotnet{version}" in runtimes:
        return f"dotnet{version}"
    newest = sorted((r for r in runtimes if re.fullmatch(r"dotnet\d+", r)),
                    key=lambda r: int(r[len("dotnet"):]))
    return newest[-1] if newest else pick("polyglot")


def detect(paths: list[str], read: Callable[[str], str | None], runtimes: frozenset[str]) -> dict:
    """`{runtime, components}` for a file tree. Two or more top-level directories with a
    project each make a monorepo: each one is a component with its own runtime, and the
    repository's runtime covers them all."""
    tops = sorted({p.split("/", 1)[0] for p in paths if "/" in p})
    units = {top: found for top in tops
             if not top.startswith(".") and top.casefold() not in NOT_COMPONENTS
             and (found := _markers(paths, top, 2))}
    root = _markers(paths, "", 0)
    if len(units) >= 2:
        # Component names follow the workflow's rule (`[a-z0-9][a-z0-9-]*`); the path keeps
        # the directory's own spelling.
        components = {_component_name(top): {"path": top, "runtime": _runtime(found, read, runtimes)}
                      for top, found in units.items() if _component_name(top)}
        distinct = {c["runtime"] for c in components.values()}
        if root:
            distinct.add(_runtime(root, read, runtimes))
        polyglot = "polyglot" if "polyglot" in runtimes else None
        return {"runtime": distinct.pop() if len(distinct) == 1 else polyglot,
                "components": components}
    merged = dict(root)
    for found in units.values():
        for kind, files in found.items():
            merged.setdefault(kind, []).extend(files)
    return {"runtime": _runtime(merged, read, runtimes), "components": {}}


def _component_name(directory: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", directory.lower()).strip("-")


def effective(cfg: Config, doc: dict | None) -> Config:
    """`cfg` with detections filling what WORKFLOW.md leaves unset, per repository."""
    if not doc:
        return cfg
    runtimes = dict(cfg.repo_runtimes)
    components = dict(cfg.components)
    for item in doc.get("items") or []:
        name = str(item.get("name") or "").lower()
        if not name or item.get("archived"):
            continue
        if item.get("runtime") and name not in runtimes:
            runtimes[name] = item["runtime"]
        if item.get("components") and name not in components:
            components[name] = item["components"]
    if runtimes == cfg.repo_runtimes and components == cfg.components:
        return cfg
    return replace(cfg, repo_runtimes=runtimes, components=components)


def public(doc: dict | None) -> frozenset[str]:
    """The repositories (lower-case `owner/name`) discovery saw as public. One it has not seen
    yet, or whose visibility it did not read, counts as private."""
    return frozenset(str(item.get("name") or "").lower() for item in (doc or {}).get("items") or []
                     if item.get("private") is False)


def discover(http: httpx.Client, token: str, runtimes: frozenset[str]) -> list[dict]:
    """One pass over the installation: GET /installation/repositories, then per repository
    the recursive tree of its default branch and a few small files."""
    api = github_api_url()
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    repos: list[dict] = []
    url: str | None = f"{api}/installation/repositories?per_page=100"
    while url and len(repos) < MAX_REPOS:
        response = http.get(url, headers=headers, timeout=TIMEOUT_S)
        response.raise_for_status()
        repos.extend(response.json().get("repositories") or [])
        url = response.links.get("next", {}).get("url")
    items = []
    for repo in sorted(repos[:MAX_REPOS], key=lambda r: r["full_name"].lower()):
        items.append(_inspect(http, headers, api, repo, runtimes))
    return items


def _inspect(http: httpx.Client, headers: dict, api: str, repo: dict,
             runtimes: frozenset[str]) -> dict:
    name = repo["full_name"].lower()
    branch = repo.get("default_branch") or "main"
    item = {"name": name, "default_branch": branch, "archived": bool(repo.get("archived")),
            "private": bool(repo.get("private", True)),
            "merger_policy": False, "runtime": None, "components": {}, "truncated": False}
    if item["archived"]:
        return item
    try:
        response = http.get(f"{api}/repos/{name}/git/trees/{branch}?recursive=1",
                            headers=headers, timeout=TIMEOUT_S)
        if response.status_code in (404, 409):  # an empty repository has no tree
            return item
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        item["error"] = f"tree unreadable: {type(exc).__name__}"
        return item
    paths = [e["path"] for e in body.get("tree") or [] if e.get("type") == "blob"]
    item["truncated"] = bool(body.get("truncated"))
    item["merger_policy"] = ".symphony/merger.md" in paths
    reads = {"left": MAX_READS_PER_REPO}

    def read(path: str) -> str | None:
        if reads["left"] <= 0:
            return None
        reads["left"] -= 1
        try:
            r = http.get(f"{api}/repos/{name}/contents/{path}?ref={branch}",
                         headers={**headers, "Accept": "application/vnd.github.raw"},
                         timeout=TIMEOUT_S)
            return r.text if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    item.update(detect(paths, read, runtimes))
    return item


class Refresher:
    """Keeps `DISCOVERY_KEY` fresh from the daemon, off the tick's thread: `due` is cheap,
    `start` runs one pass in the background and never two at once."""

    def __init__(self, kv, token: Callable[[], str | None], now_ms: Callable[[], int],
                 http: httpx.Client | None = None,
                 after: Callable[[list[dict], Config], None] | None = None) -> None:
        self.kv = kv
        self.token = token
        self.now_ms = now_ms
        self.http = http or httpx.Client()
        # Runs on the fresh items before they are stored (provisioning), and may annotate them.
        self.after = after
        self._running = threading.Lock()

    def due(self) -> bool:
        doc = self.kv.get(DISCOVERY_KEY) or {}
        return self.now_ms() - (doc.get("observed_at_ms") or 0) >= REFRESH_EVERY_MS

    def start(self, cfg: Config) -> None:
        if not self.due() or not self._running.acquire(blocking=False):
            return
        threading.Thread(target=self._locked_pass, args=(cfg,), daemon=True).start()

    def _locked_pass(self, cfg: Config) -> None:
        try:
            self.run_once(cfg)
        finally:
            self._running.release()

    def run_once(self, cfg: Config) -> None:
        """One pass in the caller's thread. A failure keeps the last good items and records
        when it was tried, so the next attempt waits a full interval."""
        try:
            token = self.token()
            if not token:
                return
            items = discover(self.http, token, known_runtimes(cfg))
            if self.after is not None:
                try:
                    self.after(items, cfg)
                except Exception as exc:  # noqa: BLE001 — the discovery itself still counts
                    log.warning("repos_after_discovery_failed", error=str(exc)[:300])
            self.kv.put(DISCOVERY_KEY, {"observed_at_ms": self.now_ms(), "items": items})
            log.info("repos_discovered", repositories=len(items))
        except Exception as exc:  # noqa: BLE001 — discovery is advisory; the last pass stays
            log.warning("repos_discovery_failed", error=str(exc)[:300])
            previous = self.kv.get(DISCOVERY_KEY) or {}
            self.kv.put(DISCOVERY_KEY, {**previous, "observed_at_ms": self.now_ms(),
                                        "error": type(exc).__name__})
