"""Review conversation watch — pure decisions over pull-request conversations.

An issue parked for review (the merger owns it) is an issue nobody is listening for. This
module holds the decisions that change that, all of them pure:

- is this issue due for a check (throttle, or a GitHub comment webhook naming its PR);
- which of these comments are questions from a person;
- is there one newer than what we have already answered (watermark).

The code-host calls live in `codehost.py`. Everything here takes dictionaries.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

# Comment authorship. The code host types every account, and that is the signal we use:
# a name list would need editing whenever a bot is added, and would silently start
# answering the merger's own report the day its handle changed.
HUMAN_ACCOUNT_TYPE = "User"

ANSWER_MODE = "answer"

# Wake reason the API pushes when a GitHub conversation event (issue comment on a PR,
# review, review comment) arrives: `github:comment:<owner/name>#<number>`.
COMMENT_WAKE_PREFIX = "github:comment:"


def comment_wake(repo: str, number: int) -> str:
    """The `wake(daemon)` reason for a new comment on `repo#number`."""
    return f"{COMMENT_WAKE_PREFIX}{repo.lower()}#{int(number)}"


def parse_comment_wakes(reasons: Iterable[str]) -> set[tuple[str, int]]:
    """`(repo, number)` for every comment wake among `reasons`; anything else is ignored."""
    out: set[tuple[str, int]] = set()
    for reason in reasons:
        if not isinstance(reason, str) or not reason.startswith(COMMENT_WAKE_PREFIX):
            continue
        repo, _, number = reason[len(COMMENT_WAKE_PREFIX) :].rpartition("#")
        if repo and number.isdigit():
            out.add((repo.lower(), int(number)))
    return out


def is_due(entry: dict, now_ms: int, interval_s: int) -> bool:
    """Throttle per issue. Ticks run on every wake and the code host's rate limit is a
    shared, finite resource — checking every parked issue on every tick would spend it on
    issues nobody has commented on."""
    last = entry.get("pr_checked_at")
    if not last:
        return True
    return (now_ms - last) >= interval_s * 1_000


def human_comments(comments: list[dict]) -> list[dict]:
    """Comments written by a person.

    Excludes the agent's own identity and the automated reviewer alike — both are machine
    accounts, and answering either would be a conversation with itself. Anything missing an
    author is treated as non-human: unattributable input is not a question.
    """
    out = []
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") or {}
        if not isinstance(user, dict):
            continue
        if user.get("type") != HUMAN_ACCOUNT_TYPE:
            continue
        if comment.get("created_at"):
            out.append(comment)
    return out


def latest_at(comments: list[dict]) -> str | None:
    """Newest creation timestamp, or None. Timestamps are ISO-8601 UTC from the code host,
    which sorts lexicographically — no parsing, so a malformed one cannot raise here."""
    stamps = [str(c.get("created_at")) for c in comments if c.get("created_at")]
    return max(stamps) if stamps else None


def new_question(entry: dict, comments: list[dict]) -> str | None:
    """The timestamp to advance to when there IS a new human comment, else None.

    First sight establishes a baseline instead of firing: an issue that has been under
    review for a day would otherwise wake the agent to answer a conversation that was over
    before watching began (FR-003).
    """
    people = human_comments(comments)
    newest = latest_at(people)
    if newest is None:
        return None
    seen = entry.get("pr_comment_watermark")
    if seen is None:
        return None  # baseline only — the caller still records it
    return newest if newest > str(seen) else None


def baseline(entry: dict, comments: list[dict]) -> str:
    """What to record as seen when watching starts. The empty string is deliberate: it means
    "watched, nothing there yet", which is different from "never looked" and must not be
    confused with it — the second fires on the next comment, the first also does, but only
    the second would fire on the whole backlog.

    The backlog ends where the run stopped reading: `last_event_at`, written when the run
    finishes. A comment that arrived after it — while the issue was being handed over, before
    this first look — has been read by nobody, so it stays above the baseline and is answered.
    The cut is at the whole second (the code host's precision), on the side of the backlog."""
    people = human_comments(comments)
    ended = entry.get("last_event_at")
    if isinstance(ended, int) and not isinstance(ended, bool):
        cutoff = _dt.datetime.fromtimestamp(ended // 1000, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        people = [c for c in people if str(c["created_at"])[:19] + "Z" <= cutoff]
    return latest_at(people) or ""


def matches_issue(pull: dict, identifier: str) -> bool:
    """Does this pull request belong to the ticket?

    Matched on the identifier appearing in the title, body or head branch — the three places
    the prompt's own instructions cause it to land. Exact-ish rather than clever: filing an
    answer against another issue's pull request would put a reply in front of the wrong
    reviewer.
    """
    if not identifier:
        return False
    needle = identifier.lower()
    for field in ("title", "body"):
        value = pull.get(field)
        if isinstance(value, str) and needle in value.lower():
            return True
    head = (pull.get("head") or {}).get("ref") if isinstance(pull.get("head"), dict) else None
    return isinstance(head, str) and needle in head.lower()


def find_pull_number(pulls: list[dict], identifier: str) -> int | None:
    """Lowest matching open pull request number, or None.

    Lowest, not newest: if a duplicate was ever opened for one issue, the first one carries
    the review history — which is the conversation we are trying to answer.
    """
    numbers = [
        int(p["number"])
        for p in (pulls or [])
        if isinstance(p, dict) and isinstance(p.get("number"), int) and matches_issue(p, identifier)
    ]
    return min(numbers) if numbers else None
