"""Tests for skills: config validation, resolution, precedence, staging, and the API route."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import time

import pytest

from symphony.config import ConfigError, parse_workflow
from symphony.skills import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILLS_PER_SOURCE,
    MAX_SOURCE_BYTES,
    ResolvedSkill,
    ResolvedSource,
    SkillFile,
    SkillSource,
    _extract_skills,
    deduplicate,
    skills_meta,
    stage_skills_local,
    stage_skills_workspace,
)

# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

MINIMAL = """---
tracker:
  kind: memory
---
body
"""


def _with_skills(skills_yaml: str) -> str:
    return f"""---
tracker:
  kind: memory
{skills_yaml}
---
body
"""


class TestConfigValidation:
    def test_no_skills_section(self):
        cfg = parse_workflow(MINIMAL)
        assert cfg.skill_sources == ()

    def test_valid_global_source(self):
        cfg = parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/agent-skills
      path: skills
      ref: main
"""))
        assert len(cfg.skill_sources) == 1
        src = cfg.skill_sources[0]
        assert src.repo == "acme/agent-skills"
        assert src.path == "skills"
        assert src.ref == "main"
        assert src.roles == ()
        assert src.repos == ()
        assert src.is_global

    def test_valid_repo_specific_source(self):
        cfg = parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/agent-skills
      roles: [implementer, reviewer]
      repos: [acme/app, acme/lib]
"""))
        src = cfg.skill_sources[0]
        assert src.roles == ("implementer", "reviewer")
        assert src.repos == ("acme/app", "acme/lib")
        assert not src.is_global

    def test_multiple_sources(self):
        cfg = parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills-a
    - repo: acme/skills-b
      path: custom
      ref: v2
"""))
        assert len(cfg.skill_sources) == 2
        assert cfg.skill_sources[0].repo == "acme/skills-a"
        assert cfg.skill_sources[1].path == "custom"
        assert cfg.skill_sources[1].ref == "v2"

    def test_bad_repo_format(self):
        with pytest.raises(ConfigError, match="owner/name"):
            parse_workflow(_with_skills("""
skills:
  sources:
    - repo: not-a-repo
"""))

    def test_bad_role(self):
        with pytest.raises(ConfigError, match="unknown roles"):
            parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills
      roles: [nonexistent]
"""))

    def test_bad_repos_format(self):
        with pytest.raises(ConfigError, match="owner/name"):
            parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills
      repos: [not-valid]
"""))

    def test_sources_not_a_list(self):
        with pytest.raises(ConfigError, match="must be a list"):
            parse_workflow(_with_skills("""
skills:
  sources: not-a-list
"""))

    def test_unknown_skills_key(self):
        with pytest.raises(ConfigError, match="unknown keys"):
            parse_workflow(_with_skills("""
skills:
  sources: []
  bad_key: 1
"""))

    def test_unknown_source_key(self):
        with pytest.raises(ConfigError, match="unknown keys"):
            parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills
      unknown: true
"""))

    def test_path_with_dotdot(self):
        with pytest.raises(ConfigError, match="relative path"):
            parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills
      path: ../escape
"""))

    def test_absolute_path(self):
        with pytest.raises(ConfigError, match="relative path"):
            parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills
      path: /absolute
"""))

    def test_empty_skills_section_ok(self):
        cfg = parse_workflow(_with_skills("skills: {}"))
        assert cfg.skill_sources == ()

    def test_null_skills_section_ok(self):
        cfg = parse_workflow(_with_skills("skills:"))
        assert cfg.skill_sources == ()

    def test_all_valid_roles(self):
        """Every role from teamconfig.ROLES is accepted."""
        cfg = parse_workflow(_with_skills("""
skills:
  sources:
    - repo: acme/skills
      roles: [implementer, reviewer, oracle, arbiter, developer, reflector, probe, assistant, researcher, planner]
