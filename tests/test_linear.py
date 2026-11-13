"""Upstream test matrix §17.3: Linear adapter against a fake GraphQL transport.
Covers normalization (url/branchName/blockers via inverseRelations), dispatchable
per active state, rate limiting (HTTP 400 + RATELIMITED, and 429), outages, the
host-side mutation tool, and the singular workpad query."""

import json

import httpx
import pytest

from symphony.trackers.base import TrackerUnavailable
from symphony.trackers.linear import LinearTracker

ACTIVE = ["Todo", "In Progress", "Rework"]
TERMINAL = ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]


def node(issue_id="id1", identifier="ENG-1", state="Todo", blockers=(), **kw):
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": kw.get("title", "t"),
        "description": kw.get("description", "d"),
        "priority": kw.get("priority", 2),
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-02T00:00:00.000Z",
        "url": f"https://linear.app/x/issue/{identifier}",
        "branchName": f"{identifier.lower()}-t",
        "state": {"name": state},
        "labels": {"nodes": [{"name": "symphony"}]},
        "assignee": kw.get("assignee", {"id": "u1", "name": "alice"}),
        "inverseRelations": {
            "nodes": [
                {"type": "blocks", "issue": {"id": b, "state": {"name": "Todo", "type": bt}}}
                for b, bt in blockers
            ]
        },
    }


def make_tracker(handler, assignee=None, default_repo="acme/mono"):
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    return LinearTracker(
        api_key="lin_test", project_slug="proj", active_states=ACTIVE,
        terminal_states=TERMINAL, assignee=assignee, client=client,
        default_repo=default_repo,
    )


def graphql_response(payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {})


def test_normalization_and_inverse_relation_blockers():
    def handler(request):
        body = json.loads(request.content)
        assert "inverseRelations" in body["query"]  # blockers come from the INVERSE relation
        assert request.headers["Authorization"] == "lin_test"  # raw key, no Bearer
        return graphql_response({"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                node("id1", "ENG-1"),
                node("id2", "ENG-2", blockers=[("idX", "started")]),      # live blocker
                node("id3", "ENG-3", blockers=[("idY", "completed")]),    # terminal blocker
                node("id4", "ENG-4", state="Rework"),
            ],
        }}})

    issues = {i.identifier: i for i in make_tracker(handler).fetch_issues_by_states(ACTIVE)}
    assert issues["ENG-1"].dispatchable and issues["ENG-1"].url.endswith("ENG-1")
    assert issues["ENG-1"].branch_name == "eng-1-t"
    assert issues["ENG-1"].assignee_id == "u1" and issues["ENG-1"].native_ref == "id1"
    assert not issues["ENG-2"].dispatchable
    assert issues["ENG-2"].blocked_by[0]["id"] == "idX"  # blocker REF {id, identifier, state}
    assert issues["ENG-3"].dispatchable  # a completed blocker does not block
    assert issues["ENG-4"].dispatchable  # Rework is dispatchable (active state)


def test_assignee_routing_gates_dispatchable():
    def handler(request):
        return graphql_response({"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [node("id1", "ENG-1", assignee={"id": "u2", "name": "bob"})],
        }}})

    [issue] = make_tracker(handler, assignee="alice").fetch_issues_by_states(ACTIVE)
    assert not issue.dispatchable  # routed to someone else


def test_pagination_follows_end_cursor():
    calls = []

    def handler(request):
        variables = json.loads(request.content)["variables"]
        calls.append(variables.get("after"))
        if variables.get("after") is None:
            return graphql_response({"data": {"issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "cur1"},
                "nodes": [node("id1", "ENG-1")],
            }}})
        return graphql_response({"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [node("id2", "ENG-2")],
        }}})

    issues = make_tracker(handler).fetch_issues_by_states(ACTIVE)
    assert [i.identifier for i in issues] == ["ENG-1", "ENG-2"]
    assert calls == [None, "cur1"]


