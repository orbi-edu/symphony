"""Code-host reads for the review watch.

Contract: specs/005-review-conversation-watch/spec.md

The rule this module lives by: every failure becomes `CodeHostUnavailable`, and the caller
treats that as "no information this cycle". Nothing here may propagate an arbitrary
exception into a polling cycle.
"""

import pytest

from symphony.codehost import CodeHostUnavailable, GitHubReader


class FakeHTTP:
    def __init__(self, routes=None, raises=None):
        self.routes = routes or {}
        self.raises = raises
        self.calls = []

    def __call__(self, url, token, params):
        self.calls.append((url, token, params))
        if self.raises:
            raise self.raises
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return payload
        return []


def reader(routes=None, raises=None, token=lambda repo: "tok"):
    http = FakeHTTP(routes, raises)
    return GitHubReader(token, http), http


def test_open_pulls_are_scoped_to_the_repository():
    r, http = reader({"/pulls": [{"number": 4}]})
    assert r.open_pulls("acme/api") == [{"number": 4}]
    assert "/repos/acme/api/pulls" in http.calls[0][0]
    assert http.calls[0][2]["state"] == "open"


def test_the_conversation_spans_every_surface_a_reviewer_can_use():
    """A question asked inline on a diff is as much a question as one in the main thread;
    answering only one of them would look arbitrary from the outside."""
    r, _ = reader({
        "/issues/4/comments": [{"id": 1, "body": "thread"}],
        "/pulls/4/comments": [{"id": 2, "body": "inline"}],
        "/pulls/4/reviews": [{"id": 3, "body": "summary", "submitted_at": "2026-08-07T10:00:00Z"}],
    })
    got = r.conversation("acme/api", 4)
    assert {c["id"] for c in got} == {1, 2, 3}


def test_a_review_with_no_body_is_dropped():
    """An approval is a verdict, not a question — it must not wake the agent to answer it."""
    r, _ = reader({"/pulls/4/reviews": [
        {"id": 3, "body": "", "submitted_at": "2026-08-07T10:00:00Z"},
        {"id": 4, "body": "   ", "submitted_at": "2026-08-07T10:00:00Z"},
    ]})
    assert r.conversation("acme/api", 4) == []


def test_a_review_gets_a_comparable_timestamp():
    """Reviews carry `submitted_at`, comments carry `created_at`. The watermark compares one
    field, so a review with only its own name would sort as having no timestamp and be
    silently ignored."""
    r, _ = reader({"/pulls/4/reviews": [
        {"id": 3, "body": "please explain", "submitted_at": "2026-08-07T10:00:00Z"}]})
    assert r.conversation("acme/api", 4)[0]["created_at"] == "2026-08-07T10:00:00Z"


def test_a_network_failure_becomes_the_declared_error():
    r, _ = reader(raises=RuntimeError("connection reset"))
    with pytest.raises(CodeHostUnavailable):
        r.open_pulls("acme/api")


def test_a_credential_failure_is_also_an_outage_not_a_crash():
    """A repository the App was uninstalled from must degrade to silence, exactly like a
    network problem — the issue simply stays parked."""
    def broken(repo):
        raise RuntimeError("not installed")

    r, _ = reader(token=broken)
    with pytest.raises(CodeHostUnavailable):
        r.open_pulls("acme/api")


def test_unexpected_payload_shapes_do_not_raise():
    r, _ = reader({"/pulls": {"message": "Not Found"}})
    assert r.open_pulls("acme/api") == []


def test_the_token_is_requested_per_repository():
    seen = []
    r, _ = reader({"/pulls": []}, token=lambda repo: seen.append(repo) or "tok")
    r.open_pulls("acme/api")
    r.open_pulls("acme/web")
    assert seen == ["acme/api", "acme/web"]
