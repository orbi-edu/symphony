"""Pure decisions behind the review watch: who is a person, what is new, which pull request.

Contract: specs/005-review-conversation-watch/spec.md
"""

import pytest

from symphony import prwatch

HUMAN = {"type": "User", "login": "maintainer"}
APP = {"type": "Bot", "login": "symphony[bot]"}
REVIEWER = {"type": "Bot", "login": "symphony-merger[bot]"}


def c(at, user=HUMAN):
    return {"created_at": at, "user": user}


# -- authorship -------------------------------------------------------------


def test_only_people_count_as_questions():
    """Both the agent's own identity and the merger are machine accounts. Answering
    either is a conversation with itself."""
    got = prwatch.human_comments([c("1", HUMAN), c("2", APP), c("3", REVIEWER)])
    assert [x["created_at"] for x in got] == ["1"]


def test_authorship_is_decided_by_account_type_not_by_name():
    """A name list would need editing whenever a bot is added, and would silently start
    answering the reviewer the day it changed its handle."""
    unknown_bot = {"type": "Bot", "login": "some-new-tool[bot]"}
    assert prwatch.human_comments([c("1", unknown_bot)]) == []


@pytest.mark.parametrize("bad", [
    {"created_at": "1"},                       # no author at all
    {"created_at": "1", "user": None},
    {"created_at": "1", "user": "maintainer"},    # not a mapping
    {"user": HUMAN},                           # no timestamp
    None,
    "not a comment",
])
def test_unattributable_input_is_not_a_question(bad):
    assert prwatch.human_comments([bad]) == []


# -- watermark --------------------------------------------------------------


def test_first_sight_establishes_a_baseline_and_asks_for_nothing():
    """Turning the watch on must not replay a conversation that ended days ago."""
    comments = [c("2026-08-01T10:00:00Z"), c("2026-08-02T10:00:00Z")]
    assert prwatch.new_question({}, comments) is None
    assert prwatch.baseline({}, comments) == "2026-08-02T10:00:00Z"


def test_a_newer_comment_is_a_question():
    entry = {"pr_comment_watermark": "2026-08-02T10:00:00Z"}
    comments = [c("2026-08-02T10:00:00Z"), c("2026-08-03T09:00:00Z")]
    assert prwatch.new_question(entry, comments) == "2026-08-03T09:00:00Z"


def test_the_same_comment_is_not_a_question_twice():
    entry = {"pr_comment_watermark": "2026-08-03T09:00:00Z"}
    assert prwatch.new_question(entry, [c("2026-08-03T09:00:00Z")]) is None


def test_a_new_machine_comment_does_not_advance_anything():
    entry = {"pr_comment_watermark": "2026-08-02T10:00:00Z"}
    assert prwatch.new_question(entry, [c("2026-08-09T10:00:00Z", REVIEWER)]) is None


def test_an_empty_baseline_still_fires_on_the_next_comment():
    """`""` means "watched, nothing there" — different from never having looked, and the
    difference is whether the backlog fires."""
    entry = {"pr_comment_watermark": ""}
    assert prwatch.new_question(entry, [c("2026-08-03T09:00:00Z")]) == "2026-08-03T09:00:00Z"


def test_a_conversation_with_no_people_has_no_baseline_but_is_still_watched():
    assert prwatch.baseline({}, [c("1", REVIEWER)]) == ""


# -- throttle ---------------------------------------------------------------


def test_a_never_checked_issue_is_due():
    assert prwatch.is_due({}, 1_000_000, 120)


def test_the_interval_is_respected():
    entry = {"pr_checked_at": 1_000_000}
    assert not prwatch.is_due(entry, 1_000_000 + 119_000, 120)
    assert prwatch.is_due(entry, 1_000_000 + 120_000, 120)


# -- pull request identification -------------------------------------------


def test_the_pull_request_is_found_by_identifier():
    pulls = [{"number": 3, "title": "unrelated"},
             {"number": 4, "title": "ENG-85 fix the admin"}]
    assert prwatch.find_pull_number(pulls, "ENG-85") == 4


@pytest.mark.parametrize("pull", [
    {"number": 4, "title": "fix", "body": "closes ENG-85"},
    {"number": 4, "title": "fix", "head": {"ref": "symphony/eng-85"}},
    {"number": 4, "title": "ENG-85"},
])
def test_the_identifier_is_matched_wherever_the_prompt_puts_it(pull):
    assert prwatch.find_pull_number([pull], "ENG-85") == 4


def test_an_unmatched_identifier_finds_nothing_rather_than_guessing():
    """Filing an answer against another issue's pull request would put a reply in front of
    the wrong reviewer."""
    assert prwatch.find_pull_number([{"number": 4, "title": "ENG-90"}], "ENG-85") is None
    assert prwatch.find_pull_number([], "ENG-85") is None
    assert prwatch.find_pull_number([{"number": 4, "title": "x"}], "") is None


def test_a_duplicate_pull_request_resolves_to_the_one_with_the_history():
    """Duplicates have happened. The first one carries the review conversation, which is the
    thing we are trying to answer."""
    pulls = [{"number": 11, "title": "ENG-85 second try"}, {"number": 9, "title": "ENG-85"}]
    assert prwatch.find_pull_number(pulls, "ENG-85") == 9


def test_malformed_pull_entries_are_skipped():
    pulls = [None, "x", {"title": "ENG-85"}, {"number": "4", "title": "ENG-85"},
             {"number": 6, "title": "ENG-85"}]
    assert prwatch.find_pull_number(pulls, "ENG-85") == 6
