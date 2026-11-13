"""Release engineering: one version, the docs link check and the release notes."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from scripts import check_links

from symphony.cli.main import main

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_cli_version_is_the_project_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"symphony {VERSION}"


def test_link_check_reports_missing_files_and_anchors(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n## Set `WORKFLOW.md` up\n\n## Steps\n\n## Steps\n\n"
        "[ok](#set-workflowmd-up) [dup](#steps-1) [gone](#nope) [file](missing.md)\n"
        "`[code](missing.md)` [web](https://example.com/x.md)\n"
        "```\n[fenced](missing.md)\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "[a](docs/guide.md#guide) [b](docs/guide.md#steps-2)\n", encoding="utf-8"
    )
    assert check_links.check(tmp_path) == [
        "README.md:1: docs/guide.md#steps-2: no such anchor",
        "docs/guide.md:9: #nope: no such anchor",
        "docs/guide.md:9: missing.md: no such file",
    ]


def test_release_notes_are_the_changelog_section_of_the_version():
    script = ROOT / ".github/scripts/release_notes.sh"
    notes = subprocess.run([script, VERSION], capture_output=True, text=True, check=True).stdout
    assert notes.strip() and "## [" not in notes
    missing = subprocess.run([script, "0.0.0-none"], capture_output=True, text=True)
    assert missing.returncode == 1 and "no notes for 0.0.0-none" in missing.stderr
