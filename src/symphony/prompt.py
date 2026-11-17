"""STRICT Liquid rendering of the prompt (upstream spec §6 requirement):
unknown variable OR unknown filter ⇒ failure — never a silent render.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from liquid import Environment
from liquid.exceptions import LiquidError

from .config import Config
from .trackers.base import Issue


class PromptError(Exception):
    pass


def _strict_env() -> Environment:
    from liquid import StrictUndefined

    try:
        return Environment(undefined=StrictUndefined, strict_filters=True)
    except TypeError:  # python-liquid >= 2.x: unknown filters are already strict
        return Environment(undefined=StrictUndefined)


_ENV = _strict_env()


def validate_template(body: str) -> None:
    """Syntax errors are caught at parse time (used by config on reload)."""
    try:
        _ENV.from_string(body)
    except LiquidError as exc:
        raise PromptError(f"invalid Liquid template: {exc}") from exc


def render_prompt(
    body: str,
    issue: Issue,
    attempt: int,
    extra_guidance: str = "",
    *,
    investigation_label: str = Config.investigation_label,
    runtime: str | None = None,
    component: str | None = None,
    component_path: str | None = None,
) -> str:
    """The Liquid context is strictly `issue` and `attempt`;
    any additional content (e.g. the L1 failure summary) goes in as guidance APPENDED to
    the rendered prompt, never as a new variable.

    `issue.investigation` is derived here — true when the ticket carries
    `tracker.investigation_label` — so a template branches on the KIND of work without
    hardcoding the board's label name."""
    # upstream semantics (§5.4): `attempt` is null/absent on the first attempt and
    # an integer on subsequent ones — reference templates use `{% if attempt %}`
    label = investigation_label.casefold()
    context: dict[str, Any] = {
        "issue": {
            **asdict(issue),
            "investigation": any(lbl.casefold() == label for lbl in issue.labels),
            "component": component,
            "component_path": component_path,
            "runtime": runtime,
        },
        "attempt": attempt if attempt and attempt > 1 else None,
    }
    try:
        template = _ENV.from_string(body)
        rendered = template.render(**context)
    except LiquidError as exc:
        raise PromptError(f"prompt render failed: {exc}") from exc
    if extra_guidance:
        rendered = f"{rendered}\n\n{extra_guidance}"
    return rendered
