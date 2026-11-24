"""Central skill sources: fetch skill directories from configured repositories and stage
them into agent workspaces so every role gets the instructions its operator chose.

A skill source is a repository directory holding one sub-directory per skill, each with a
``SKILL.md`` file and optionally supporting files (templates, scripts) at any depth.
Sources may be GLOBAL (every repository) or scoped to specific repositories. The target
repository's own ``.omp/skills/`` and ``.agents/skills/`` always win; among central
sources, repository-specific beats global, and earlier sources beat later ones.

Fetching uses a GitHub App installation token scoped to the source repository with read-only
permissions. The ref→SHA resolution is cached (TTL configurable, default 10 min) so repeated
calls within a session do not re-download an unchanged source. A source that fails to fetch
is logged and skipped — it never fails a run.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shlex
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import structlog

from . import ghapp

log = structlog.get_logger("symphony.skills")

# Limits: prevent a source from flooding the agent with instructions.
MAX_SKILL_FILE_BYTES = 256 * 1024  # 256 KiB per file
MAX_SOURCE_BYTES = 5 * 1024 * 1024  # 5 MiB total per source
MAX_SKILLS_PER_SOURCE = 100
# Default TTL for the ref→SHA resolution cache (seconds).
DEFAULT_REF_TTL_S = 600
# Redis key prefix for cached ref→SHA mappings.
_CACHE_PREFIX = "sym:{ns}:skill-ref"
# Cache key version: bump when the serialized format changes so old entries are ignored.
_CACHE_VERSION = "v2"

# File extensions delivered alongside SKILL.md. Only text types that agents can read.
ALLOWED_EXTENSIONS = frozenset({
    ".md", ".txt", ".sh", ".py", ".json", ".yaml", ".yml", ".toml",
    ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts",
    ".svg", ".mmd", ".excalidraw", ".csv",
})


@dataclass(frozen=True)
class SkillSource:
    """One entry from ``skills.sources`` in WORKFLOW.md."""
    repo: str
    path: str = "skills"
    ref: str | None = None
    roles: tuple[str, ...] = ()  # empty → every role
    repos: tuple[str, ...] = ()  # empty → global (every repository)

    def as_entry(self) -> dict:
        """The WORKFLOW.md entry this source was parsed from (unset fields omitted)."""
        entry: dict = {"repo": self.repo, "path": self.path}
        if self.ref:
            entry["ref"] = self.ref
        if self.roles:
            entry["roles"] = list(self.roles)
        if self.repos:
            entry["repos"] = list(self.repos)
        return entry

    def applies_to(self, role: str, target_repo: str | None) -> bool:
        """Does this source apply to the given role and target repository?"""
        if self.roles and role not in self.roles:
            return False
        if self.repos and target_repo:
            return target_repo.lower() in {r.lower() for r in self.repos}
        return True

    @property
    def is_global(self) -> bool:
        return len(self.repos) == 0


@dataclass
class SkillFile:
    """One file inside a skill directory."""
    path: str           # relative to the skill folder, e.g. "SKILL.md" or "templates/deck.html"
    content: str        # UTF-8 text
    executable: bool = False  # tar member had the execute bit


@dataclass
class ResolvedSkill:
    """One skill directory extracted from a source."""
    name: str
    content: str                            # the SKILL.md text (convenience accessor)
    files: dict[str, SkillFile] = field(default_factory=dict)
    # files maps relative path → SkillFile; always includes "SKILL.md"

    def __post_init__(self) -> None:
        if "SKILL.md" not in self.files:
            self.files["SKILL.md"] = SkillFile(path="SKILL.md", content=self.content)


@dataclass
class ResolvedSource:
    """A fetched source: its SHA and the skills it contains."""
    source: SkillSource
    sha: str
    skills: list[ResolvedSkill] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# ref → SHA resolution with caching
# ---------------------------------------------------------------------------

def _cache_key(ns: str, repo: str, ref: str) -> str:
    return f"{_CACHE_PREFIX.format(ns=ns)}:{repo}:{ref}"


def _resolve_ref(
    repo: str,
    ref: str | None,
    token: str,
    api_url: str,
    cache_client=None,
    cache_ns: str = "",
    ref_ttl_s: int = DEFAULT_REF_TTL_S,
) -> str | None:
    """Resolve a ref to a SHA. Returns None on failure."""
    import httpx

    # Check cache first.
    if cache_client is not None and ref is not None:
        key = _cache_key(cache_ns, repo, ref)
        cached = cache_client.get(key)
        if cached:
            return cached

    # Fetch from the API.
    url = f"{api_url}/repos/{repo}/commits/{ref or 'HEAD'}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.sha",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code != 200:
            log.warning("skill_ref_resolve_failed", repo=repo, ref=ref, status=resp.status_code)
            return None
        sha = resp.text.strip()
    except Exception as exc:
        log.warning("skill_ref_resolve_error", repo=repo, ref=ref, error=str(exc))
        return None

    # Cache the mapping.
    if cache_client is not None and ref is not None:
        key = _cache_key(cache_ns, repo, ref)
        try:
            cache_client.setex(key, ref_ttl_s, sha)
        except Exception:
            pass  # best-effort caching
    return sha


# ---------------------------------------------------------------------------
# Tarball fetching and extraction
# ---------------------------------------------------------------------------

def _fetch_tarball(
    repo: str,
    sha: str,
    token: str,
    api_url: str,
    path_prefix: str,
) -> list[ResolvedSkill]:
    """Download the repo tarball at `sha` and extract skills from `path_prefix`."""
    import httpx

    url = f"{api_url}/repos/{repo}/tarball/{sha}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=60.0, follow_redirects=True)
        if resp.status_code != 200:
            log.warning("skill_tarball_failed", repo=repo, sha=sha, status=resp.status_code)
            return []
    except Exception as exc:
        log.warning("skill_tarball_error", repo=repo, sha=sha, error=str(exc))
        return []

    return _extract_skills(resp.content, path_prefix)


def _has_allowed_ext(filename: str) -> bool:
    """True when the file name ends with one of the allowed text extensions."""
    dot = filename.rfind(".")
    return dot >= 0 and filename[dot:].lower() in ALLOWED_EXTENSIONS


def _safe_relpath(parts: list[str]) -> bool:
    """Reject `..`, absolute components and hidden components (starting with `.`)."""
    for p in parts:
        if not p or p == ".." or p.startswith(".") or p.startswith("/"):
            return False
    return True


def _extract_skills(tarball_bytes: bytes, path_prefix: str) -> list[ResolvedSkill]:
    """Extract skill directories from a tarball, respecting size limits.

    A skill is a folder under ``<prefix>/`` that contains ``SKILL.md``.
    Every regular text file (by extension allowlist) under the skill folder at any
    depth is included. Symlinks, hardlinks, devices, binary files, hidden components
    and paths with ``..`` are skipped. The executable bit (mode & 0o111) is remembered.
    """
    # First pass: collect all candidate file entries grouped by skill name.
    prefix = path_prefix.rstrip("/")

    # skill_name -> list of (relpath_within_skill, TarInfo)
    candidates: dict[str, list[tuple[str, tarfile.TarInfo]]] = {}

    try:
        tar = tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        log.warning("skill_tarball_extract_error", error=str(exc))
        return []

    try:
        for member in tar.getmembers():
            # Only regular files; skip symlinks, hardlinks, devices.
            if not member.isfile() or member.issym() or member.islnk():
                continue

            if member.name.startswith("/") or "\\" in member.name:
                continue
            parts = member.name.split("/")
            if len(parts) < 3 or ".." in parts:
                continue
            # Remove the top-level GitHub directory (owner-repo-sha).
            rel_parts = parts[1:]
            rel = "/".join(rel_parts)

            # Must be under <prefix>/<skill_name>/...
            if prefix:
                expected_start = prefix + "/"
                if not rel.startswith(expected_start):
                    continue
                after_prefix = rel[len(expected_start):]
            else:
                after_prefix = rel

            sub_parts = after_prefix.split("/")
            if len(sub_parts) < 2:
                continue

            skill_name = sub_parts[0]
            file_parts = sub_parts[1:]  # path within the skill folder

            if not skill_name or not _safe_relpath([skill_name] + file_parts):
                continue

            file_relpath = "/".join(file_parts)

            # Check extension allowlist (SKILL.md is .md so it passes).
            if not _has_allowed_ext(file_relpath):
                continue

            # Per-file size check.
            if member.size > MAX_SKILL_FILE_BYTES:
                log.info("skill_file_too_large", skill=skill_name, file=file_relpath,
                         size=member.size)
                continue

            candidates.setdefault(skill_name, []).append((file_relpath, member))

        # Second pass: build ResolvedSkill for each folder that has SKILL.md.
        skills: list[ResolvedSkill] = []
        total_bytes = 0

        for skill_name in sorted(candidates):
            entries = candidates[skill_name]
            # A skill must have SKILL.md.
            has_skill_md = any(rp == "SKILL.md" for rp, _ in entries)
            if not has_skill_md:
                continue

            if len(skills) >= MAX_SKILLS_PER_SOURCE:
                log.info("skill_source_count_exceeded")
                break

            files: dict[str, SkillFile] = {}
            skill_content = ""
            skill_ok = True

            for file_relpath, member in entries:
                if total_bytes + member.size > MAX_SOURCE_BYTES:
                    log.info("skill_source_budget_exceeded", skill=skill_name)
                    skill_ok = False
                    break

                f = tar.extractfile(member)
                if f is None:
                    continue
                raw = f.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    # Non-UTF-8 file: skip silently.
                    continue

                total_bytes += len(raw)
                executable = bool(member.mode & 0o111)
                sf = SkillFile(path=file_relpath, content=text, executable=executable)
                files[file_relpath] = sf
                if file_relpath == "SKILL.md":
                    skill_content = text

            if not skill_ok:
                break

            if "SKILL.md" not in files:
                continue

            skills.append(ResolvedSkill(
                name=skill_name, content=skill_content, files=files,
            ))

        return skills
    except (tarfile.TarError, OSError) as exc:
        log.warning("skill_tarball_extract_error", error=str(exc))
        return []
    finally:
        tar.close()


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def _tarball_cache_key(ns: str, repo: str, sha: str, path: str) -> str:
    # One repository can hold several sources (one per path): the path is part of the key.
    return f"{_CACHE_PREFIX.format(ns=ns)}:tarball:{_CACHE_VERSION}:{repo}:{sha}:{path}"


def _serialize_skill(skill: ResolvedSkill) -> dict:
    return {
        "name": skill.name,
        "content": skill.content,
        "files": {
            path: {"content": sf.content, "executable": sf.executable}
            for path, sf in skill.files.items()
        },
    }


def _deserialize_skill(data: dict) -> ResolvedSkill:
    files_raw = data.get("files")
    if isinstance(files_raw, dict):
        files = {
            path: SkillFile(path=path, content=info["content"],
                            executable=info.get("executable", False))
            for path, info in files_raw.items()
        }
    else:
        # Old v1 cache entry (no files dict): skip it.
        raise ValueError("old cache format")
    return ResolvedSkill(name=data["name"], content=data.get("content", ""), files=files)


def resolve_sources(
    sources: Sequence[SkillSource],
    role: str,
    target_repo: str | None,
    environ: Mapping[str, str],
    now_ms_fn,
    *,
    cache_client=None,
    cache_ns: str = "",
    api_url: str | None = None,
) -> list[ResolvedSource]:
    """Resolve the applicable sources for a (role, target_repo) pair.

    Returns one ``ResolvedSource`` per applicable source, in order. Skills from the
    target repository itself are not included here (they load natively).
    """
    from .endpoints import github_api_url
    api = api_url or github_api_url()

    applicable = [s for s in sources if s.applies_to(role, target_repo)]
    results: list[ResolvedSource] = []

    for source in applicable:
        # Skip if the source IS the target repo (the repo's own skills load natively).
        if target_repo and source.repo.lower() == target_repo.lower():
            continue

        try:
            cred = ghapp.InstallationCredential.from_env(
                environ, now_ms_fn,
                target_repos=(source.repo,),
                permissions=ghapp.READ_ONLY,
            )
            token = cred.token()
        except Exception as exc:
            log.warning("skill_credential_failed", repo=source.repo, error=str(exc))
            results.append(ResolvedSource(source=source, sha="", error=str(exc)))
            continue

        sha = _resolve_ref(
            source.repo, source.ref, token, api,
            cache_client=cache_client, cache_ns=cache_ns,
        )
        if sha is None:
            results.append(ResolvedSource(
                source=source, sha="", error="ref resolution failed",
            ))
            continue

        # Check if we already have this sha cached (tarball not needed).
        tk = _tarball_cache_key(cache_ns, source.repo, sha, source.path)
        cached_skills = None
        if cache_client is not None:
            try:
                raw = cache_client.get(tk)
                if raw:
                    cached_skills = [_deserialize_skill(s) for s in json.loads(raw)]
            except Exception:
                pass  # old format or corrupt: re-fetch

        if cached_skills is not None:
            results.append(ResolvedSource(source=source, sha=sha, skills=cached_skills))
            continue

        skills = _fetch_tarball(source.repo, sha, token, api, source.path)

        # Cache the extracted skills.
        if cache_client is not None and skills:
            try:
                payload = json.dumps(
                    [_serialize_skill(s) for s in skills],
                    ensure_ascii=False,
                )
                # Cache for a longer period — the SHA is immutable.
                cache_client.setex(tk, 3600, payload)
            except Exception:
                pass

        results.append(ResolvedSource(source=source, sha=sha, skills=skills))

    return results


# ---------------------------------------------------------------------------
# Deduplication and precedence
# ---------------------------------------------------------------------------

def deduplicate(
    resolved: list[ResolvedSource],
    target_repo: str | None,
) -> dict[str, ResolvedSkill]:
    """Apply precedence: repo-specific sources beat global ones; earlier sources beat
    later ones within the same scope. Returns a name→skill map."""
    # Phase 1: collect repo-specific skills.
    specific: dict[str, ResolvedSkill] = {}
    for rs in resolved:
        if not rs.source.is_global:
            for skill in rs.skills:
                specific.setdefault(skill.name, skill)

    # Phase 2: collect global skills (only names not already claimed).
    result = dict(specific)
    for rs in resolved:
        if rs.source.is_global:
            for skill in rs.skills:
                result.setdefault(skill.name, skill)

    return result


# ---------------------------------------------------------------------------
# Staging — workspace pod (implementer)
# ---------------------------------------------------------------------------

# omp reads user skills from ~/.omp/agent/skills/*/SKILL.md. The workspace pod's
# HOME depends on the base image (typically /root for root, /home/<user> otherwise).
# We resolve HOME inside the exec script so the path is always correct.
_USER_SKILLS_SUBDIR = ".omp/agent/skills"

# One staging script carries at most this many bytes. It travels on the exec's stdin, never
# in argv (Linux caps a single exec argument at 128 KiB), so the cap only bounds one write.
_BATCH_LIMIT = 1024 * 1024
_HEREDOC_END = "SYMPHONY_SKILL_EOF"


def _file_block(skill_dir: str, sf: SkillFile) -> str:
    """Shell that writes one file from a base64 here-doc (base64 never holds the marker)."""
    dest = f"{skill_dir}/{sf.path}"
    encoded = base64.encodebytes(sf.content.encode("utf-8")).decode("ascii")
    mode = "755" if sf.executable else "644"
    return (
        f"mkdir -p {shlex.quote(os.path.dirname(dest))}\n"
        f"base64 -d > {shlex.quote(dest)} <<'{_HEREDOC_END}'\n{encoded}{_HEREDOC_END}\n"
        f"chmod {mode} {shlex.quote(dest)}\n"
    )


def stage_skills_workspace(
    sandbox,
    skills: dict[str, ResolvedSkill],
) -> None:
    """Write skills into the workspace pod's user skills directory, replacing any
    previously staged central skills. Every file of each skill folder is written with its
    executable bit. Files travel in as few execs as the batch limit allows, on stdin."""
    skills_dir = f"$HOME/{_USER_SKILLS_SUBDIR}"
    if not skills:
        sandbox.exec_script(f'rm -rf "{skills_dir}"', timeout_s=30)
        return

    sandbox.exec_script(f'rm -rf "{skills_dir}" && mkdir -p "{skills_dir}"', timeout_s=30)

    # Absolute paths for the here-doc targets: the HOME the engine will see.
    code, resolved_home = sandbox.exec_script('echo -n "$HOME"', timeout_s=10)
    home = resolved_home.strip() if code == 0 and resolved_home.strip() else "/root"
    abs_skills_dir = f"{home}/{_USER_SKILLS_SUBDIR}"

    batches: list[str] = []
    current = ""
    for name, skill in skills.items():
        for sf in skill.files.values():
            block = _file_block(f"{abs_skills_dir}/{name}", sf)
            if current and len(current) + len(block) > _BATCH_LIMIT:
                batches.append(current)
                current = ""
            current += block
    if current:
        batches.append(current)

    for script in batches:
        size = len(script.encode("utf-8"))
        code, output = sandbox.exec_script(
            f"head -c {size} | bash -e", timeout_s=60, stdin=script,
        )
        if code != 0:
            log.warning("skill_workspace_write_failed", exit_code=code, output=output[-300:])


# ---------------------------------------------------------------------------
# Staging — local agent (merger, assistant)
# ---------------------------------------------------------------------------

def stage_skills_local(
    skills_dir: str,
    skills: dict[str, ResolvedSkill],
) -> None:
    """Write skills into a local directory for a merger or assistant agent.
    The agent reads ``PI_CODING_AGENT_DIR/skills/*/SKILL.md``. Every file from
    each skill's folder is written, with the executable bit preserved."""
    import shutil

    target = os.path.join(skills_dir, "skills")
    if os.path.exists(target):
        shutil.rmtree(target)

    if not skills:
        return

    os.makedirs(target, exist_ok=True)
    for name, skill in skills.items():
        for relpath, sf in skill.files.items():
            full_path = os.path.join(target, name, relpath)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            mode = 0o755 if sf.executable else 0o644
            fd = os.open(full_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(sf.content)
            os.chmod(full_path, mode)


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------

def skills_meta(resolved: list[ResolvedSource]) -> list[dict]:
    """The ``skills`` field for session metadata."""
    return [
        {
            "source": rs.source.repo,
            "sha": rs.sha,
            "names": [s.name for s in rs.skills],
            "files": sum(len(s.files) for s in rs.skills),
        }
        for rs in resolved
        if rs.skills
    ]
