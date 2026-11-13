"""Shared Redis stores: live sessions, API tokens, team configuration, wake-ups."""

import threading
import time

import pytest

from symphony import apitokens, teamconfig, wake
from symphony.sessions import EV_INTENT_APPLIED, EV_STATE, SessionStore

NS = "test"


# -- sessions -----------------------------------------------------------------


def test_a_session_lists_newest_first_and_filters_by_state(redis_client):
    clock = iter(range(1000, 10_000, 10))
    store = SessionStore(redis_client, NS, now_ms=lambda: next(clock))
    store.open("i1:1", identifier="ENG-1", state="running")
    store.open("i2:1", identifier="ENG-2", state="running")
    store.update("i1:1", state="ended")
    assert [s["sid"] for s in store.list()] == ["i1:1", "i2:1"]
    assert [s["sid"] for s in store.list(states=["running"])] == ["i2:1"]


def test_a_tail_resumes_after_the_last_seen_event_and_never_repeats(redis_client):
    store = SessionStore(redis_client, NS)
    store.open("i1:1", state="running")
    first = store.read_events("i1:1")
    assert [k for _, k, _ in first] == [EV_STATE]
    store.append_event("i1:1", "message_update", {"delta": "hello"})
    tail = store.read_events("i1:1", after=first[-1][0])
    assert [(k, d) for _, k, d in tail] == [("message_update", {"delta": "hello"})]
    assert store.read_events("i1:1", after=tail[-1][0]) == []


def test_a_blocked_tail_wakes_on_the_next_event(redis_client):
    """SSE and the ACP bridge tail with a blocking read: a new event must arrive without
    waiting for any poll interval."""
    store = SessionStore(redis_client, NS)
    store.open("i1:1", state="running")
    last = store.last_event_id("i1:1")
    threading.Timer(0.05, lambda: store.append_event("i1:1", "agent_end", {})).start()
    start = time.monotonic()
    rows = store.read_events("i1:1", after=last, block_ms=2000)
    assert [k for _, k, _ in rows] == ["agent_end"]
    assert time.monotonic() - start < 1


def test_an_oversized_event_is_replaced_by_a_marker(redis_client):
    store = SessionStore(redis_client, NS)
    store.append_event("s", "tool_execution_end", {"type": "x", "out": "a" * 100_000})
    (_, _, data), = store.read_events("s")
    assert data == {"truncated": True, "bytes": data["bytes"], "type": "x"}


def test_the_transcript_keeps_only_its_tail(redis_client):
    store = SessionStore(redis_client, NS, max_transcript=3)
    store.append_transcript("s", [{"n": i} for i in range(5)])
    assert store.transcript("s") == [{"n": 2}, {"n": 3}, {"n": 4}]


def test_intents_are_read_after_the_runners_cursor(redis_client):
    store = SessionStore(redis_client, NS)
    cursor = store.last_intent_id("s")
    store.send_intent("s", "steer", actor="alice", text="also add a test")
    (iid, kind, actor, payload), = store.read_intents("s", after=cursor)
    assert (kind, actor, payload) == ("steer", "alice", {"text": "also add a test"})
    store.intent_applied("s", iid, kind)
    assert store.read_events("s")[-1][1] == EV_INTENT_APPLIED
    with pytest.raises(ValueError):
        store.send_intent("s", "rm -rf", actor="alice")


def test_an_input_request_is_visible_until_answered(redis_client):
    store = SessionStore(redis_client, NS)
    store.open("s", state="running")
    store.request_input("s", "q1", "Which database?", ["postgres", "sqlite"])
    assert store.meta("s")["state"] == "waiting_input"
    assert store.meta("s")["input_request"]["question"] == "Which database?"
    store.resolve_input("s", "q1", answer="postgres", by="alice")
    assert store.meta("s")["state"] == "running" and store.meta("s")["input_request"] is None


# -- tokens -------------------------------------------------------------------


