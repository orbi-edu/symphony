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


# A branch name the merger maps back to the ticket without naming it (`Merger.ticket_of`).
PUBLIC_BRANCH_PREFIX = "symphony/"


def public_repository_guidance(issue: Issue, repos: list[str]) -> str:
    """Appended to the prompt when the work touches a public repository: there the ticket is
    never named, and the branch carries the ticket's opaque id instead of its identifier."""
    names = ", ".join(f"`{repo}`" for repo in repos)
    return (
        f"## Public repositories: {names}\n\n"
        "Everything you write there is public and permanent: code, tests, docs, commit "
        "messages, the branch, the pull request and its comments. This section overrides "
        "every instruction above about naming or linking the ticket.\n\n"
        f"- Never write `{issue.identifier}`, any other ticket identifier, ticket URLs, the "
        "tracker's or the company's name, people's names, hostnames, account numbers or "
        "anything copied from the ticket, its comments or logs. Describe the problem in "
        "generic terms, with neutral examples (`acme/app`, `ENG-12`, `example.com`).\n"
        f"- Name the branch `{PUBLIC_BRANCH_PREFIX}{issue.id}`: Symphony links the pull "
        "request to the ticket through it. Link the pull request from the ticket (the Workpad "
        "or a comment there), never the ticket from the pull request. The title and "
        "description say what changed and why, nothing else.\n"
        "- Before every push and before you open or edit the pull request, follow the "
        "repository's public-repo-stealth skill when it has one (`.omp/skills/`), and check "
        "the diff, the commit messages, the branch and the pull request text for anything "
        "this list forbids."
    )


def preview_links_guidance(links: list[tuple[str, int, str, str]]) -> str:
    """Appended to an answer run's prompt when the ticket's PRs have preview builds
    (`preview_links`): `(repo, pr, sha12, url)`, newest first per PR."""
    rows = "\n".join(f"- {repo}#{pr} at `{sha12}`: {url}" for repo, pr, sha12, url in links)
    return (
        "## Preview links\n\n"
        "Each link opens that build of the app on a simulator in Seiji for the person who "
        "clicks it, now or later, while GitHub keeps the build (newest first):\n\n"
        f"{rows}\n\n"
        "When someone asks to open, run or test the app in Seiji or a simulator, reply with "
        "the link of the build they mean (the newest unless they name another). Do not "
        "install it on a device yourself: the person cannot reach a device you hold."
    )
