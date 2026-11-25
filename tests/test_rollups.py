"""Daily rollups (`symphony.rollups`): the live bumps, the history buckets, the backfill and
`/v1/telemetry/history|backfill`."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

from tests.api_support import NS, bearer, client_for, make_services, tokens_for
from tests.test_dispatch import FakeEffects, cfg

from symphony import backup, questions, rollups, state
from symphony.kv import RedisKV
from symphony.orchestrator import tick
from symphony.runner import _finish
from symphony.sessions import SessionStore


def ms(*args) -> int:
    return int(dt.datetime(*args, tzinfo=dt.UTC).timestamp() * 1000)


MONDAY = ms(2026, 11, 16, 0, 30)  # a Monday, 00:30 UTC
DAY = 24 * 3600 * 1000


def usage_frame(inp=100, out=20, cache_read=300, cache_write=5, cost=0.0123) -> dict:
    return {"type": "message_end", "message": {"role": "assistant", "content": [], "usage": {
        "input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": cache_write,
        "totalTokens": inp + out + cache_read + cache_write, "cost": {"total": cost}}}}


class Clock:
    def __init__(self, at: int) -> None:
        self.at = at

    def __call__(self) -> int:
        return self.at


def day_hash(client, at_ms: int) -> dict[str, int]:
    return {k: int(v) for k, v in client.hgetall(rollups.day_key(rollups.day_of(at_ms))).items()}


def run_session(sessions: SessionStore, clock: Clock, sid: str, *, kind="implement",
                model="anthropic/claude-x", repo="acme/app", outcome="done", frames=2,
                start=MONDAY, minutes=20) -> None:
    clock.at = start
    sessions.open(sid, kind=kind, model=model, repo=repo, identifier="ENG-1")
    for _ in range(frames):
        sessions.append_event(sid, "message_end", usage_frame())
    clock.at = start + minutes * 60_000
    sessions.close(sid, outcome)


# -- live bumps -----------------------------------------------------------------------------
def test_an_ended_session_lands_in_its_day_once(redis_client):
    clock = Clock(MONDAY)
    sessions = SessionStore(redis_client, NS, now_ms=clock)
    run_session(sessions, clock, "iss-1:1")
    sessions.close("iss-1:1", "error")  # closed again (an orphan sweep): counted once
    doc = day_hash(redis_client, MONDAY)
    assert doc == {
        "sessions:implement": 1, "outcome:done": 1, "hour:0": 1,
        "tokens_input": 200, "tokens_output": 40, "tokens_cache_read": 600,
        "tokens_cache_write": 10, "cost_micro_usd": 24600,
        "model:anthropic/claude-x:tokens": 850, "model:anthropic/claude-x:cost_micro_usd": 24600,
        "repo:acme/app:sessions": 1, "repo:acme/app:cost_micro_usd": 24600,
        "names:model": 1, "names:repo": 1,
    }
    assert sessions.meta("iss-1:1")["usage"] == {
        "input": 200, "output": 40, "cache_read": 600, "cache_write": 10, "cost_micro_usd": 24600}
    assert redis_client.zrange(state.METRICS_DAYS_KEY, 0, -1) == ["20261116"]


def test_a_replayed_bump_counts_once_and_names_are_capped(redis_client):
    assert rollups.bump(redis_client, MONDAY, {"dispatched": 1}, once="dispatch:a:1")
    assert not rollups.bump(redis_client, MONDAY + 1000, {"dispatched": 1}, once="dispatch:a:1")
    for n in range(rollups.NAMED_LIMIT + 2):
        rollups.bump(redis_client, MONDAY, [(f"model:m{n}:tokens", 1), (f"model:m{n}:cost_micro_usd", 2)])
    doc = day_hash(redis_client, MONDAY)
    assert doc["dispatched"] == 1
    assert doc["names:model"] == rollups.NAMED_LIMIT
    assert sum(1 for f in doc if f.endswith(":tokens")) == rollups.NAMED_LIMIT + 1
    assert (doc["model:other:tokens"], doc["model:other:cost_micro_usd"]) == (2, 4)
    # the in-memory twin (fallback and backfill) applies the same rule
    mirror: dict[str, int] = {"dispatched": 1}
    for n in range(rollups.NAMED_LIMIT + 2):
        rollups.apply(mirror, [(f"model:m{n}:tokens", 1), (f"model:m{n}:cost_micro_usd", 2)])
    assert mirror == doc


def test_a_dispatch_counts_in_the_day_of_the_tick(redis_client, clock, tracker):
    kv = RedisKV(redis_client)
    tracker.add_issue("i1", "ENG-1")
    tracker.add_issue("i2", "ENG-2", state=cfg().tracker_states["rework"])
    for _ in range(2):  # the second tick dispatches nothing new
        tick(kv, tracker, cfg(), clock.now_ms(), FakeEffects(), owner="d1")
        clock.advance(30_000)
    assert day_hash(redis_client, clock.now_ms()) == {"dispatched": 2, "rework": 1}
    assert kv.get(state.metrics_key(rollups.day_of(clock.now_ms())[:6]))["dispatched"] == 2


def test_an_attempt_blocked_on_a_person_counts_once(redis_client, clock, tracker):
    kv = RedisKV(redis_client)
    tracker.add_issue("i1", "ENG-1")
    tick(kv, tracker, cfg(), clock.now_ms(), FakeEffects(), owner="d1")
    issue = tracker.fetch_issues_by_ids(["i1"])[0]
    runner = SimpleNamespace(kv=kv, cfg=cfg(), now_ms=clock.now_ms)
    for _ in range(2):  # the runner's finish, replayed
        kv.put(state.issue_key("i1"), {**kv.get(state.issue_key("i1")), "status": "running"})
        assert _finish(runner, "i1", "blocked", issue, "which database?") == "blocked"
    assert day_hash(redis_client, clock.now_ms())["blocked_on_person"] == 1


def test_questions_asked_and_answered(redis_client):
    questions.ask(redis_client, NS, qid="q1", issue_id="i1", identifier="ENG-1", sid="i1:1",
                  url=None, question="Which?", options=["a", "b"], to=None, now_ms=MONDAY)
    won, _ = questions.submit(redis_client, NS, "q1", "a", by="alice", via="slack",
                              now_ms=MONDAY + 90_000)
    assert won
    assert not questions.submit(redis_client, NS, "q1", "b", by="bob", via="linear",
                                now_ms=MONDAY + 99_000)[0]
    doc = day_hash(redis_client, MONDAY)
    assert doc == {"questions:asked": 1, "questions:answered": 1,
                   "questions:answered_via:slack": 1, "questions:answer_ms_sum": 90_000}


# -- history --------------------------------------------------------------------------------
def test_history_buckets_zero_fill_and_derived_fields(redis_client):
    rollups.bump(redis_client, MONDAY, {"dispatched": 4, "rework": 1, "cost_micro_usd": 1_500_000,
                                        "sessions:implement": 2, "outcome:done": 2,
                                        "prs_merged": 1, "time_to_merge_ms_sum": 600_000,
                                        "time_to_merge_count": 1})
    rollups.bump(redis_client, MONDAY + 9 * DAY, {"dispatched": 1, "sessions:review": 1,
                                                  "questions:answered": 2,
                                                  "questions:answer_ms_sum": 60_000})
    doc = rollups.history(redis_client, first=None, last=dt.date(2026, 11, 25), bucket=None,
                          observed_at="now")
    assert (doc["from"], doc["to"], doc["bucket"], doc["days"]) == ("2026-11-16", "2026-11-25", "day", 2)
    assert len(doc["points"]) == 10 and doc["points"][1]["sessions"] == 0
    first = doc["points"][0]
    assert (first["rework_rate"], first["cost_usd"], first["mean_time_to_merge_ms"]) == (0.25, 1.5, 600_000)
    assert doc["totals"]["sessions_by_kind"] == {"implement": 2, "review": 1}
    assert doc["totals"]["mean_answer_ms"] == 30_000
    assert [k["kind"] for k in doc["by_kind"]] == ["implement", "review"]
    assert doc["by_kind"][1]["series"][9] == 1

    weeks = rollups.history(redis_client, first=dt.date(2026, 11, 16), last=dt.date(2026, 11, 25),
                            bucket="week", observed_at="now")
    assert [(p["start"], p["end"]) for p in weeks["points"]] == [
        ("2026-11-16", "2026-11-22"), ("2026-11-23", "2026-11-25")]
    assert [p["dispatched"] for p in weeks["points"]] == [4, 1]
    months = rollups.history(redis_client, first=None, last=dt.date(2026, 12, 2), bucket="month",
                             observed_at="now")
    assert [p["start"] for p in months["points"]] == ["2026-11-16", "2026-12-01"]
    assert months["totals"]["dispatched"] == 5


def test_history_caps_the_points():
    assert rollups.choose_bucket(dt.date(2026, 1, 1), dt.date(2026, 4, 30), None) == "day"
    assert rollups.choose_bucket(dt.date(2026, 1, 1), dt.date(2026, 6, 30), None) == "week"
    assert rollups.choose_bucket(dt.date(2026, 1, 1), dt.date(2026, 6, 30), "day") == "week"
    assert rollups.choose_bucket(dt.date(2010, 1, 1), dt.date(2026, 6, 30), "day") == "month"


def test_history_keeps_the_latest_points_past_the_cap(redis_client):
    rollups.bump(redis_client, ms(2010, 1, 5), {"dispatched": 1})
    doc = rollups.history(redis_client, first=None, last=dt.date(2026, 11, 25), bucket=None,
                          observed_at="now")
    assert doc["bucket"] == "month" and len(doc["points"]) == rollups.MAX_POINTS
    assert doc["points"][-1]["start"] == "2026-11-01" and doc["totals"]["dispatched"] == 0


def test_activity_heatmap_cells(redis_client):
    clock = Clock(MONDAY)
    sessions = SessionStore(redis_client, NS, now_ms=clock)
    run_session(sessions, clock, "a:1", start=MONDAY)  # Monday 00h
    run_session(sessions, clock, "a:2", start=ms(2026, 11, 22, 23, 10), minutes=5)  # Sunday 23h
    run_session(sessions, clock, "a:3", start=ms(2026, 11, 22, 23, 50), minutes=30)  # ends Monday
    doc = rollups.history(redis_client, first=None, last=dt.date(2026, 11, 23), bucket=None,
                          observed_at="now")
    cells = doc["activity"]["cells"]
    assert (len(cells), len(cells[0])) == (7, 24)
    assert (cells[0][0], cells[6][23], doc["activity"]["total"], doc["activity"]["max"]) == (1, 2, 3, 2)
    assert doc["points"][-1]["sessions"] == 1  # counted in the day it ended


def test_empty_history():
    class Empty:
        def zrange(self, *a):
            return []

    doc = rollups.history(Empty(), first=None, last=dt.date(2026, 11, 25), bucket=None,
                          observed_at="now")
    assert (doc["from"], doc["points"], doc["totals"]["sessions"]) == (None, [], 0)
    assert doc["activity"]["total"] == 0


# -- backfill -------------------------------------------------------------------------------
def live_sessions(redis_client) -> tuple[SessionStore, dict[str, dict[str, int]]]:
    clock = Clock(MONDAY)
    sessions = SessionStore(redis_client, NS, now_ms=clock)
    run_session(sessions, clock, "i1:1")
    run_session(sessions, clock, "merger:acme:app:7:reviewer:1", kind="review", outcome="failed",
                model="openai/gpt-x", start=MONDAY + 3600_000)
    run_session(sessions, clock, "assist:slack:c:t:1", kind="assist", repo=None,
                start=MONDAY + DAY + 7200_000)
    rollups.bump(redis_client, MONDAY, {"dispatched": 1}, once="dispatch:i1:1")
    days = {day: {k: int(v) for k, v in redis_client.hgetall(rollups.day_key(day)).items()}
            for day in ("20261116", "20261117")}
    return sessions, days


def wipe_session_fields(redis_client) -> None:
    for key in redis_client.scan_iter(match=f"{state.METRICS_DAY_PREFIX}*"):
        fields = [f for f in redis_client.hkeys(key) if rollups.SESSION_FIELDS.match(f)]
        if fields:
            redis_client.hdel(key, *fields)
    redis_client.delete(rollups.day_key("20261117"), state.METRICS_ONCE_KEY)


def current(redis_client) -> dict[str, dict[str, int]]:
    return {day: {k: int(v) for k, v in redis_client.hgetall(rollups.day_key(day)).items()}
            for day in ("20261116", "20261117")}


def test_backfill_from_redis_matches_the_live_bumps(redis_client):
    _, live = live_sessions(redis_client)
    wipe_session_fields(redis_client)
    # 16th still has its live `dispatched`: kept without force, rebuilt with it
    result = rollups.backfill(redis_client, NS)
    assert (result["days"], result["kept"], result["sessions"]) == (1, 1, 1)
    assert current(redis_client)["20261117"] == live["20261117"]
    result = rollups.backfill(redis_client, NS, force=True)
    assert (result["days"], result["sessions"]) == (2, 3)
    assert current(redis_client) == live
    rollups.backfill(redis_client, NS, force=True)  # idempotent
    assert current(redis_client) == live
    # a session already backfilled is not counted again when closed again
    SessionStore(redis_client, NS).close("i1:1", "done")
    assert current(redis_client)["20261116"] == live["20261116"]


def test_backfill_from_the_archive_matches_the_live_bumps(redis_client, tmp_path):
    _, live = live_sessions(redis_client)
    target = backup.DirTarget(str(tmp_path), NS)
    assert backup.archive(redis_client, NS, target)["sessions"] == 3
    for key in redis_client.scan_iter(match=f"sym:{NS}:session*"):
        redis_client.delete(key)
    wipe_session_fields(redis_client)
    result = rollups.backfill(redis_client, NS, target, force=True)
    assert (result["archive_objects"], result["sessions"]) == (1, 3)
    assert current(redis_client) == live


def test_backfill_reads_usage_from_events_of_old_sessions(redis_client):
    """A session that ended before `meta.usage` existed: its message_end events count."""
    clock = Clock(MONDAY)
    sessions = SessionStore(redis_client, NS, now_ms=clock)
    run_session(sessions, clock, "old:1")
    live = current(redis_client)["20261116"]
    meta = sessions.meta("old:1")
    del meta["usage"]
    redis_client.set(f"sym:{NS}:session:old:1:meta", json.dumps(meta))
    wipe_session_fields(redis_client)
    redis_client.delete(rollups.day_key("20261116"))
    rollups.backfill(redis_client, NS)
    assert current(redis_client)["20261116"] == live


# -- API ------------------------------------------------------------------------------------
def test_history_and_backfill_routes(redis_client):
    services = make_services(redis_client)
    tokens = tokens_for(services)
    api = client_for(services)
    rollups.bump(redis_client, ms(2026, 11, 20), {"dispatched": 2})
    assert api.get("/v1/telemetry/history").status_code == 401
    response = api.get("/v1/telemetry/history?from=2026-11-19&to=2026-11-21",
                       headers=bearer(tokens["viewer"]))
    assert response.status_code == 200
    doc = response.json()
    assert [p["dispatched"] for p in doc["points"]] == [0, 2, 0]
    bad = api.get("/v1/telemetry/history?from=2026-11-22&to=2026-11-21",
                  headers=bearer(tokens["viewer"]))
    assert bad.status_code == 422
    assert api.get("/v1/telemetry/history?bucket=hour",
                   headers=bearer(tokens["viewer"])).status_code == 422

    assert api.post("/v1/telemetry/backfill").status_code == 401
    assert api.post("/v1/telemetry/backfill", headers=bearer(tokens["viewer"])).status_code == 403
    response = api.post("/v1/telemetry/backfill?force=true", headers=bearer(tokens["admin"]))
    assert response.status_code == 200
    assert response.json() == {"days": 0, "kept": 0, "sessions": 0, "archive_objects": 0,
                               "force": True, "target": None}
    actions = [e["action"] for e in services.store.audit(limit=50)["items"]]
    assert "telemetry.backfill" in actions
