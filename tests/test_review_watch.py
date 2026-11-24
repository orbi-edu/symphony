"""Review conversation watch: answering the reviewer without taking the review from them.

Contract: specs/005-review-conversation-watch/spec.md

The property under test throughout is that an answer run is a *guest* in a state it does not
own — it may speak, and nothing else.
"""

import datetime as dt

from tests.test_dispatch import FakeEffects
from tests.test_dispatch import cfg as base_cfg

from symphony import notify, prwatch, questions, state
from symphony.kv import RedisKV
from symphony.orchestrator import PARKED, tick

HUMAN = {"type": "User"}
BOT = {"type": "Bot"}


def comment(at, user=None, body="why this approach?"):
    return {"created_at": at, "user": user or HUMAN, "body": body}


def cfg(**over):
    base = dict(
        review_watch_enabled=True,
        review_watch_interval_s=120,
        answer_max_turns=3,
        answer_prompt="Answer {{ issue.identifier }}.",
    )
    base.update(over)
    return base_cfg(**base)


class WatchEffects(FakeEffects):
    def __init__(self, pulls=None, comments=None, raises=False, **kw):
        super().__init__(**kw)
        self._pulls = pulls if pulls is not None else [{"number": 7, "title": "ENG-i1 fix"}]
        self._comments = comments or []
        self._raises = raises
        self.pull_calls = 0
        self.conversation_calls = 0

    def open_pulls(self, repo):
        self.pull_calls += 1
        if self._raises:
            raise RuntimeError("github is down")
        return self._pulls

    def pr_conversation(self, repo, number):
        self.conversation_calls += 1
        if self._raises:
            raise RuntimeError("github is down")
        return self._comments


# When the parked run stopped reading (its `last_event_at`): after every "backlog" comment of
# these tests, before every new one. The fake clock itself is near the epoch.
RUN_ENDED_MS = int(dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.UTC).timestamp() * 1000)


def park(kv, clock, tracker, c, issue_id="i1"):
    """Drive an issue to the state a real one is in while under review."""
    tracker.add_issue(issue_id, "ENG-i1")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key(issue_id))
    entry.update(status="running", runner_call_id=None, sandbox_name="sb-1",
                 target_repo="acme/api", target_repos=["acme/api"],
                 tokens={"total": 100, "cost_estimate": 0.1}, last_event_at=RUN_ENDED_MS)
    kv.put(state.issue_key(issue_id), entry)
    tracker.set_state(issue_id, "In Review")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")  # parks + reports
    return kv.get(state.issue_key(issue_id))


def watch(kv, clock, tracker, c, fx, advance=200_000):
    clock.advance(advance)
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    return kv.get(state.issue_key("i1"))


# -- baseline ---------------------------------------------------------------


def test_the_first_check_records_a_baseline_and_dispatches_nothing(kv, clock, tracker):
    """FR-003. Turning the watch on must not wake the agent to answer a conversation that
    finished days ago."""
    c = cfg()
    park(kv, clock, tracker, c)
    fx = WatchEffects(comments=[comment("2026-08-01T10:00:00Z")])
    entry = watch(kv, clock, tracker, c, fx)
    assert fx.spawned == []
    assert entry["pr_comment_watermark"] == "2026-08-01T10:00:00Z"


def test_a_comment_after_the_run_ended_but_before_the_first_check_is_answered(kv, clock, tracker):
    """A reviewer commented one second after the PR was ready, before the daemon parked the
    issue (found by the mocked certification). The first check took it as backlog, and the
    question was never answered. The backlog ends where the run stopped reading."""
    c = cfg()
    park(kv, clock, tracker, c)
    fx = WatchEffects(comments=[comment("2026-08-01T10:00:00Z"), comment("2026-08-01T12:00:01Z")])
    entry = watch(kv, clock, tracker, c, fx)
    assert fx.spawned == [("i1", 1)], "the comment nobody read is answered on the first check"
    assert entry["mode"] == prwatch.ANSWER_MODE
    assert entry["pr_comment_watermark"] == "2026-08-01T12:00:01Z"
    in_last_second = [comment("2026-08-01T10:00:00Z"), comment("2026-08-01T12:00:00.4Z")]
    assert prwatch.baseline({"last_event_at": RUN_ENDED_MS + 900}, in_last_second) \
        == "2026-08-01T12:00:00.4Z", "a comment in the run's last second is backlog"


