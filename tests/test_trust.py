"""Repository knowledge written by the reflector must be discoverable by Oh My Pi."""

import pytest

from symphony import reflector


def test_reflector_writes_skills_to_the_path_the_engine_discovers():
    """FR-027. The engine scans `.agents/skills/<slug>/SKILL.md` from the working
    directory up to the git root. A loose `.md` at the skills root would be IGNORED, so
    the per-slug directory shape is load-bearing."""
    lesson = reflector.Lesson(
        title="How to land a PR", description="steps", content="do the thing", kind="skill-proposal"
    )
    path, body = reflector.render_skill(lesson)
    assert path.startswith(".agents/skills/")
    assert path.endswith("/SKILL.md")
    assert path.count("/") == 3, "must be .agents/skills/<slug>/SKILL.md, not a loose file"
    assert "name:" in body and "description:" in body


def test_skill_frontmatter_emits_the_description_field():
    lesson = reflector.Lesson(title="Some lesson", description="d", content="body")
    _, rendered = reflector.render_skill(lesson)
    assert "description: d" in rendered.split("---")[1]


@pytest.mark.parametrize("description", ["", "   "])
def test_curation_rejects_a_lesson_with_no_description(description):
    """A skill proposal without a description would be merged and then silently dropped
    by the engine, which requires a non-empty `description` in the front matter."""
    lesson = reflector.Lesson(
        title="Some lesson", description=description, content="body", kind="skill-proposal"
    )
    curation = reflector.curate([lesson], [], budget_lines=100, usage={})
    assert curation.additions == []
    assert curation.replacements == []
    assert curation.skipped == ["some-lesson"]


def test_curation_rejects_an_empty_description_even_as_a_replacement():
    """An update to an existing lesson goes through the same gate: a replacement with no
    description would clobber a valid lesson with an unloadable one."""
    existing = reflector.Lesson(title="Some lesson", description="d", content="old")
    update = reflector.Lesson(title="Some lesson", description="", content="new")
    curation = reflector.curate([update], [existing], budget_lines=100, usage={})
    assert curation.replacements == [] and curation.additions == []


def test_skill_slug_matches_the_engines_accepted_charset():
    """name must be lowercase a-z, 0-9 and hyphens; violations are warnings today but
    would break portability to stricter harnesses."""
    import re

    lesson = reflector.Lesson(title="Ação: Deploy & Rollback (v2)!", description="d", content="c")
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", lesson.slug), lesson.slug