"""))
        assert len(cfg.skill_sources[0].roles) == 10


# ---------------------------------------------------------------------------
# Tarball extraction
# ---------------------------------------------------------------------------

def _make_tarball(files: dict, *, links: dict[str, str] | None = None,
                  modes: dict[str, int] | None = None) -> bytes:
    """A gzipped tarball shaped like GitHub's (every path under `owner-repo-sha/`).
    `files` maps a path to text or bytes; `links` adds symlinks (path → target); `modes`
    sets a member's mode (default 0o644)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = (modes or {}).get(name, 0o644)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name=name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buf.getvalue()


R = "owner-repo-abc123"


class TestTarballExtraction:
    def test_basic_extraction(self):
        tarball = _make_tarball({
            f"{R}/skills/my-skill/SKILL.md": "# My Skill\nDo things.",
            f"{R}/skills/other/SKILL.md": "# Other\nMore.",
            f"{R}/skills/other/extra.txt": "a supporting note",
            f"{R}/README.md": "not a skill",
        })
        skills = {s.name: s for s in _extract_skills(tarball, "skills")}
        assert set(skills) == {"my-skill", "other"}
        assert skills["my-skill"].content.startswith("# My Skill")
        assert set(skills["other"].files) == {"SKILL.md", "extra.txt"}

    def test_nested_path(self):
        tarball = _make_tarball({f"{R}/deep/path/skill-a/SKILL.md": "content a"})
        skills = _extract_skills(tarball, "deep/path")
        assert [s.name for s in skills] == ["skill-a"]

    def test_empty_path_prefix(self):
        tarball = _make_tarball({f"{R}/my-skill/SKILL.md": "content"})
        assert [s.name for s in _extract_skills(tarball, "")] == ["my-skill"]

    def test_folder_without_its_own_skill_md_is_not_a_skill(self):
        tarball = _make_tarball({
            f"{R}/skills/a/b/SKILL.md": "nested only",
            f"{R}/skills/c/notes.md": "no SKILL.md",
        })
        assert _extract_skills(tarball, "skills") == []

    def test_hidden_dirs_skipped(self):
        tarball = _make_tarball({f"{R}/skills/.hidden/SKILL.md": "hidden"})
        assert _extract_skills(tarball, "skills") == []

    def test_file_size_limit(self):
        tarball = _make_tarball({
            f"{R}/skills/big/SKILL.md": "x" * (MAX_SKILL_FILE_BYTES + 1),
            f"{R}/skills/small/SKILL.md": "ok",
        })
        assert [s.name for s in _extract_skills(tarball, "skills")] == ["small"]

    def test_whole_folder_with_only_allowed_text_files(self):
        """SKILL.md plus templates, an executable script and a nested dir arrive; a binary,
        a symlink, a `../` path, a hidden file, a non-UTF-8 file and an oversized file do not."""
        base = f"{R}/skills/deck"
        tarball = _make_tarball(
            {
                f"{base}/SKILL.md": "# Deck\nRun scripts/render.mjs.",
                f"{base}/templates/deck.html": "<html></html>",
                f"{base}/scripts/render.mjs": "#!/usr/bin/env node\n",
                f"{base}/assets/themes/dark/theme.css": "body{}",
                f"{base}/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
                f"{base}/../escape.md": "outside",
                f"{base}/.secret/notes.md": "hidden",
                f"{base}/latin1.txt": "caf\xe9".encode("latin-1"),
                f"{base}/huge.md": "y" * (MAX_SKILL_FILE_BYTES + 1),
            },
            links={f"{base}/link.md": "/etc/passwd"},
            modes={f"{base}/scripts/render.mjs": 0o755},
        )
        [skill] = _extract_skills(tarball, "skills")
        assert skill.name == "deck"
        assert set(skill.files) == {
            "SKILL.md", "templates/deck.html", "scripts/render.mjs",
            "assets/themes/dark/theme.css",
        }
        assert skill.files["scripts/render.mjs"].executable
        assert not skill.files["templates/deck.html"].executable
        assert skill.files["templates/deck.html"].content == "<html></html>"

    def test_source_byte_budget_counts_every_file(self):
        """MAX_SOURCE_BYTES covers supporting files too: a skill that would cross it is
        not delivered, and extraction stops there."""
        chunk = "z" * (MAX_SKILL_FILE_BYTES - 1)
        files = {f"{R}/skills/a-first/SKILL.md": "first"}
        for i in range(MAX_SOURCE_BYTES // MAX_SKILL_FILE_BYTES + 2):
            files[f"{R}/skills/b-heavy/part{i:02d}.md"] = chunk
        files[f"{R}/skills/b-heavy/SKILL.md"] = "heavy"
        files[f"{R}/skills/c-later/SKILL.md"] = "later"
        skills = _extract_skills(_make_tarball(files), "skills")
        assert [s.name for s in skills] == ["a-first"]

    def test_skill_count_limit(self):
        files = {f"{R}/skills/s{i:03d}/SKILL.md": "x" for i in range(MAX_SKILLS_PER_SOURCE + 5)}
        skills = _extract_skills(_make_tarball(files), "skills")
        assert len(skills) == MAX_SKILLS_PER_SOURCE


# ---------------------------------------------------------------------------
# Applies_to and precedence
# ---------------------------------------------------------------------------

class TestAppliesTo:
    def test_global_source_applies_to_all(self):
        src = SkillSource(repo="acme/skills")
        assert src.applies_to("implementer", "acme/app")
        assert src.applies_to("reviewer", "acme/lib")
        assert src.applies_to("assistant", None)

    def test_role_filter(self):
        src = SkillSource(repo="acme/skills", roles=("implementer",))
        assert src.applies_to("implementer", "acme/app")
        assert not src.applies_to("reviewer", "acme/app")

    def test_repo_filter(self):
        src = SkillSource(repo="acme/skills", repos=("acme/app",))
        assert src.applies_to("implementer", "acme/app")
        assert not src.applies_to("implementer", "acme/other")

    def test_both_filters(self):
        src = SkillSource(repo="acme/skills", roles=("reviewer",), repos=("acme/app",))
        assert src.applies_to("reviewer", "acme/app")
        assert not src.applies_to("implementer", "acme/app")
        assert not src.applies_to("reviewer", "acme/other")

    def test_case_insensitive_repo(self):
        src = SkillSource(repo="acme/skills", repos=("ACME/App",))
        assert src.applies_to("implementer", "acme/app")


class TestDeduplicate:
    def test_specific_beats_global(self):
        specific = ResolvedSource(
            source=SkillSource(repo="acme/specific", repos=("acme/app",)),
            sha="aaa",
            skills=[ResolvedSkill(name="shared", content="specific version")],
        )
        global_src = ResolvedSource(
            source=SkillSource(repo="acme/global"),
            sha="bbb",
            skills=[ResolvedSkill(name="shared", content="global version")],
        )
        result = deduplicate([specific, global_src], "acme/app")
        assert result["shared"].content == "specific version"

    def test_first_source_wins_within_scope(self):
        first = ResolvedSource(
            source=SkillSource(repo="acme/first"),
            sha="aaa",
            skills=[ResolvedSkill(name="overlap", content="first")],
        )
        second = ResolvedSource(
            source=SkillSource(repo="acme/second"),
            sha="bbb",
            skills=[ResolvedSkill(name="overlap", content="second")],
        )
        result = deduplicate([first, second], None)
        assert result["overlap"].content == "first"

    def test_no_collision(self):
        a = ResolvedSource(
            source=SkillSource(repo="acme/a"),
            sha="aaa",
            skills=[ResolvedSkill(name="skill-a", content="a")],
        )
        b = ResolvedSource(
            source=SkillSource(repo="acme/b"),
            sha="bbb",
            skills=[ResolvedSkill(name="skill-b", content="b")],
        )
        result = deduplicate([a, b], None)
        assert set(result.keys()) == {"skill-a", "skill-b"}


# ---------------------------------------------------------------------------
# Staging — local
# ---------------------------------------------------------------------------

class TestStageSkillsLocal:
    def test_writes_skills(self):
        with tempfile.TemporaryDirectory() as agent_dir:
            skills = {
                "my-skill": ResolvedSkill(name="my-skill", content="# My Skill"),
                "other": ResolvedSkill(name="other", content="# Other"),
            }
            stage_skills_local(agent_dir, skills)
            path = os.path.join(agent_dir, "skills", "my-skill", "SKILL.md")
            assert os.path.isfile(path)
            with open(path) as f:
                assert f.read() == "# My Skill"
            path2 = os.path.join(agent_dir, "skills", "other", "SKILL.md")
            assert os.path.isfile(path2)

    def test_clears_old_skills(self):
        with tempfile.TemporaryDirectory() as agent_dir:
            skills_dir = os.path.join(agent_dir, "skills", "old-skill")
            os.makedirs(skills_dir)
            with open(os.path.join(skills_dir, "SKILL.md"), "w") as f:
                f.write("old")
            # Stage new skills — old ones should be gone
            stage_skills_local(agent_dir, {
                "new-skill": ResolvedSkill(name="new-skill", content="new"),
            })
            assert not os.path.exists(os.path.join(agent_dir, "skills", "old-skill"))
            assert os.path.isfile(os.path.join(agent_dir, "skills", "new-skill", "SKILL.md"))

    def test_empty_skills_clears_dir(self):
        with tempfile.TemporaryDirectory() as agent_dir:
            skills_dir = os.path.join(agent_dir, "skills", "old")
            os.makedirs(skills_dir)
            stage_skills_local(agent_dir, {})
            assert not os.path.exists(os.path.join(agent_dir, "skills"))

    def test_writes_whole_folder_with_modes(self, tmp_path):
        stage_skills_local(str(tmp_path), {"deck": _deck_skill()})
        root = tmp_path / "skills" / "deck"
        _assert_deck_tree(root)


def _deck_skill() -> ResolvedSkill:
    files = {
        "SKILL.md": SkillFile("SKILL.md", "# Deck"),
        "templates/deck.html": SkillFile("templates/deck.html", "<html>é</html>"),
        "scripts/render.mjs": SkillFile("scripts/render.mjs", "#!/bin/sh\necho ok\n",
                                        executable=True),
        "assets/themes/dark/theme.css": SkillFile("assets/themes/dark/theme.css", ""),
    }
    return ResolvedSkill(name="deck", content="# Deck", files=files)


def _assert_deck_tree(root) -> None:
    found = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert found == ["SKILL.md", "assets/themes/dark/theme.css", "scripts/render.mjs",
                     "templates/deck.html"]
    assert (root / "templates" / "deck.html").read_text(encoding="utf-8") == "<html>é</html>"
    assert (root / "assets/themes/dark/theme.css").read_text() == ""
    assert stat.S_IMODE((root / "scripts" / "render.mjs").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "templates" / "deck.html").stat().st_mode) == 0o644


