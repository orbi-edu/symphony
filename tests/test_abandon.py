"""A canceled ticket stops everything (`tracker.abandon_states`, `Config.abandons`): its open
questions close, and the merger closes its pull requests once, with one comment, and gives
back their preview devices. A Done ticket never does any of it."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from tests import test_questions
from tests.test_dispatch import FakeEffects, cfg
from tests.test_merger_support import FakeGitHub, FakeTracker, ScriptedAgents, World, make_merger
from tests.test_questions import HOUR, NS, Out, comments

from symphony import notify, orchestrator, questions, state
from symphony.assistant import Action
from symphony.assistant.answers import Answers
from symphony.config import ConfigError, parse_workflow
from symphony.merger import comments as cm
from symphony.merger.abandon import check_abandoned, close_abandoned_prs, ticket_state
from symphony.merger.policy import parse
from symphony.merger.review import pick

world = test_questions.world  # the questions' fixture: a Slack fake, a memory tracker


# -- configuration ------------------------------------------------------------------------
def test_abandon_states_default_to_the_canceled_type_terminal_states():
    c = cfg()
    assert c.abandon_states == ("Cancelled", "Canceled", "Duplicate")
    assert c.abandons("Canceled") and c.abandons("duplicate")
    assert not c.abandons("Done") and not c.abandons("Done", canceled=True)
    assert not c.abandons("Closed")
    assert c.abandons("Won't do", canceled=True)  # the tracker's canceled-type state
    # A board whose terminal states have none of the default names gets none by default.
    narrow = parse_workflow("---\ntracker: {kind: memory, terminal_states: [Done, Closed]}\n---\nx")
    assert narrow.abandon_states == ()


@pytest.mark.parametrize("states, error", [
    ("[Done]", "must not include tracker.states.done"),
    ("[Archived]", "must be terminal states"),
])
def test_abandon_states_are_terminal_and_never_done(states, error):
    with pytest.raises(ConfigError, match=error):
        parse_workflow(f"---\ntracker: {{kind: memory, abandon_states: {states}}}\n---\nx")


# -- questions (the daemon) ---------------------------------------------------------------
def _finalize(world, state_name):
    tracker = world["tracker"]
    tracker.set_state("i1", state_name)
    [issue] = tracker.fetch_issues_by_ids(["i1"])
    fx = FakeEffects()
    orchestrator._finalize_terminal(world["kv"], "i1", {"attempt": 1, "dispatch_seq": 1}, issue,
                                    cfg(), world["now"][0], fx)
    return fx


def test_a_canceled_tickets_open_question_closes_and_is_never_reminded(world):
    world["slack"].dm_channel = "D1"
    world["ask"]()
    world["tick"]()
    world["now"][0] += 1
    fx = _finalize(world, "Canceled")
    assert ("ticket_abandoned", {"issue_id": "i1", "state": "Canceled"}) in fx.events
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["state"], rec["closed"], rec["closed_state"]) == ("closed", "canceled", "Canceled")
    [(where, head, text)] = world["slack"].edits
    assert head == ":no_entry: *Ticket canceled*" and text == "It was moved to Canceled."
    assert where["dms"] and world["slack"].edited_questions == ["Which layout?"]
    assert questions.open_on(world["client"], NS, "i1") == []

    world["now"][0] += 3 * HOUR
    world["tick"]()
    assert len(comments(world)) == 1 and len(world["slack"].posts) == 1  # no reminder

    out = Out()
    answers = Answers(world["client"], NS, notify_users=lambda: {"slack": {"ana": "U1"}})
    click = Action(transport="slack", channel="C1", thread="1.0", message_id="1.0",
                   user_id="U1", action_id=notify.question_action("q1", 0), value="0")
    assert answers.on_click(click, out) == "canceled"
    assert out.notes[-1] == "Thanks, but this question is closed: the ticket was canceled."


def test_a_question_asked_after_the_ticket_came_back_stays_open(world):
    _finalize(world, "Duplicate")
    world["tracker"].set_state("i1", "In Review")
    world["now"][0] += 1
    world["ask"]()
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "open"


def test_an_untracked_canceled_ticket_is_caught_before_the_reminder(world):
    world["ask"]()
    world["tick"]()
    world["tracker"].set_state("i1", "Duplicate")  # the daemon no longer tracked it
    world["now"][0] += 2 * HOUR
    world["tick"]()
    rec = questions.load(world["client"], NS, "q1")
    assert (rec["state"], rec["closed"]) == ("closed", "canceled")
    assert len(comments(world)) == 1 and len(world["slack"].posts) == 1


def test_a_done_ticket_leaves_its_question_alone(world):
    world["ask"]()
    world["tick"]()
    _finalize(world, "Done")
    assert world["kv"].get(state.abandoned_key("i1")) is None
    world["tick"]()
    assert questions.load(world["client"], NS, "q1")["state"] == "open"
    assert world["slack"].edits == []


# -- pull requests (the merger) -----------------------------------------------------------
OWNER = "review:test:1"


@pytest.fixture
def env(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    w = World(str(tmp_path / "w"))
    gh = FakeGitHub(w)
    tracker = FakeTracker()
    released: list[int] = []
    m = make_merger(redis_client, tmp_path, w, gh, ScriptedAgents(), tracker=tracker)
    m.release_previews = lambda n: released.append(n) or 1
    w.commit("eng-7", {"src/a.py": "A = 1\n"}, "feat: a")
    gh.open_pr(7, "eng-7", "ENG-7 add a", author="alice")
    gh.tick()
    return SimpleNamespace(world=w, gh=gh, tracker=tracker, m=m, released=released)


def _ticket(env, name, kind="terminal"):
    env.tracker.states["ENG-7"] = {"name": name, "type": kind}
    env.m._ticket_states.clear()  # past the minute the merger caches a ticket's state


@pytest.mark.parametrize("name", ["Canceled", "Duplicate"])
def test_a_canceled_tickets_pr_is_closed_once_with_one_comment(env, name):
    env.m.store.park({"pr": 7, "kind": "clean", "head_sha": "x"})
    _ticket(env, name)
    assert close_abandoned_prs(env.m, OWNER) == [7]
    assert env.gh.prs[7]["state"] == "closed"
    [comment] = env.gh.comments[7]
    assert comment["body"] == (f"**symphony-merger**: Closed by symphony-merger: ENG-7 was moved "
                               f"to {name}. Reopen the PR to work on it again.")
    assert cm.OURS.match(comment["body"])
    assert env.m.store.parked(7) is None
    assert env.released == [7]
    assert close_abandoned_prs(env.m, OWNER) == []
    assert len(env.gh.comments[7]) == 1


def test_a_reopened_pr_is_not_closed_again_and_a_live_ticket_resumes_reviews(env):
    _ticket(env, "Canceled")
    check_abandoned(env.m)
    env.gh.reopen(7)  # a person wants it after all
    assert check_abandoned(env.m) == []
    assert env.gh.prs[7]["state"] == "open" and len(env.gh.comments[7]) == 1

    _ticket(env, "Todo", "active")
    assert check_abandoned(env.m) == []
    picked = pick(env.m, parse("---\n---\nRules."), OWNER)
    assert picked is not None and picked["number"] == 7

    _ticket(env, "Canceled")  # canceled again: a new cancel closes it again
    env.m.store.unclaim(7, OWNER)
    assert check_abandoned(env.m) == [7]
    assert len(env.gh.comments[7]) == 2


def test_a_done_ticket_never_closes_its_pr(env):
    _ticket(env, "Done")
    env.tracker.states["ENG-7"]["canceled"] = True  # even a tracker that says so
    assert check_abandoned(env.m) == []
    assert env.gh.prs[7]["state"] == "open" and env.gh.comments[7] == []


def test_a_failed_close_is_retried_without_a_second_comment(env):
    _ticket(env, "Canceled")
    env.gh.fail_next["close"] = RuntimeError("GitHub is down")
    assert check_abandoned(env.m) == []
    assert check_abandoned(env.m) == [7]
    assert env.gh.prs[7]["state"] == "closed" and len(env.gh.comments[7]) == 1


def test_a_public_repositorys_pr_does_not_name_the_ticket(env):
    env.world.commit("symphony/abc123", {"src/b.py": "B = 1\n"}, "feat: b")
    env.gh.open_pr(8, "symphony/abc123", "Add b", author="alice")
    pr = {**env.gh.pr(8)}
    env.tracker.states["ENG-8"] = {"name": "Canceled", "type": "terminal"}
    env.m.ticket_of = lambda p: "ENG-8" if p["number"] == 8 else None
    assert check_abandoned(env.m, [pr]) == [8]
    assert "its ticket was moved to Canceled" in env.gh.comments[8][0]["body"]
    assert "ENG-8" not in env.gh.comments[8][0]["body"]


def test_ticket_states_are_read_once_a_minute(env):
    calls = []
    real = env.tracker.state_of
    env.tracker.state_of = lambda identifier: calls.append(identifier) or real(identifier)
    for _ in range(3):
        ticket_state(env.m, "ENG-7")
    assert calls == ["ENG-7"]


def _linear(answers: list, **scope):
    """A Linear tracker whose API answers every `issue(id:)` the way Linear does for an
    identifier it does not know: an `INPUT_ERROR` "Entity not found", not `issue: null`."""
    import httpx

    from symphony.trackers.linear import LinearTracker

    def handler(request):
        answers.append(json.loads(request.content)["variables"])
        return httpx.Response(200, json={"data": None, "errors": [{
            "message": "Entity not found: Issue", "path": ["issue"],
            "extensions": {"type": "invalid input", "code": "INPUT_ERROR", "statusCode": 400,
                           "userError": True, "userPresentableMessage": "Could not find referenced Issue."}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    return LinearTracker(api_key="lin_test", active_states=["Todo"], terminal_states=["Done", "Canceled"],
                         client=client, **{"project_slug": "", **scope})


@pytest.mark.parametrize("scope, asked", [
    ({"project_slug": "web"}, [{"id": "WIDGET-5"}]),  # a project board: any key may be a ticket
    ({"team_key": "ENG"}, []),  # a team's board: `widget-5` is no ENG ticket, nothing to ask
])
def test_a_branch_naming_a_key_linear_does_not_know_is_no_ticket(env, scope, asked):
    """Regression: a person's branch `bob/widget-5-app-data` read as the ticket WIDGET-5, which
    Linear answers with an error, not `issue: null`; the check raised, cached nothing, and
    logged `merger_abandon_check_failed` every pass. It is no ticket: no log, nothing closed,
    and Linear is asked at most once a minute."""
    from structlog.testing import capture_logs

    answers: list = []
    env.m.tracker = _linear(answers, **scope)
    env.world.commit("bob/widget-5-app-data", {"src/w.py": "W = 5\n"}, "feat: widget")
    env.gh.open_pr(8, "bob/widget-5-app-data", "Store the app data", author="bob")
    prs = [env.gh.pr(8)]
    with capture_logs() as logs:
        assert check_abandoned(env.m, prs) == []
        assert check_abandoned(env.m, prs) == []
    assert logs == [] and env.gh.prs[8]["state"] == "open" and env.gh.comments[8] == []
    assert answers == asked
    env.m.clock.ms += 60_001  # a minute later: asked again, still no ticket
    assert check_abandoned(env.m, prs) == [] and answers == asked * 2


def test_other_linear_errors_still_fail_the_lookup():
    import httpx

    from symphony.trackers.base import TrackerUnavailable
    from symphony.trackers.linear import LinearTracker

    def handler(request):
        return httpx.Response(200, json={"data": None, "errors": [{
            "message": "Argument Validation Error", "extensions": {"code": "INPUT_ERROR"}}]})

    tracker = LinearTracker(api_key="k", project_slug="web", active_states=["Todo"], terminal_states=["Done"],
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TrackerUnavailable, match="Argument Validation Error"):
        tracker.state_of("ENG-1")
