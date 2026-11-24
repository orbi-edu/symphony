"""Central skill sources: fetch SKILL.md directories from configured repositories and stage
them into agent workspaces so every role gets the instructions its operator chose.

A skill source is a repository directory holding one sub-directory per skill, each with a
``SKILL.md`` file. Sources may be GLOBAL (every repository) or scoped to specific
repositories. The target repository's own ``.omp/skills/`` and ``.agents/skills/`` always
win; among central sources, repository-specific beats global, and earlier sources beat later
ones.

Fetching uses a GitHub App installation token scoped to the source repository with read-only
permissions. The ref→SHA resolution is cached (TTL configurable, default 10 min) so repeated
calls within a session do not re-download an unchanged source. A source that fails to fetch
is logged and skipped — it never fails a run.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class SkillSource:
    """One entry from ``skills.sources`` in WORKFLOW.md."""
    repo: str
    path: str = "skills"
    ref: str | None = None
    roles: tuple[str, ...] = ()  # empty → every role
    repos: tuple[str, ...] = ()  # empty → global (every repository)

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
class ResolvedSkill:
    """One skill directory extracted from a source."""
    name: str
    content: str  # the SKILL.md content


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


def _extract_skills(tarball_bytes: bytes, path_prefix: str) -> list[ResolvedSkill]:
    """Extract ``<prefix>/*/SKILL.md`` from a tarball, respecting size limits."""
    skills: list[ResolvedSkill] = []
    total_bytes = 0
    seen_names: set[str] = set()

    # Normalize: strip trailing slash.
    prefix = path_prefix.rstrip("/")

    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                # GitHub tarballs start with `<owner>-<repo>-<sha>/` prefix.
                # We need: `<anything>/<prefix>/<skill_name>/SKILL.md`
                parts = member.name.split("/")
                if len(parts) < 3:
                    continue
                # Remove the top-level GitHub directory.
                rel_parts = parts[1:]
                # Build relative path.
                rel = "/".join(rel_parts)

                # Must match: <prefix>/<skill_name>/SKILL.md
                if prefix:
                    expected_start = prefix + "/"
                    if not rel.startswith(expected_start):
                        continue
                    after_prefix = rel[len(expected_start):]
                else:
                    after_prefix = rel

                # Must be exactly one level deep: <skill_name>/SKILL.md
                sub_parts = after_prefix.split("/")
                if len(sub_parts) != 2 or sub_parts[1] != "SKILL.md":
                    continue

                skill_name = sub_parts[0]
                if not skill_name or skill_name.startswith("."):
                    continue
                if skill_name in seen_names:
                    continue

                # Size check.
                if member.size > MAX_SKILL_FILE_BYTES:
                    log.info("skill_file_too_large", name=skill_name, size=member.size)
                    continue
                if total_bytes + member.size > MAX_SOURCE_BYTES:
                    log.info("skill_source_budget_exceeded", name=skill_name)
                    break
                if len(skills) >= MAX_SKILLS_PER_SOURCE:
                    log.info("skill_source_count_exceeded")
                    break

                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8", errors="replace")
                total_bytes += len(content.encode("utf-8"))
                seen_names.add(skill_name)
                skills.append(ResolvedSkill(name=skill_name, content=content))
    except (tarfile.TarError, OSError) as exc:
        log.warning("skill_tarball_extract_error", error=str(exc))

    return skills


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

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
        tarball_cache_key = f"{_CACHE_PREFIX.format(ns=cache_ns)}:tarball:{source.repo}:{sha}"
        cached_skills = None
        if cache_client is not None:
            try:
                raw = cache_client.get(tarball_cache_key)
                if raw:
                    cached_skills = [
                        ResolvedSkill(name=s["name"], content=s["content"])
                        for s in json.loads(raw)
                    ]
            except Exception:
                pass

        if cached_skills is not None:
            results.append(ResolvedSource(source=source, sha=sha, skills=cached_skills))
            continue

        skills = _fetch_tarball(source.repo, sha, token, api, source.path)

        # Cache the extracted skills.
        if cache_client is not None and skills:
            try:
                payload = json.dumps(
                    [{"name": s.name, "content": s.content} for s in skills],
                    ensure_ascii=False,
                )
                # Cache for a longer period — the SHA is immutable.
                cache_client.setex(tarball_cache_key, 3600, payload)
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


def stage_skills_workspace(
    sandbox,
    skills: dict[str, ResolvedSkill],
) -> None:
    """Write skills into the workspace pod's user skills directory, replacing any
    previously staged central skills. The repo's own .omp/skills/ loads natively and
    is not touched."""
    # Resolve HOME inside the pod.
    skills_dir = f"$HOME/{_USER_SKILLS_SUBDIR}"
    if not skills:
        # Clear any previously staged central skills.
        sandbox.exec_script(
            f'rm -rf "{skills_dir}"',
            timeout_s=30,
        )
        return

    # Wipe and recreate the directory.
    sandbox.exec_script(
        f'rm -rf "{skills_dir}" && mkdir -p "{skills_dir}"',
        timeout_s=30,
    )

    # We need the actual resolved path for write_private_file (which needs an absolute path).
    code, resolved_home = sandbox.exec_script('echo -n "$HOME"', timeout_s=10)
    home = resolved_home.strip() if code == 0 and resolved_home.strip() else "/root"
    abs_skills_dir = f"{home}/{_USER_SKILLS_SUBDIR}"

    for name, skill in skills.items():
        skill_dir = f"{abs_skills_dir}/{name}"
        sd = shlex.quote(skill_dir)
        sandbox.exec_script(f"mkdir -p {sd}", timeout_s=15)
        sandbox.write_private_file(
            f"{skill_dir}/SKILL.md",
            skill.content,
        )


# ---------------------------------------------------------------------------
# Staging — local agent (merger, assistant)
# ---------------------------------------------------------------------------

def stage_skills_local(
    skills_dir: str,
    skills: dict[str, ResolvedSkill],
) -> None:
    """Write skills into a local directory for a merger or assistant agent.
    The agent reads ``PI_CODING_AGENT_DIR/skills/*/SKILL.md``."""
    import shutil

    target = os.path.join(skills_dir, "skills")
    if os.path.exists(target):
        shutil.rmtree(target)

    if not skills:
        return

    os.makedirs(target, exist_ok=True)
    for name, skill in skills.items():
        skill_path = os.path.join(target, name)
        os.makedirs(skill_path, exist_ok=True)
        fd = os.open(
            os.path.join(skill_path, "SKILL.md"),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(skill.content)


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
        }
        for rs in resolved
        if rs.skills
    ]
