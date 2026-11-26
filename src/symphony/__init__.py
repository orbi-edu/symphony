"""Symphony: tracker tickets worked by coding agents on Kubernetes."""

import tomllib
from importlib.metadata import version
from pathlib import Path

# `<checkout>/src/symphony/__init__.py`: the pyproject.toml of the checkout this package runs
# from (an editable install, or `src/` on the path), when there is one.
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _source_version() -> str | None:
    """`version` of the checkout's pyproject.toml. The metadata of an editable install keeps the
    version of the day it was installed; the checkout has moved on since."""
    try:
        project = tomllib.loads(_PYPROJECT.read_text()).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return project.get("version") if project.get("name") == "symphony" else None


_source = _source_version()
# The checkout the package runs from, or None for an installed copy.
SOURCE_ROOT: Path | None = _PYPROJECT.parent if _source else None
# The one version source is pyproject.toml: the checkout's when there is one, else the installed
# package metadata, which a build copies from it.
__version__: str = _source or version("symphony")
