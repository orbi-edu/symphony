"""FR-029: mechanical, deterministic curation — dedupe, contradiction replacement,
budget, decay; empty output is valid."""

from symphony.reflector import (
    CurationResult,
    Lesson,
    curate,
    parse_candidates,
    render_agents_md,
)


def lesson(title, content="do X; next action Y", **kw):
    kw.setdefault("description", "one line")
    return Lesson(title=title, content=content, **kw)


def test_dedupe_identical_lesson_is_skipped():
    existing = [lesson("Use lock in migrations")]
    result = curate([lesson("Use lock in migrations")], existing, 100, {})
    assert result.empty is False or result.skipped  # skip recorded
    assert result.additions == [] and result.replacements == []
    assert "use-lock-in-migrations" in result.skipped


def test_contradiction_replaces_never_coexists():
    existing = [lesson("Network timeout", content="use 5s")]
    new = lesson("Network timeout", content="use 30s — 5s kills slow CI")
    result = curate([new], existing, 100, {})
    assert result.additions == []
    assert len(result.replacements) == 1
    slug, replacement = result.replacements[0]
    assert slug == new.slug and replacement.content.startswith("use 30s")
    # the render never contains both versions
    text = render_agents_md(existing, result)
    assert "use 30s" in text and "use 5s\n" not in text


def test_budget_cuts_extra_additions():
    candidates = [lesson(f"Lesson number {i}") for i in range(50)]
    result = curate(candidates, [], budget_lines=30, usage={})
    assert len(result.additions) <= 10  # ~3 lines per lesson
    assert len(result.skipped) >= 40


def test_decay_removes_unused_lessons():
    existing = [lesson("Old and unused"), lesson("Always used")]
    result = curate(
        [],
        existing,
        100,
        usage={"always-used": 7},
        incorporations_since_use={"old-and-unused": 12, "always-used": 12},
    )
    assert result.removals == ["old-and-unused"]


def test_empty_curation_is_valid_and_preferred():
    result = curate([], [], 100, {})
    assert isinstance(result, CurationResult)
    assert result.empty


def test_invalid_candidates_are_skipped():
    result = curate([Lesson(title="", description="", content="")], [], 100, {})
    assert result.additions == []


def test_parse_candidates_tolerant_json():
    text = 'Analyzing...\n[{"title": "T", "description": "d", "content": "c", "globs": ["src/**"]}]\nend'
    lessons = parse_candidates(text, source="PR #7")
    assert len(lessons) == 1
    assert lessons[0].source == "PR #7"
    assert lessons[0].globs == ("src/**",)
    assert parse_candidates("nothing to learn", "PR #7") == []
    assert parse_candidates("[not valid json", "PR #7") == []
