"""Read projections (`/v1/repos|infra|telemetry|learning`), rate limits, evidence, audit paging
and the API's patience with a Redis that is still starting."""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest
import redis
from tests.api_support import bearer, client_for, fresh_daemon, make_services, tokens_for

from symphony import repos, state
from symphony.api.app import wait_for_redis


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client)
    return services, client_for(services), tokens_for(services)


def now_ms() -> int:
    return int(time.time() * 1000)


def test_overview_reports_rate_limits_seen_in_the_last_hour(api, redis_client):
    _, client, tokens = api
    key = "sym:test:ratelimits"
    redis_client.hset(key, mapping={
        "amazon-bedrock/opus": json.dumps({
            "stage": "throttled", "remaining": 0, "limit": 100, "reset_at": now_ms() + 60_000,
            "retry_after_ms": 30_000, "observed_at": now_ms() - 5_000, "source": "retry"}),
        "anthropic/sonnet": json.dumps({
            "stage": "warning", "remaining": 12, "observed_at": now_ms() - 2 * 3_600_000,
            "source": "usage_limit"}),
        "broken/model": "not json",
    })
    body = client.get("/v1/overview", headers=bearer(tokens["viewer"])).json()
    [limit] = body["rate_limits"]
    assert limit["model"] == "amazon-bedrock/opus" and limit["stage"] == "throttled"
    assert (limit["remaining"], limit["limit"], limit["retry_after_ms"]) == (0, 100, 30_000)
    assert limit["reset_at"].endswith("Z") and limit["observed_at"].endswith("Z")
    assert limit["source"] == "retry"
    mcp = client.post("/mcp", headers=bearer(tokens["viewer"]), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "overview", "arguments": {}}})
    assert mcp.json()["result"]["structuredContent"]["rate_limits"][0]["model"] == limit["model"]


def test_session_evidence_passes_through_and_issues_count_it(api, redis_client):
    services, client, tokens = api
    redis_client.set(state.issue_key("i1"), json.dumps(
        {"dispatch_seq": 1, "attempt": 1, "identifier": "ENG-1", "status": "running"}))
    services.sessions.open("i1:1", kind="implement")
    evidence = [{"title": "Login works", "kind": "screenshot", "url": "https://uploads.linear.app/a"},
                {"title": "Test log", "kind": "log", "url": "https://uploads.linear.app/b"}]
    services.sessions.update("i1:1", evidence=evidence)
    headers = bearer(tokens["viewer"])
    detail = client.get("/v1/sessions/i1:1", headers=headers).json()
    assert detail["meta"]["evidence"] == evidence
    [item] = client.get("/v1/issues", headers=headers).json()["items"]
    assert item["evidence_count"] == 2