def test_pause_preserves_unanswered_review_without_spawning(kv, clock, tracker):
    c = cfg()
    park(kv, clock, tracker, c)
    baseline = "2026-08-01T10:00:00Z"
    watch(kv, clock, tracker, c, WatchEffects(comments=[comment(baseline)]))
    fx = WatchEffects(comments=[comment(baseline), comment("2026-08-07T09:00:00Z")])
    clock.advance(200_000)
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1", admission_open=lambda: False)
    assert fx.spawned == []
    entry = kv.get(state.issue_key("i1"))
    assert entry["status"] == PARKED
    assert entry["pr_comment_watermark"] == baseline


def test_a_comment_newer_than_the_baseline_dispatches_an_answer(kv, clock, tracker):
    c = cfg()
    park(kv, clock, tracker, c)
    fx = WatchEffects(comments=[comment("2026-08-01T10:00:00Z")])
    watch(kv, clock, tracker, c, fx)

    fx2 = WatchEffects(comments=[comment("2026-08-01T10:00:00Z"), comment("2026-08-07T09:00:00Z")])
    entry = watch(kv, clock, tracker, c, fx2)
    assert fx2.spawned == [("i1", 1)]
    assert entry["mode"] == prwatch.ANSWER_MODE
    assert entry["pr_comment_watermark"] == "2026-08-07T09:00:00Z"
    assert "answer_dispatch" in fx2.names()


# -- who counts -------------------------------------------------------------


def test_machine_comments_never_wake_the_agent(kv, clock, tracker):
    """FR-002. The agent's own identity and the merger are both machine accounts;
    answering either is a conversation with itself."""
    c = cfg()
    park(kv, clock, tracker, c)
    watch(kv, clock, tracker, c, WatchEffects(comments=[comment("2026-08-01T10:00:00Z")]))

    fx = WatchEffects(comments=[
        comment("2026-08-01T10:00:00Z"),
        comment("2026-08-07T09:00:00Z", user=BOT, body="**symphony-merger** on `abc123`: blocked"),
    ])
    watch(kv, clock, tracker, c, fx)
    assert fx.spawned == []


def test_an_approval_with_no_body_is_a_verdict_not_a_question(kv, clock, tracker):
    c = cfg()
    park(kv, clock, tracker, c)
    watch(kv, clock, tracker, c, WatchEffects(comments=[comment("2026-08-01T10:00:00Z")]))
    fx = WatchEffects(comments=[
        comment("2026-08-01T10:00:00Z"),
        {"created_at": None, "user": HUMAN, "body": ""},
    ])
    watch(kv, clock, tracker, c, fx)
    assert fx.spawned == []


# -- throttle and failure ---------------------------------------------------


def test_checks_are_throttled_per_issue(kv, clock, tracker):
    """FR-004. The tick runs every 30s and the code host's rate limit is shared."""
    c = cfg(review_watch_interval_s=600)
    park(kv, clock, tracker, c)
    fx = WatchEffects(comments=[comment("2026-08-01T10:00:00Z")])
    watch(kv, clock, tracker, c, fx, advance=700_000)
    assert fx.conversation_calls == 1
    for _ in range(5):
        watch(kv, clock, tracker, c, fx, advance=30_000)
    assert fx.conversation_calls == 1, "still inside the interval"
    watch(kv, clock, tracker, c, fx, advance=700_000)
    assert fx.conversation_calls == 2


def test_a_code_host_outage_leaves_the_tick_and_the_entry_alone(kv, clock, tracker):
    """FR-005. A watch that can break a polling cycle is worse than the silence it fixes."""
    c = cfg()
    park(kv, clock, tracker, c)
    fx = WatchEffects(raises=True)
    entry = watch(kv, clock, tracker, c, fx)
    assert entry["status"] == PARKED
    assert fx.spawned == []
    assert "review_watch_unavailable" in fx.names()


def test_a_broken_repo_is_not_retried_every_tick(kv, clock, tracker):
    """SC-006: the throttle timestamp is written before the call, so a permanently failing
    repository costs one check per interval, not one per tick."""
    c = cfg(review_watch_interval_s=600)
    park(kv, clock, tracker, c)
    fx = WatchEffects(raises=True)
    watch(kv, clock, tracker, c, fx, advance=700_000)
    for _ in range(5):
        watch(kv, clock, tracker, c, fx, advance=30_000)
    assert fx.pull_calls == 1


