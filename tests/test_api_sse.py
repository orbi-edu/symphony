"""SSE over a real uvicorn server: blocking delivery, resume, pings, end and disconnect."""

from __future__ import annotations

import json
import queue
import threading
import time

import httpx
import pytest
from tests.api_support import Server, bearer, build_app, make_services, tokens_for


@pytest.fixture
def live(redis_client):
    services = make_services(redis_client, ping_interval_s=0.3)
    tokens = tokens_for(services)
    services.sessions.open("s:1", kind="implement", identifier="ENG-1")
    with Server(build_app(services)) as server:
        yield services, server, tokens


class Reader:
    """Reads SSE frames on a thread so the test can time their arrival."""

    def __init__(self, url: str, headers: dict) -> None:
        self.frames: queue.Queue = queue.Queue()
        self.status: int | None = None
        self.client = httpx.Client(timeout=httpx.Timeout(10, read=10))
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(url, headers), daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self, url, headers):
        try:
            with self.client.stream("GET", url, headers=headers) as response:
                self.status = response.status_code
                self.headers = response.headers
                self.ready.set()
                frame: dict = {}
                for line in response.iter_lines():
                    if line == "":
                        if frame:
                            self.frames.put(frame)
                        frame = {}
                    elif line.startswith(":"):
                        self.frames.put({"comment": line[1:].strip()})
                    else:
                        field, _, value = line.partition(":")
                        frame[field] = value.lstrip()
                self.frames.put({"closed": True})
        except httpx.HTTPError:
            self.frames.put({"closed": True})
        finally:
            self.ready.set()

    def next_event(self, timeout=2.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            frame = self.frames.get(timeout=max(0.01, deadline - time.monotonic()))
            if "event" in frame or "closed" in frame:
                return frame

    def close(self):
        self.client.close()


def test_event_appended_after_connect_arrives_fast(live):
    services, server, tokens = live
    reader = Reader(f"{server.url}/v1/sessions/s:1/events", bearer(tokens["viewer"]))
    try:
        assert reader.status == 200
        assert reader.headers["content-type"].startswith("text/event-stream")
        time.sleep(0.1)  # the server is now blocked in XREAD
        sent = time.monotonic()
        event_id = services.sessions.append_event("s:1", "message_update", {"delta": "hi"})
        frame = reader.next_event(timeout=1.0)
        latency = time.monotonic() - sent
        assert latency < 1.0
        assert frame["id"] == event_id
        assert frame["event"] == "message_update"
        assert json.loads(frame["data"]) == {"delta": "hi"}
    finally:
        reader.close()


def test_event_to_viewer_p95_under_300ms(live):
    """internals §6: omp event → Tern under 300 ms. Each event is appended while the server is
    parked in XREAD, so every sample measures a full wake-up and delivery."""
    services, server, tokens = live
    reader = Reader(f"{server.url}/v1/sessions/s:1/events", bearer(tokens["viewer"]))
    try:
        time.sleep(0.1)
        samples = []
        for n in range(25):
            sent = time.monotonic()
            event_id = services.sessions.append_event("s:1", "message_update", {"n": n})
            frame = reader.next_event(timeout=2.0)
            samples.append(time.monotonic() - sent)
            assert frame["id"] == event_id
            time.sleep(0.02)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        assert p95 < 0.300, f"SSE p95 {p95 * 1000:.1f} ms"
    finally:
        reader.close()


def test_last_event_id_resumes_after_the_seen_events(live):
    services, server, tokens = live
    first = services.sessions.append_event("s:1", "a", {"n": 1})
    second = services.sessions.append_event("s:1", "b", {"n": 2})
    third = services.sessions.append_event("s:1", "c", {"n": 3})
    reader = Reader(f"{server.url}/v1/sessions/s:1/events",
                    bearer(tokens["viewer"]) | {"Last-Event-ID": first})
    try:
        assert [reader.next_event()["id"] for _ in range(2)] == [second, third]
    finally:
        reader.close()
    replay = Reader(f"{server.url}/v1/sessions/s:1/events?after=0-0", bearer(tokens["viewer"]))
    try:
        ids = [replay.next_event()["id"] for _ in range(4)]
        assert ids[1:] == [first, second, third]  # ids[0] is the `starting` state event
    finally:
        replay.close()


def test_idle_stream_sends_pings_and_ends_with_the_session(live):
    services, server, tokens = live
    reader = Reader(f"{server.url}/v1/sessions/s:1/events", bearer(tokens["viewer"]))
    try:
        comments = []
        deadline = time.monotonic() + 3
        while not comments and time.monotonic() < deadline:
            frame = reader.frames.get(timeout=3)
            if frame.get("comment") == "ping":
                comments.append(frame)
        assert comments, "no ping on an idle stream"
        services.sessions.close("s:1", "done")
        frame = reader.next_event()
        assert frame["event"] == "symphony.state"
        assert json.loads(frame["data"]) == {"state": "ended"}
        assert reader.next_event().get("closed")
    finally:
        reader.close()


def test_client_disconnect_releases_the_blocking_read(live, redis_client):
    services, server, tokens = live
    for _ in range(50):
        if int(redis_client.info("clients")["blocked_clients"]) == 0:
            break
        time.sleep(0.02)
    reader = Reader(f"{server.url}/v1/sessions/s:1/events", bearer(tokens["viewer"]))
    deadline = time.monotonic() + 2
    while int(redis_client.info("clients")["blocked_clients"]) < 1:
        assert time.monotonic() < deadline, "server never blocked in XREAD"
        time.sleep(0.01)
    reader.close()
    deadline = time.monotonic() + 2
    while int(redis_client.info("clients")["blocked_clients"]) > 0:
        assert time.monotonic() < deadline, "XREAD still blocked after the client left"
        time.sleep(0.01)


def test_events_require_auth_and_an_existing_session(live):
    _, server, tokens = live
    assert httpx.get(f"{server.url}/v1/sessions/s:1/events").status_code == 401
    missing = httpx.get(f"{server.url}/v1/sessions/none:1/events", headers=bearer(tokens["viewer"]))
    assert missing.status_code == 404
    bad = httpx.get(f"{server.url}/v1/sessions/s:1/events",
                    headers=bearer(tokens["viewer"]) | {"Last-Event-ID": "nope"})
    assert bad.status_code == 422