def test_rate_limit_http400_ratelimited_body():
    """Linear signals rate limiting with HTTP 400 + errors[].extensions.code=RATELIMITED."""
    import time

    reset_ms = (time.time() + 42) * 1000

    def handler(request):
        return graphql_response(
            {"errors": [{"message": "rate", "extensions": {"code": "RATELIMITED"}}]},
            status=400,
            headers={"X-RateLimit-Requests-Reset": str(reset_ms)},
        )

    with pytest.raises(TrackerUnavailable) as excinfo:
        make_tracker(handler).fetch_issues_by_states(ACTIVE)
    assert 30 < excinfo.value.retry_after_s <= 43


def test_http_429_and_5xx_and_transport_errors_raise_unavailable():
    for response in (
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(500),
    ):
        with pytest.raises(TrackerUnavailable):
            make_tracker(lambda request, r=response: r).fetch_issues_by_states(ACTIVE)

    def broken(request):
        raise httpx.ConnectError("down")

    with pytest.raises(TrackerUnavailable):
        make_tracker(broken).fetch_issues_by_states(ACTIVE)


def test_plain_400_is_rejected_not_ratelimited():
    def handler(request):
        return graphql_response({"errors": [{"message": "bad query"}]}, status=400)

    with pytest.raises(TrackerUnavailable) as excinfo:
        make_tracker(handler).fetch_issues_by_states(ACTIVE)
    assert excinfo.value.retry_after_s is None


def test_execute_agent_tool_posts_mutation_host_side():
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen.update(body)
        return graphql_response({"data": {"commentCreate": {"success": True}}})

    tracker = make_tracker(handler)
    result = tracker.execute_agent_tool("linear_graphql", {"query": "mutation X", "variables": {"a": 1}})
    assert result["commentCreate"]["success"] is True
    assert seen["query"] == "mutation X" and seen["variables"] == {"a": 1}
    with pytest.raises(ValueError):
        tracker.execute_agent_tool("outra_tool", {})


def test_workpad_uses_singular_issue_query_with_identifier():
    def handler(request):
        body = json.loads(request.content)
        assert "issue(id: $id)" in body["query"]  # IssueFilter does NOT have `identifier`
        assert body["variables"] == {"id": "ENG-1"}
        return graphql_response({"data": {"issue": {"comments": {"nodes": [
            {"body": "Plan: do X", "createdAt": "2026-07-01", "user": {"name": "agent"}},
            {"body": "Notes: Y", "createdAt": "2026-07-02", "user": {"name": "agent"}},
        ]}}}})

    workpad = make_tracker(handler).fetch_issue_workpad("ENG-1")
    assert "Plan: do X" in workpad and "Notes: Y" in workpad


def test_workpad_degrades_to_empty_on_outage():
    def handler(request):
        return httpx.Response(500)

    assert make_tracker(handler).fetch_issue_workpad("ENG-1") == ""


def test_empty_inputs_short_circuit_without_provider_request():
    def exploding(request):
        raise AssertionError("must not be called")

    tracker = make_tracker(exploding)
    assert tracker.fetch_issues_by_states([]) == []
    assert tracker.fetch_issues_by_ids([]) == []


def test_duplicate_typed_blocker_is_terminal():
    def handler(request):
        return graphql_response({"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [node("id1", "ENG-1", blockers=[("idD", "duplicate")])],
        }}})

    [issue] = make_tracker(handler).fetch_issues_by_states(ACTIVE)
    assert issue.dispatchable  # a blocker closed as Duplicate no longer blocks


def test_label_normalization_trim_lower_dedupe():
    def handler(request):
        base = node("id1", "ENG-1")
        base["labels"] = {"nodes": [{"name": " Symphony "}, {"name": "symphony"}, {"name": ""}, {"name": "OPS"}]}
        return graphql_response({"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [base],
        }}})

    [issue] = make_tracker(handler).fetch_issues_by_states(ACTIVE)
    assert issue.labels == ("symphony", "ops")


def test_by_ids_chunks_into_batches_of_50():
    calls = []

    def handler(request):
        variables = json.loads(request.content)["variables"]
        calls.append(len(variables["ids"]))
        return graphql_response({"data": {"issues": {"nodes": [node(i, i) for i in variables["ids"]]}}})

    ids = [f"id{i}" for i in range(120)]
    issues = make_tracker(handler).fetch_issues_by_ids(ids)
    assert len(issues) == 120
    assert calls == [50, 50, 20]