def test_a_token_is_shown_once_and_stored_only_as_a_hash(redis_client):
    tokens = apitokens.TokenStore(redis_client, NS)
    token, record = tokens.create("alice", "operator", actor="admin")
    assert token.startswith("sym_") and "hash" not in record
    assert token.split("_")[2] not in str(redis_client.hgetall(tokens.key))
    assert tokens.verify(token)["role"] == "operator"
    assert tokens.verify(token[:-1] + ("A" if token[-1] != "A" else "B")) is None


def test_revocation_and_expiry_apply_on_the_next_request(redis_client):
    now = [1_000_000]
    tokens = apitokens.TokenStore(redis_client, NS, now_ms=lambda: now[0])
    revoked, record = tokens.create("bob", "viewer", actor="admin")
    tokens.revoke(record["id"], actor="admin")
    assert tokens.verify(revoked) is None
    expiring, _ = tokens.create("ci", "viewer", actor="admin", expires_in_s=60)
    assert tokens.verify(expiring) is not None
    now[0] += 61_000
    assert tokens.verify(expiring) is None


def test_role_order():
    assert apitokens.role_allows("admin", "operator")
    assert not apitokens.role_allows("viewer", "operator")


# -- team configuration ---------------------------------------------------------


def test_a_publication_is_a_new_generation_consumers_report(redis_client):
    cfg = teamconfig.TeamConfig(redis_client, NS)
    doc = cfg.publish(
        {"models": {"implementer": {"thinking": "xhigh"}}},
        expected_generation=0, actor="alice", reason="Harder tickets this sprint",
    )
    assert doc["generation"] == 1
    assert cfg.model("implementer")["thinking"] == "xhigh"
    assert cfg.model("implementer")["model"] == teamconfig.DEFAULT["models"]["implementer"]["model"]
    cfg.observe("runner:ENG-1", 1)
    assert cfg.observed()["runner:ENG-1"]["generation"] == 1
    assert cfg.history()[0]["reason"] == "Harder tickets this sprint"


def test_a_stale_publication_is_refused(redis_client):
    cfg = teamconfig.TeamConfig(redis_client, NS)
    cfg.publish({"omp": {"settings": {"a": 1}}}, expected_generation=0, actor="a", reason="r")
    with pytest.raises(teamconfig.ConflictError):
        cfg.publish({"omp": {"settings": {"a": 2}}}, expected_generation=0, actor="b", reason="r")


def test_the_oracle_cannot_be_the_reviewers_model(redis_client):
    """A finding blocks only when two independent models agree; one model twice is one opinion."""
    cfg = teamconfig.TeamConfig(redis_client, NS)
    reviewer = teamconfig.DEFAULT["models"]["reviewer"]
    with pytest.raises(teamconfig.ConfigError, match="oracle"):
        cfg.publish({"models": {"oracle": dict(reviewer)}}, expected_generation=0, actor="a", reason="r")


# -- wake -----------------------------------------------------------------------


def test_a_wake_ends_the_wait_at_once_and_none_are_lost(redis_client):
    wake.wake(redis_client, NS, wake.DAEMON, "linear:issue-updated")
    wake.wake(redis_client, NS, wake.DAEMON, "linear:issue-created")
    assert wake.wait(redis_client, NS, wake.DAEMON, 5) == [
        "linear:issue-updated", "linear:issue-created",
    ]
    start = time.monotonic()
    assert wake.wait(redis_client, NS, wake.DAEMON, 0.1) == []
    assert time.monotonic() - start < 1


def test_a_wait_longer_than_the_socket_timeout_neither_fails_nor_loses_the_wake(_redis_server):
    """The daemon's client has a 10 s socket timeout and waits 15 s: one long BRPOP raised
    mid-wait and the server could hand a wake to the abandoned pop."""
    import redis

    client = redis.Redis(unix_socket_path=_redis_server, decode_responses=True, socket_timeout=1.5)
    client.delete(f"sym:{NS}:wake:{wake.DAEMON}")
    threading.Timer(1.2, lambda: wake.wake(client, NS, wake.DAEMON, "late")).start()
    assert wake.wait(client, NS, wake.DAEMON, 3) == ["late"]
    start = time.monotonic()
    assert wake.wait(client, NS, wake.DAEMON, 2.2) == []
    assert time.monotonic() - start >= 2.0
    client.close()