def test_projection_routes_are_sanitized_and_explain_their_sources(api, redis_client):
    services, client, tokens = api
    fresh_daemon(redis_client)
    redis_client.set(state.issue_key("i1"), json.dumps({
        "dispatch_seq": 1, "attempt": 1, "identifier": "ENG-1", "status": "running",
        "target_repo": "acme/app", "runtime": "omp", "failure_summary": "password=hunter2"}))
    redis_client.set(state.TOTALS_KEY, json.dumps(
        {"input": 10, "output": 5, "total": 15, "cost_estimate": 0.5}))
    redis_client.set(state.telemetry_key("i1"), json.dumps(
        {"by_model": {"opus": {"requests": 2, "input": 10, "output": 5}}}))
    month = dt.datetime.now(dt.UTC).strftime("%Y%m")
    redis_client.set(state.metrics_key(month), json.dumps({"dispatched": 3, "prs_merged": 1}))
    redis_client.set(state.retro_key("i9"), json.dumps({
        "identifier": "ENG-9", "status": "done", "outcome": "nothing-to-learn",
        "target_repo": "acme/app", "at": now_ms()}))
    services.sessions.open("i1:1", kind="implement", identifier="ENG-1", model="opus")
    services.sessions.update("i1:1", turn=3, tokens={"input": 10, "output": 5, "total": 15,
                                                     "cost": 0.5})
    headers = bearer(tokens["viewer"])

    repos = client.get("/v1/repos", headers=headers).json()
    assert [r["name"] for r in repos["items"]] == ["acme/app"]
    assert repos["items"][0]["issues"] == 1 and repos["items"][0]["runtime"] == "omp"
    assert repos["items"][0]["routes"] == ["issue", "retrospective"]
    assert repos["sources"]["workflow"]["status"] == "unavailable"

    infra = client.get("/v1/infra", headers=headers).json()
    assert infra["services"] == [
        {"name": "redis", "role": "redis", "status": "healthy", "detail": "ok"},
        {"name": "daemon", "role": "daemon", "status": "healthy", "detail": "ok"},
    ]
    assert infra["sources"]["kubernetes"]["status"] == "unavailable"
    assert infra["paused"] is False and infra["heartbeatAt"].endswith("Z")

    telemetry = client.get("/v1/telemetry?limit=10", headers=headers).json()
    assert telemetry["totals"]["total"] == 15 and telemetry["totals"]["costEstimate"] == 0.5
    assert telemetry["models"] == [{"model": "opus", "requests": 2, "input": 10, "output": 5}]
    assert telemetry["monthly"][-1]["dispatched"] == 3
    [row] = telemetry["sessions"]
    assert row["sid"] == "i1:1" and row["turn"] == 3
    assert row["tokens"] == {"input": 10, "output": 5, "total": 15, "cost": 0.5}

    learning = client.get("/v1/learning", headers=headers).json()
    [item] = learning["items"]
    assert (item["status"], item["outcome"], item["provenance"]) == (
        "completed", "nothing_to_learn", "retained")
    assert item["identifier"] == "ENG-9"
    assert learning["sources"]["learning"]["status"] == "fresh"

    everything = json.dumps([repos, infra, telemetry, learning])
    assert "hunter2" not in everything
    # Only codes reach users; English stays in `sources.*.note`.
    for document in (repos, infra, learning):
        assert all("message" not in source for source in document["sources"].values())
    assert client.get("/v1/repos").status_code == 401


def test_audit_pages_with_a_cursor(api):
    services, client, tokens = api
    for n in range(5):
        services.store.audit_event(actor="alice", action="token.create", target=f"token:{n}",
                                   reason="new teammate")
    headers = bearer(tokens["viewer"])
    first = client.get("/v1/audit?limit=3", headers=headers).json()
    assert len(first["items"]) == 3 and first["nextCursor"]
    second = client.get(f"/v1/audit?limit=3&cursor={first['nextCursor']}", headers=headers).json()
    assert len(second["items"]) == 2 and second["nextCursor"] is None
    ids = [e["id"] for e in first["items"] + second["items"]]
    assert len(set(ids)) == 5
    assert client.get("/v1/audit?cursor=bogus", headers=headers).status_code == 422


class FlakyRedis:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.pings = 0

    def ping(self):
        self.pings += 1
        if self.pings <= self.failures:
            raise redis.ConnectionError("Connection refused")
        return True


def test_startup_waits_for_redis_with_backoff():
    delays = []
    client = FlakyRedis(failures=5)
    wait_for_redis(client, sleep=delays.append, max_delay_s=1.0)
    assert client.pings == 6
    assert delays == [0.1, 0.2, 0.4, 0.8, 1.0]


def test_startup_wait_can_be_bounded():
    with pytest.raises(redis.ConnectionError):
        wait_for_redis(FlakyRedis(failures=10), sleep=lambda _: None, attempts=3)


def test_issue_reason_is_a_code_not_prose(api, redis_client):
    _, client, tokens = api
    redis_client.set(state.issue_key("i1"), json.dumps({
        "dispatch_seq": 1, "attempt": 1, "identifier": "ENG-1", "status": "blocked",
        "undispatchable_reason": "no-repo-designation"}))
    redis_client.set(state.issue_key("i2"), json.dumps(
        {"dispatch_seq": 1, "attempt": 1, "identifier": "ENG-2", "status": "running"}))
    # The router's own reason code (repos.MIXED_OWNERS) reaches clients as a code too.
    redis_client.set(state.issue_key("i3"), json.dumps({
        "dispatch_seq": 1, "attempt": 1, "identifier": "ENG-3", "status": "blocked",
        "undispatchable_reason": repos.MIXED_OWNERS}))
    items = client.get("/v1/issues", headers=bearer(tokens["viewer"])).json()["items"]
    reasons = {item["identifier"]: item["reason"] for item in items}
    assert reasons == {"ENG-1": "no_repo_designation", "ENG-2": None,
                       "ENG-3": "mixed_owner_repo_designation"}
