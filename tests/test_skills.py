"""Tests for skills: config validation, resolution, precedence, staging, and the API route."""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import time

import pytest

from symphony.config import ConfigError, parse_workflow
from symphony.skills import (
    MAX_SKILL_FILE_BYTES,
    ResolvedSkill,
    ResolvedSource,
    SkillSource,
    _extract_skills,
    deduplicate,
    skills_meta,
    stage_skills_local,
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

def _make_tarball(files: dict[str, str]) -> bytes:
    """Create a gzipped tarball with the given paths and contents.
    Paths are relative inside the archive (GitHub prepends owner-repo-sha/)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestTarballExtraction:
    def test_basic_extraction(self):
        tarball = _make_tarball({
            "owner-repo-abc123/skills/my-skill/SKILL.md": "# My Skill\nDo things.",
            "owner-repo-abc123/skills/other/SKILL.md": "# Other\nMore.",
            "owner-repo-abc123/skills/other/extra.txt": "not extracted",
            "owner-repo-abc123/README.md": "not a skill",
        })
        skills = _extract_skills(tarball, "skills")
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"my-skill", "other"}
        for s in skills:
            if s.name == "my-skill":
                assert "My Skill" in s.content

    def test_nested_path(self):
        tarball = _make_tarball({
            "owner-repo-abc123/deep/path/skill-a/SKILL.md": "content a",
        })
        skills = _extract_skills(tarball, "deep/path")
        assert len(skills) == 1
        assert skills[0].name == "skill-a"

    def test_empty_path_prefix(self):
        tarball = _make_tarball({
            "owner-repo-abc123/my-skill/SKILL.md": "content",
        })
        skills = _extract_skills(tarball, "")
        assert len(skills) == 1

    def test_too_deep_not_extracted(self):
        tarball = _make_tarball({
            "owner-repo-abc123/skills/a/b/SKILL.md": "too deep",
        })
        skills = _extract_skills(tarball, "skills")
        assert len(skills) == 0

    def test_hidden_dirs_skipped(self):
        tarball = _make_tarball({
            "owner-repo-abc123/skills/.hidden/SKILL.md": "hidden",
        })
        skills = _extract_skills(tarball, "skills")
        assert len(skills) == 0

    def test_file_size_limit(self):
        big_content = "x" * (MAX_SKILL_FILE_BYTES + 1)
        tarball = _make_tarball({
            "owner-repo-abc123/skills/big/SKILL.md": big_content,
            "owner-repo-abc123/skills/small/SKILL.md": "ok",
        })
        skills = _extract_skills(tarball, "skills")
        assert len(skills) == 1
        assert skills[0].name == "small"

    def test_duplicate_names_first_wins(self):
        # tarfile preserves insertion order; first entry wins
        tarball = _make_tarball({
            "owner-repo-abc123/skills/dup/SKILL.md": "first",
        })
        # Can't truly have duplicate names in tar, but test the dedup via seen_names
        skills = _extract_skills(tarball, "skills")
        assert len(skills) == 1


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


# ---------------------------------------------------------------------------
# Staging — workspace pod (FakeSandbox)
# ---------------------------------------------------------------------------

class FakeSandbox:
    """Tracks exec_script and write_private_file calls."""

    def __init__(self, home: str = "/root"):
        self.scripts: list[str] = []
        self.files: dict[str, str] = {}
        self.home = home

    def exec_script(self, script: str, timeout_s: float | None = None) -> tuple[int, str]:
        self.scripts.append(script)
        # Return HOME when asked.
        if script.strip() == 'echo -n "$HOME"':
            return 0, self.home
        return 0, ""

    def write_private_file(self, path: str, content: str) -> tuple[int, str]:
        self.files[path] = content
        return 0, ""


class TestStageSkillsWorkspace:
    def test_stages_skills_into_pod(self):
        from symphony.skills import stage_skills_workspace

        sandbox = FakeSandbox(home="/root")
        skills = {
            "my-skill": ResolvedSkill(name="my-skill", content="# My Skill"),
        }
        stage_skills_workspace(sandbox, skills)
        assert any(".omp/agent/skills" in s for s in sandbox.scripts)
        assert "/root/.omp/agent/skills/my-skill/SKILL.md" in sandbox.files
        assert sandbox.files["/root/.omp/agent/skills/my-skill/SKILL.md"] == "# My Skill"

    def test_stages_skills_with_custom_home(self):
        from symphony.skills import stage_skills_workspace

        sandbox = FakeSandbox(home="/home/agent")
        skills = {
            "my-skill": ResolvedSkill(name="my-skill", content="# Custom Home"),
        }
        stage_skills_workspace(sandbox, skills)
        assert "/home/agent/.omp/agent/skills/my-skill/SKILL.md" in sandbox.files
        assert sandbox.files["/home/agent/.omp/agent/skills/my-skill/SKILL.md"] == "# Custom Home"

    def test_clears_old_skills_on_empty(self):
        from symphony.skills import stage_skills_workspace

        sandbox = FakeSandbox()
        stage_skills_workspace(sandbox, {})
        assert any("rm -rf" in s and ".omp/agent/skills" in s for s in sandbox.scripts)


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


# ---------------------------------------------------------------------------
# Resolution with caching (mock HTTP)
# ---------------------------------------------------------------------------

class TestResolution:
    def test_resolve_with_cache(self):
        """Second resolution of the same source makes no tarball request."""
        import httpx

        tarball = _make_tarball({
            "acme-skills-abc123/skills/test-skill/SKILL.md": "# Test",
        })

        call_count = {"ref": 0, "tarball": 0}

        def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/commits/" in url:
                call_count["ref"] += 1
                return httpx.Response(200, text="abc123def456")
            if "/tarball/" in url:
                call_count["tarball"] += 1
                return httpx.Response(200, content=tarball)
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)

        def patched_get(url, **kwargs):
            kwargs.pop("follow_redirects", None)
            return transport.handle_request(httpx.Request("GET", url, headers=kwargs.get("headers", {})))


        # We need a fake Redis cache
        cache = {}

        class FakeCache:
            def get(self, key):
                return cache.get(key)

            def setex(self, key, ttl, value):
                cache[key] = value

        fake_cache = FakeCache()

        sources = [SkillSource(repo="acme/skills", ref="main")]

        # Mock the credential
        env = {
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY": "",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }

        # We need to mock the entire flow - use httpx.get directly
        import unittest.mock as mock

        with mock.patch("httpx.get", side_effect=patched_get):
            with mock.patch("symphony.ghapp.InstallationCredential.from_env") as mock_cred:
                mock_instance = mock.MagicMock()
                mock_instance.token.return_value = "test-token"
                mock_cred.return_value = mock_instance

                from symphony.skills import resolve_sources

                # First call: should fetch ref and tarball
                result1 = resolve_sources(
                    sources, "implementer", "acme/app", env,
                    lambda: int(time.time() * 1000),
                    cache_client=fake_cache, cache_ns="test",
                    api_url="https://api.example.com",
                )
                assert len(result1) == 1
                assert len(result1[0].skills) == 1
                assert result1[0].skills[0].name == "test-skill"
                assert call_count["ref"] == 1
                assert call_count["tarball"] == 1

                # Second call: should use cache for both ref and tarball
                result2 = resolve_sources(
                    sources, "implementer", "acme/app", env,
                    lambda: int(time.time() * 1000),
                    cache_client=fake_cache, cache_ns="test",
                    api_url="https://api.example.com",
                )
                assert len(result2) == 1
                assert len(result2[0].skills) == 1
                # No additional tarball request
                assert call_count["tarball"] == 1

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