# ---------------------------------------------------------------------------
# Staging — workspace pod
# ---------------------------------------------------------------------------

class BashSandbox:
    """A SandboxLike whose `exec_script` runs the script in a real bash with HOME pointing
    at a temporary directory — the PodSandbox contract (stdin carries payloads)."""

    def __init__(self, home: str):
        self.home = home
        self.execs = 0

    def exec_script(self, script: str, timeout_s: float | None = None, *,
                    stdin: str | None = None) -> tuple[int, str]:
        self.execs += 1
        done = subprocess.run(
            ["bash", "-c", f"{{ {script}\n}} 2>&1"], input=stdin or "", text=True,
            capture_output=True, env={"HOME": self.home, "PATH": os.environ["PATH"]},
            timeout=timeout_s or 60,
        )
        return done.returncode, done.stdout


class TestStageSkillsWorkspace:
    def test_writes_whole_folder_under_home_with_modes(self, tmp_path):
        sandbox = BashSandbox(str(tmp_path))
        stage_skills_workspace(sandbox, {"deck": _deck_skill()})
        _assert_deck_tree(tmp_path / ".omp" / "agent" / "skills" / "deck")

    def test_clears_stale_skills_from_a_previous_attempt(self, tmp_path):
        sandbox = BashSandbox(str(tmp_path))
        stage_skills_workspace(sandbox, {"old": ResolvedSkill(name="old", content="old")})
        stage_skills_workspace(sandbox, {"deck": _deck_skill()})
        skills_root = tmp_path / ".omp" / "agent" / "skills"
        assert sorted(p.name for p in skills_root.iterdir()) == ["deck"]

    def test_empty_set_removes_staged_skills(self, tmp_path):
        sandbox = BashSandbox(str(tmp_path))
        stage_skills_workspace(sandbox, {"deck": _deck_skill()})
        stage_skills_workspace(sandbox, {})
        assert not (tmp_path / ".omp" / "agent" / "skills").exists()

    def test_many_files_are_batched_not_one_exec_each(self, tmp_path):
        files = {f"refs/r{i:03d}.md": SkillFile(f"refs/r{i:03d}.md", f"ref {i}") for i in range(60)}
        skill = ResolvedSkill(name="big", content="# Big", files=files)
        sandbox = BashSandbox(str(tmp_path))
        stage_skills_workspace(sandbox, {"big": skill})
        root = tmp_path / ".omp" / "agent" / "skills" / "big"
        assert len(list((root / "refs").iterdir())) == 60
        assert (root / "SKILL.md").read_text() == "# Big"
        # wipe + HOME lookup + one batch
        assert sandbox.execs == 3