def test_an_unidentifiable_pull_request_stays_parked_quietly(kv, clock, tracker):
    c = cfg()
    park(kv, clock, tracker, c)
    fx = WatchEffects(pulls=[{"number": 9, "title": "unrelated work"}])
    entry = watch(kv, clock, tracker, c, fx)
    assert entry["status"] == PARKED
    assert fx.spawned == []
    assert "review_watch_unavailable" not in fx.names()  # not an error, just nothing to do


# -- exclusivity ------------------------------------------------------------


def test_no_second_run_while_one_is_already_answering(kv, clock, tracker):
    """FR-012."""
    c = cfg()
    park(kv, clock, tracker, c)
    watch(kv, clock, tracker, c, WatchEffects(comments=[comment("2026-08-01T10:00:00Z")]))
    later = [comment("2026-08-01T10:00:00Z"), comment("2026-08-07T09:00:00Z")]
    watch(kv, clock, tracker, c, WatchEffects(comments=later))

    entry = kv.get(state.issue_key("i1"))
    entry["runner_call_id"] = "fc-answer"
    kv.put(state.issue_key("i1"), entry)

    fx = WatchEffects(comments=[*later, comment("2026-08-07T09:30:00Z")], running={"fc-answer"})
    watch(kv, clock, tracker, c, fx)
    assert fx.spawned == []


def test_answering_does_not_consume_implementation_attempts(kv, clock, tracker):
    """FR-010. A chatty reviewer must not be able to exhaust an issue's retries."""
    c = cfg()
    before = park(kv, clock, tracker, c)
    watch(kv, clock, tracker, c, WatchEffects(comments=[comment("2026-08-01T10:00:00Z")]))
    after = watch(kv, clock, tracker, c, WatchEffects(
        comments=[comment("2026-08-01T10:00:00Z"), comment("2026-08-07T09:00:00Z")]))
    assert after["attempt"] == before["attempt"]


# -- disabled ---------------------------------------------------------------


def test_disabled_watch_never_touches_the_code_host(kv, clock, tracker):
    """FR-017/SC-005."""
    c = cfg(review_watch_enabled=False)
    park(kv, clock, tracker, c)
    fx = WatchEffects(comments=[comment("2026-08-07T09:00:00Z")])
    entry = watch(kv, clock, tracker, c, fx)
    assert fx.pull_calls == 0 and fx.conversation_calls == 0
    assert fx.spawned == []
    assert entry["status"] == PARKED


# -- the 004 interaction ----------------------------------------------------


def test_answering_does_not_repeat_the_handover_report(kv, clock, tracker):
    """SC-004. An answer run takes the entry out of parked and back; a guard keyed on that
    transition would post a second report for the same work."""
    c = cfg()
    tracker.add_issue("i1", "ENG-i1")
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="running", sandbox_name="sb-1", target_repo="acme/api",
                 tokens={"total": 100, "cost_estimate": 0.1})
    kv.put(state.issue_key("i1"), entry)
    tracker.set_state("i1", "In Review")

    first = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), first, owner="d1")
    assert len(getattr(first, "reported", [])) == 1

    # two answer cycles, each parking the issue again
    for i, stamp in enumerate(("2026-08-07T09:00:00Z", "2026-08-07T10:00:00Z")):
        watch(kv, clock, tracker, c, WatchEffects(comments=[comment("2026-08-01T00:00:00Z")]))
        watch(kv, clock, tracker, c, WatchEffects(
            comments=[comment("2026-08-01T00:00:00Z"), comment(stamp)]))
        e = kv.get(state.issue_key("i1"))
        e.update(status=PARKED, mode=None, runner_call_id=None)  # as the runner leaves it
        kv.put(state.issue_key("i1"), e)
        after = FakeEffects()
        clock.advance(1_000)
        tick(kv, tracker, c, clock.now_ms(), after, owner="d1")
        assert getattr(after, "reported", []) == [], f"cycle {i} re-reported"


