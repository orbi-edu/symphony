"""Target-repository resolution from an issue's labels — pure.

Deliberately its own module rather than living inside the Linear adapter: the rules
(precedence, malformed, owners, case) are provider-independent, and a second tracker
adapter must not have to reimplement them.

A ticket names its repositories with `repo:owner/name` labels — one for ordinary work,
several when one change spans repositories (an API and its client, say). The first is the
PRIMARY: the workspace root, the repository the review watch and the reflector default to.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PREFIX = "repo:"

# Four reasons rather than one, because each demands a different human action:
# add a label, fix a typo, split the ticket by owner, install the application. An operator
# who cannot tell them apart debugs the wrong thing.
MISSING = "no-repo-designation"
MALFORMED = "malformed-repo-designation"
MIXED_OWNERS = "mixed-owner-repo-designation"
NOT_AUTHORIZED = "repo-not-authorized"  # only discoverable at credential-mint time

# Path segments the code host permits: letters, digits, dot, underscore, hyphen.
_SEGMENT = r"[A-Za-z0-9._-]+"
_REPO_RE = re.compile(rf"^{_SEGMENT}/{_SEGMENT}$")


def parse_designation(label: str) -> str | None:
    """`repo:owner/name` -> `owner/name`, lower-cased. None if malformed.

    Lower-casing matches the existing label normalization, and the code host treats
    `owner/name` case-insensitively in both clone URLs and its API — so this resolves
    correctly. Recorded here because "fixing" the normalization to preserve case would
    change behavior for every label in the system, not just these.
    """
    value = label[len(PREFIX) :].strip()
    return value.lower() if _REPO_RE.match(value) else None


def resolve_target_repos(
    labels: Iterable[str], default: str | None = None
) -> tuple[tuple[str, ...], str | None]:
    """Returns `(repositories, reason)`: the ticket's repositories, primary first, or an
    empty tuple and the reason the ticket is held back.

    Precedence: the issue's own designations, then the configured default. A default is a
    convenience for boards where most work goes to one repository — never an override. When
    the default is among several designations it is the primary (the board's main
    repository is where the work is anchored); otherwise the primary is the first
    designation in the tracker's label order.
    """
    designations = [label for label in labels if label.lower().startswith(PREFIX)]

    if not designations:
        return ((default.lower(),), None) if default else ((), MISSING)

    parsed = [parse_designation(label) for label in designations]
    if any(value is None for value in parsed):
        # A malformed designation does NOT fall back to the default, nor is it dropped
        # while the others proceed: either would turn a typo into work in the wrong (or a
        # missing) repository, which is exactly what this taxonomy exists to prevent.
        return (), MALFORMED

    repos = list(dict.fromkeys(parsed))  # case-insensitive dedupe, order kept
    if len({repo.split("/", 1)[0] for repo in repos}) > 1:
        # One App installation belongs to one account, so no single credential can cover
        # repositories of two owners. Refusing now beats an attempt that dies at mint time.
        return (), MIXED_OWNERS
    primary = default.lower() if default and default.lower() in repos else repos[0]
    return (primary, *(repo for repo in repos if repo != primary)), None
