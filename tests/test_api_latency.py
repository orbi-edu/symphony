"""Hot reads stay fast with a realistic amount of state: 1000 sessions in Redis."""

from __future__ import annotations

import json
import time

import pytest
from tests.api_support import bearer, client_for, fresh_daemon, make_services, tokens_for

from symphony import state


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[int(len(ordered) * 0.95) - 1]


@pytest.fixture
def loaded(redis_client):
    services = make_services(redis_client)
    fresh_daemon(redis_client)
    pipe = redis_client.pipeline()
    for n in range(1000):
        issue_id = f"issue-{n}"
        pipe.set(state.issue_key(issue_id), json.dumps({
            "dispatch_seq": 1, "attempt": 1, "identifier": f"ENG-{n}", "title": f"Ticket {n}",
            "status": ("running", "claimed", "retry_queued", "parked")[n % 4],
            "tracker_state": "In Progress",
        }))
    pipe.execute()
    for n in range(1000):
        sid = f"issue-{n}:1"
        services.sessions.open(sid, kind="implement", identifier=f"ENG-{n}", title=f"Ticket {n}")
        services.sessions.update(sid, state="running", turn=n % 30,
                                 tokens={"input": 900, "output": 100, "total": 1000, "cost": 0.01},
                                 last_message="x" * 280)
    return services, client_for(services), tokens_for(services)


def test_overview_and_session_meta_p95_under_50ms(loaded):
    services, client, tokens = loaded
    headers = bearer(tokens["viewer"])
    client.get("/v1/overview", headers=headers)  # warm up connections
    overview, meta = [], []
    for n in range(60):
        started = time.perf_counter()
        response = client.get("/v1/overview", headers=headers)
        overview.append(time.perf_counter() - started)
        assert response.status_code == 200
        started = time.perf_counter()
        response = client.get(f"/v1/sessions/issue-{n * 7}:1?transcript_limit=0", headers=headers)
        meta.append(time.perf_counter() - started)
        assert response.status_code == 200
    body = client.get("/v1/overview", headers=headers).json()
    assert body["tokens_today"] == 1_000_000
    assert body["counts"]["running"] == 250 and body["counts"]["in_review"] == 250
    assert p95(overview) < 0.050, f"overview p95 {p95(overview) * 1000:.1f} ms"
    assert p95(meta) < 0.050, f"session meta p95 {p95(meta) * 1000:.1f} ms"
