"""Shared setup for the suites that run the plugin's modules under the `luau` CLI."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIB = HERE.parent.parent / "tern-plugin" / "lib"


@pytest.fixture(scope="session")
def luau_plugin(tmp_path_factory) -> Callable[[str, str], Path]:
    """`stage(name, entry)` copies the plugin's modules to `<tmp>/lib`, each starting with
    `local tern = require("./tern_stub")`, next to `tern_stub.luau`, and copies the entry script
    `entry` (from tests/tern) to `<tmp>`. It returns the entry script's path."""
    def stage(name: str, entry: str) -> Path:
        root = tmp_path_factory.mktemp(name)
        (root / "lib").mkdir()
        for src in LIB.glob("*.luau"):
            first, _, rest = src.read_text().partition("\n")
            (root / "lib" / src.name).write_text(f'{first}\nlocal tern = require("./tern_stub")\n{rest}')
        shutil.copyfile(HERE / "tern_stub.luau", root / "lib" / "tern_stub.luau")
        shutil.copyfile(HERE / entry, root / entry)
        return root / entry
    return stage