# -- feature 003: the target repository is a property of the ticket ----------
def _labels(*names):
    return {"nodes": [{"name": n} for n in names]}


def _one_issue(handler_node):
    def handler(request):
        return graphql_response({"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [handler_node],
        }}})
    return handler


def test_repo_label_resolves_onto_the_issue():
    n = node()
    n["labels"] = _labels("symphony", "repo:acme/backend")
    issue = make_tracker(_one_issue(n)).fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repo == "acme/backend"
    assert issue.undispatchable_reason is None
    assert issue.dispatchable


def test_issue_label_beats_the_configured_default():
    n = node()
    n["labels"] = _labels("repo:acme/api")
    issue = make_tracker(_one_issue(n), default_repo="acme/mono").fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repo == "acme/api"


def test_default_applies_when_the_ticket_carries_no_designation():
    n = node()
    n["labels"] = _labels("symphony")
    issue = make_tracker(_one_issue(n), default_repo="acme/mono").fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repo == "acme/mono" and issue.dispatchable


def test_unresolvable_repository_holds_the_issue_back_with_a_reason():
    """The failure this system is most prone to is the SILENT one: an issue that simply
    never dispatches, leaving the board misrepresenting what is being worked on."""
    n = node()
    n["labels"] = _labels("symphony")
    issue = make_tracker(_one_issue(n), default_repo=None).fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repo is None
    assert issue.undispatchable_reason == "no-repo-designation"
    assert not issue.dispatchable


def test_malformed_designation_does_not_fall_back_to_the_default():
    n = node()
    n["labels"] = _labels("repo:oops")
    issue = make_tracker(_one_issue(n), default_repo="acme/mono").fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repo is None
    assert issue.undispatchable_reason == "malformed-repo-designation"


def test_several_designations_route_the_ticket_to_all_of_them_primary_first():
    n = node()
    n["labels"] = _labels("symphony", "repo:acme/api", "repo:acme/web")
    issue = make_tracker(_one_issue(n)).fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repos == ("acme/api", "acme/web")
    assert issue.target_repo == "acme/api"
    assert issue.undispatchable_reason is None
    assert issue.dispatchable


def test_designations_across_owners_hold_the_ticket_back():
    n = node()
    n["labels"] = _labels("repo:acme/api", "repo:other/web")
    issue = make_tracker(_one_issue(n)).fetch_issues_by_states(["Todo"])[0]
    assert issue.target_repos == () and issue.target_repo is None
    assert issue.undispatchable_reason == "mixed-owner-repo-designation"
    assert not issue.dispatchable


# -- board scope -----------------------------------------------------------
# A board is scoped either to one project or to a whole team. The scope decides what the
# adapter FETCHES; the `symphony` label still decides what gets worked, so widening the
# scope widens what is considered, never what is dispatched.


def test_team_scope_queries_by_team_when_no_project_is_set():
    seen = {}

    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        return graphql_response(
            {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    LinearTracker(
        api_key="k", project_slug="", team_key="ENG", active_states=ACTIVE,
        terminal_states=TERMINAL, client=client,
    ).fetch_issues_by_states(["Todo"])

    assert "team: {key: {eq: $scope}}" in seen["query"]
    assert "project:" not in seen["query"]
    assert seen["variables"]["scope"] == "ENG"


def test_a_project_designation_wins_over_a_team():
    """The narrower scope is the one an operator who set both almost certainly meant."""
    seen = {}

    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        return graphql_response(
            {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    LinearTracker(
        api_key="k", project_slug="p1", team_key="ENG", active_states=ACTIVE,
        terminal_states=TERMINAL, client=client,
    ).fetch_issues_by_states(["Todo"])

    assert "project: {slugId: {eq: $scope}}" in seen["query"]
    assert seen["variables"]["scope"] == "p1"


def test_an_unscoped_board_is_refused_at_construction():
    """Neither scope would sweep every issue the key can see, and grow silently with the
    workspace. Refusing beats defaulting to 'everything'."""
    import pytest

    with pytest.raises(ValueError, match="project_slug|team_key"):
        LinearTracker(api_key="k", project_slug="", active_states=ACTIVE, terminal_states=TERMINAL)