# ---------------------------------------------------------------------------
# Skills metadata
# ---------------------------------------------------------------------------

class TestSkillsMeta:
    def test_produces_meta(self):
        resolved = [
            ResolvedSource(
                source=SkillSource(repo="acme/skills"),
                sha="abc123",
                skills=[
                    ResolvedSkill(name="a", content="x"),
                    ResolvedSkill(name="b", content="y"),
                ],
            ),
            ResolvedSource(
                source=SkillSource(repo="acme/empty"),
                sha="def456",
                skills=[],
            ),
        ]
        meta = skills_meta(resolved)
        # Only sources with skills are included
        assert len(meta) == 1
        assert meta[0]["source"] == "acme/skills"
        assert meta[0]["sha"] == "abc123"
        assert meta[0]["names"] == ["a", "b"]
        assert meta[0]["files"] == 2  # one SKILL.md per skill


# ---------------------------------------------------------------------------
# Resolution with caching (mock HTTP)
# ---------------------------------------------------------------------------

class TestResolution:
    TARBALL_FILES = {
        "acme-skills-abc123/skills/deck/SKILL.md": "# Deck",
        "acme-skills-abc123/skills/deck/scripts/render.mjs": "#!/bin/sh\n",
    }
    TARBALL_MODES = {"acme-skills-abc123/skills/deck/scripts/render.mjs": 0o755}

    def _resolve_twice(self, cache: dict, *, seed: dict | None = None):
        """Resolve `acme/skills@main` twice against a GitHub stub; returns both results and
        the request counts."""
        import unittest.mock as mock

        import httpx

        from symphony.skills import resolve_sources

        tarball = _make_tarball(self.TARBALL_FILES, modes=self.TARBALL_MODES)
        calls = {"ref": 0, "tarball": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/commits/" in url:
                calls["ref"] += 1
                return httpx.Response(200, text="abc123def456")
            if "/tarball/" in url:
                calls["tarball"] += 1
                return httpx.Response(200, content=tarball)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def fake_get(url, **kwargs):
            return transport.handle_request(httpx.Request("GET", url, headers=kwargs.get("headers", {})))

        class FakeCache:
            def get(self, key):
                return cache.get(key)

            def setex(self, key, ttl, value):
                cache[key] = value

        cache.update(seed or {})
        sources = [SkillSource(repo="acme/skills", ref="main")]
        with mock.patch("httpx.get", side_effect=fake_get), \
                mock.patch("symphony.ghapp.InstallationCredential.from_env") as cred:
            cred.return_value.token.return_value = "test-token"
            results = [
                resolve_sources(sources, "implementer", "acme/app", {},
                                lambda: int(time.time() * 1000), cache_client=FakeCache(),
                                cache_ns="test", api_url="https://api.example.com")
                for _ in range(2)
            ]
        return results, calls

    def test_resolve_with_cache(self):
        """The second resolution makes no tarball request and keeps files and modes."""
        (first, second), calls = self._resolve_twice({})
        assert calls == {"ref": 1, "tarball": 1}
        for result in (first, second):
            [skill] = result[0].skills
            assert set(skill.files) == {"SKILL.md", "scripts/render.mjs"}
            assert skill.files["scripts/render.mjs"].executable

    def test_old_cache_format_is_not_reused(self):
        """An entry written before supporting files existed (SKILL.md only, unversioned key)
        is ignored: the source is fetched again and its whole folder delivered."""
        old = {
            "sym:test:skill-ref:acme/skills:main": "abc123def456",
            "sym:test:skill-ref:tarball:acme/skills:abc123def456":
                json.dumps([{"name": "deck", "content": "# Deck"}]),
        }
        (first, _), calls = self._resolve_twice({}, seed=old)
        assert calls["tarball"] == 1
        assert set(first[0].skills[0].files) == {"SKILL.md", "scripts/render.mjs"}

    def test_failed_source_is_logged_and_skipped(self):
        """A source that fails to fetch is skipped, never fails the run."""
        import unittest.mock as mock

        import httpx

        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        transport = httpx.MockTransport(mock_handler)

        def patched_get(url, **kwargs):
            kwargs.pop("follow_redirects", None)
            return transport.handle_request(httpx.Request("GET", url, headers=kwargs.get("headers", {})))

        env = {
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY": "",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }

        with mock.patch("httpx.get", side_effect=patched_get):
            with mock.patch("symphony.ghapp.InstallationCredential.from_env") as mock_cred:
                mock_instance = mock.MagicMock()
                mock_instance.token.return_value = "test-token"
                mock_cred.return_value = mock_instance

                from symphony.skills import resolve_sources

                sources = [SkillSource(repo="acme/broken", ref="main")]
                result = resolve_sources(
                    sources, "implementer", "acme/app", env,
                    lambda: int(time.time() * 1000),
                    api_url="https://api.example.com",
                )
                assert len(result) == 1
                assert result[0].error is not None
                assert result[0].skills == []

    def test_source_matching_target_repo_is_skipped(self):
        """The target repo's skills load natively; don't fetch them centrally."""
        import unittest.mock as mock

        env = {}
        with mock.patch("symphony.ghapp.InstallationCredential.from_env") as mock_cred:
            mock_instance = mock.MagicMock()
            mock_instance.token.return_value = "test-token"
            mock_cred.return_value = mock_instance

            from symphony.skills import resolve_sources

            sources = [SkillSource(repo="acme/app")]
            result = resolve_sources(
                sources, "implementer", "acme/app", env,
                lambda: int(time.time() * 1000),
                api_url="https://api.example.com",
            )
            # The source is skipped because it matches the target repo
            assert len(result) == 0


# ---------------------------------------------------------------------------
# Setup step (the API's skills status)
# ---------------------------------------------------------------------------

class TestSetupSkillsStep:
    """Tests the _skills_step method in the setup service."""

    def test_skipped_when_no_sources(self, redis_client):
        """When no skill sources are configured, the step is skipped."""
        from symphony.admin_commands import AdminStore
        from symphony.api.control import Control, Services
        from symphony.api.setup import SetupService
        from symphony.apitokens import TokenStore
        from symphony.sessions import SessionStore
        from symphony.teamconfig import TeamConfig

        ns = "test"
        sessions = SessionStore(redis_client, ns)
        teamconfig = TeamConfig(redis_client, ns)
        store = AdminStore(redis_client, ns)
        tokens = TokenStore(redis_client, ns)
        from unittest.mock import MagicMock

        from symphony.api.catalog import Catalog

        services = Services(
            client=redis_client, namespace=ns, sessions=sessions,
            tokens=tokens, teamconfig=teamconfig, store=store,
            kv=MagicMock(), reader=MagicMock(), catalog=Catalog(),
        )
        control = Control(services)
        svc = SetupService(control)

        step = svc._skills_step({})
        assert step["status"] == "skipped"
        assert step["detail"]["sources"] == []


# ---------------------------------------------------------------------------
# Merger AgentRunner skill_stager integration
# ---------------------------------------------------------------------------

class TestMergerSkillStager:
    """The merger's skill_stager writes skills into the agent dir for the target repo."""

    def test_skill_stager_writes_skills(self):
        """A skill_stager receives (agent_dir, role, meta) and writes skills there."""
        from symphony.skills import ResolvedSkill, stage_skills_local

        with tempfile.TemporaryDirectory() as agent_dir:
            skills = {
                "code-style": ResolvedSkill(name="code-style", content="# Code Style"),
            }
            stage_skills_local(agent_dir, skills)
            path = os.path.join(agent_dir, "skills", "code-style", "SKILL.md")
            assert os.path.isfile(path)
            with open(path) as f:
                assert "Code Style" in f.read()

    def test_skill_stager_per_role(self):
        """The skill_stager filters by role."""
        src_global = SkillSource(repo="acme/skills")
        src_reviewer = SkillSource(repo="acme/review-skills", roles=("reviewer",))

        # As implementer: only the global source applies
        assert src_global.applies_to("implementer", "acme/app")
        assert not src_reviewer.applies_to("implementer", "acme/app")

        # As reviewer: both apply
        assert src_global.applies_to("reviewer", "acme/app")
        assert src_reviewer.applies_to("reviewer", "acme/app")

    def test_skill_stager_per_repo(self):
        """Skills are scoped to the merger's target repository."""
        src_for_app = SkillSource(repo="acme/skills", repos=("acme/app",))
        src_for_lib = SkillSource(repo="acme/skills", repos=("acme/lib",))

        assert src_for_app.applies_to("reviewer", "acme/app")
        assert not src_for_app.applies_to("reviewer", "acme/lib")
        assert src_for_lib.applies_to("reviewer", "acme/lib")
        assert not src_for_lib.applies_to("reviewer", "acme/app")


# ---------------------------------------------------------------------------
# Assistant skill_stager integration
# ---------------------------------------------------------------------------

class TestAssistantSkillStager:
    """The assistant's skill_stager applies global sources and sources whose repos intersect
    the assistant's reachable repositories. Each role gets only its applicable skills."""

    def test_global_source_applies_to_all_assistant_roles(self):
        src = SkillSource(repo="acme/skills")
        for role in ("assistant", "researcher", "planner"):
            assert src.applies_to(role, None)

    def test_role_filtered_source_reaches_only_that_role(self):
        src = SkillSource(repo="acme/plan-skills", roles=("planner",))
        assert src.applies_to("planner", None)
        assert not src.applies_to("assistant", None)
        assert not src.applies_to("researcher", None)

    def test_repo_scoped_source_matches_reachable_repos(self):
        """A source with repos:[acme/app] applies when acme/app is reachable."""
        src = SkillSource(repo="acme/skills", repos=("acme/app",))
        # The assistant applies_to with each reachable repo
        assert src.applies_to("assistant", "acme/app")
        assert not src.applies_to("assistant", "acme/other")

    def test_stage_skills_local_for_assistant(self):
        """The assistant stages skills into PI_CODING_AGENT_DIR/skills/."""
        with tempfile.TemporaryDirectory() as agent_dir:
            skills = {
                "research-skill": ResolvedSkill(name="research-skill", content="# Research"),
            }
            stage_skills_local(agent_dir, skills)
            path = os.path.join(agent_dir, "skills", "research-skill", "SKILL.md")
            assert os.path.isfile(path)
            with open(path) as f:
                assert "Research" in f.read()