def test_real_rework_still_re_arms_the_report(kv, clock, tracker):
    """FR-016 — the marker must not outlive the body of work it describes."""
    c = cfg()
    park(kv, clock, tracker, c)
    tracker.set_state("i1", "Rework")
    clock.advance(1_000)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    entry = kv.get(state.issue_key("i1"))
    assert not entry.get("session_reported"), "a fresh claim carries no marker"

    entry.update(sandbox_name="sb-2", tokens={"total": 50, "cost_estimate": 0.05})
    kv.put(state.issue_key("i1"), entry)
    tracker.set_state("i1", "In Review")
    clock.advance(1_000)
    fx = FakeEffects()
    tick(kv, tracker, c, clock.now_ms(), fx, owner="d1")
    assert len(getattr(fx, "reported", [])) == 1


# -- webhook wakes and multi-repository tickets ------------------------------


def test_a_github_comment_wake_checks_the_named_pull_request_before_the_throttle(kv, clock, tracker):
    """A webhook names the pull request that got a comment; waiting out the per-issue
    throttle (minutes) would make the answer look asleep."""
    c = cfg(review_watch_interval_s=3600)
    park(kv, clock, tracker, c)
    watch(kv, clock, tracker, c, WatchEffects(comments=[comment("2026-08-01T10:00:00Z")]))
    newer = [comment("2026-08-01T10:00:00Z"), comment("2026-08-07T09:00:00Z")]

    quiet = WatchEffects(comments=newer)
    clock.advance(1_000)
    tick(kv, tracker, c, clock.now_ms(), quiet, owner="d1")
    assert quiet.conversation_calls == 0 and quiet.spawned == []  # throttled

    other = WatchEffects(comments=newer)
    clock.advance(1_000)
    tick(kv, tracker, c, clock.now_ms(), other, owner="d1",
         wake_reasons=[prwatch.comment_wake("acme/api", 99), "linear:update:ENG-i1"])
    assert other.conversation_calls == 0 and other.spawned == []  # another PR: still throttled

    woken = WatchEffects(comments=newer)
    clock.advance(1_000)
    tick(kv, tracker, c, clock.now_ms(), woken, owner="d1",
         wake_reasons=[prwatch.comment_wake("Acme/API", 7)])
    assert woken.spawned == [("i1", 1)]


def test_comment_wake_reasons_parse_and_ignore_everything_else():
    reasons = [prwatch.comment_wake("Acme/Web", 12), "github:comment:acme/x#", "linear:create:ENG-1",
               "github:comment:acme/api#3", 42]
    assert prwatch.parse_comment_wakes(reasons) == {("acme/web", 12), ("acme/api", 3)}


class MultiRepoEffects(WatchEffects):
    def __init__(self, by_repo, **kw):
        super().__init__(**kw)
        self.by_repo = by_repo  # repo -> (pulls, comments)
        self.conversations = []

    def open_pulls(self, repo):
        return self.by_repo[repo][0]

    def pr_conversation(self, repo, number):
        self.conversations.append((repo, number))
        return self.by_repo[repo][1]


def test_a_multi_repo_ticket_watches_the_pull_request_in_every_repository(kv, clock, tracker):
    c = cfg()
    park(kv, clock, tracker, c)
    entry = kv.get(state.issue_key("i1"))
    kv.put(state.issue_key("i1"), entry | {"target_repos": ["acme/api", "acme/web"]})
    base = "2026-08-01T10:00:00Z"
    first = MultiRepoEffects({
        "acme/api": ([{"number": 7, "title": "ENG-i1 api"}], [comment(base)]),
        "acme/web": ([{"number": 3, "title": "ENG-i1 web"}], []),
    })
    entry = watch(kv, clock, tracker, c, first)
    assert entry["pr_numbers"] == {"acme/api": 7, "acme/web": 3}
    assert entry["pr_comment_watermark"] == base and first.spawned == []

    # the question lands on the SECONDARY repository's pull request
    second = MultiRepoEffects({
        "acme/api": ([], [comment(base)]),
        "acme/web": ([], [comment("2026-08-07T09:00:00Z")]),
    })
    entry = watch(kv, clock, tracker, c, second)
    assert second.conversations == [("acme/api", 7), ("acme/web", 3)]
    assert second.spawned == [("i1", 1)]
    assert entry["pr_comment_watermark"] == "2026-08-07T09:00:00Z"
