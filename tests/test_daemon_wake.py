"""Wake-driven dispatch (internals §6): a webhook's wake makes the daemon tick NOW, not at
the next safety poll."""

import threading
import time

import pytest

from symphony import state, wake
from symphony.entry.daemon import run_loop

NS = "test"
INTERVAL_S = 30.0  # far longer than any assertion below waits


class Steps:
    def __init__(self, leader=True):
        self.calls: list[tuple[float, list[str]]] = []
        self.leader = leader
        self.event = threading.Event()

    def __call__(self, reasons):
        self.calls.append((time.monotonic(), list(reasons)))
        self.event.set()
        return self.leader

    def wait_for(self, n, timeout):
        deadline = time.monotonic() + timeout
        while len(self.calls) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.event.wait(remaining)
            self.event.clear()
        return True


@pytest.fixture
def loop(redis_client):
    stop = threading.Event()
    started = []

    def start(steps):
        thread = threading.Thread(
            target=run_loop, args=(steps,), daemon=True,
            kwargs={"client": redis_client, "namespace": NS,
                    "interval_s": lambda: INTERVAL_S, "stop": stop},
        )
        thread.start()
        started.append(thread)
        return thread

    yield start
    stop.set()
    wake.wake(redis_client, NS, wake.DAEMON, "shutdown")  # unblock a pending wait
    for thread in started:
        thread.join(timeout=5)


def _p95(samples):
    ordered = sorted(samples)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


def test_wake_to_tick_latency_meets_the_contract_p95(loop, redis_client):
    """internals §6: a webhook's wake reaches the next tick in < 300 ms (p95), with a safety
    interval far longer than that — so only the wake can explain the tick."""
    steps = Steps()
    loop(steps)
    assert steps.wait_for(1, 2)
    latencies = []
    for n in range(20):
        sent = time.monotonic()
        wake.wake(redis_client, NS, wake.DAEMON, f"linear:create:ENG-{n}")
        assert steps.wait_for(n + 2, 2), f"wake {n} did not start a tick"
        at, reasons = steps.calls[n + 1]
        assert reasons == [f"linear:create:ENG-{n}"]
        latencies.append(at - sent)
    assert _p95(latencies) < 0.3, latencies


def test_a_socket_timeout_shorter_than_the_interval_still_blocks_on_wakes(redis_client):
    """Production clients carry a socket timeout (10 s) shorter than a long safety interval.
    One blocking pop longer than that would fail as a Redis error and degrade the loop to
    sleeping — the wait must re-block in slices instead, still waking at once."""
    import redis

    path = redis_client.connection_pool.connection_kwargs["path"]
    client = redis.Redis(unix_socket_path=path, decode_responses=True, socket_timeout=0.5)
    steps = Steps()
    stop = threading.Event()
    thread = threading.Thread(
        target=run_loop, args=(steps,), daemon=True,
        kwargs={"client": client, "namespace": NS, "interval_s": lambda: 3.0, "stop": stop},
    )
    thread.start()
    try:
        assert steps.wait_for(1, 2)
        time.sleep(1.2)  # past two socket timeouts: the wait must still be blocking cleanly
        assert len(steps.calls) == 1  # no premature tick from a failed pop
        sent = time.monotonic()
        wake.wake(redis_client, NS, wake.DAEMON, "github:comment:acme/api#1")
        assert steps.wait_for(2, 2)
        assert steps.calls[1][0] - sent < 0.3
    finally:
        stop.set()
        wake.wake(redis_client, NS, wake.DAEMON, "shutdown")
        thread.join(timeout=5)
        client.close()


def test_wakes_that_arrive_during_a_tick_are_not_lost_and_coalesce(loop, redis_client):
    """A burst of webhooks while the loop is busy costs exactly one extra tick, and that
    tick sees every reason."""
    gate = threading.Event()
    steps = Steps()

    def slow_first(reasons):
        steps(reasons)
        if len(steps.calls) == 1:
            gate.wait(5)
        return True

    loop(slow_first)
    assert steps.wait_for(1, 2)
    for n in range(3):
        wake.wake(redis_client, NS, wake.DAEMON, f"github:comment:acme/api#{n}")
    gate.set()
    assert steps.wait_for(2, 2)
    assert sorted(steps.calls[1][1]) == [f"github:comment:acme/api#{n}" for n in range(3)]
    time.sleep(0.3)
    assert len(steps.calls) == 2  # no tick per wake: the burst coalesced


def test_without_a_wake_the_loop_waits_for_the_safety_poll(redis_client):
    steps = Steps()
    stop = threading.Event()
    thread = threading.Thread(
        target=run_loop, args=(steps,), daemon=True,
        kwargs={"client": redis_client, "namespace": NS, "interval_s": lambda: 0.2, "stop": stop},
    )
    thread.start()
    assert steps.wait_for(3, 3)
    stop.set()
    thread.join(timeout=2)
    gaps = [b[0] - a[0] for a, b in zip(steps.calls, steps.calls[1:], strict=False)]
    assert all(g >= 0.15 for g in gaps[:2]), gaps
    assert all(reasons == [] for _, reasons in steps.calls)


def test_a_standby_daemon_leaves_the_wakes_to_the_leader(loop, redis_client, monkeypatch):
    monkeypatch.setattr(state, "LEASE_TTL_MS", 60_000)
    steps = Steps(leader=False)
    loop(steps)
    assert steps.wait_for(1, 2)
    wake.wake(redis_client, NS, wake.DAEMON, "linear:update:ENG-1")
    time.sleep(0.3)
    assert len(steps.calls) == 1
    assert redis_client.llen(f"sym:{NS}:wake:{wake.DAEMON}") == 1  # still there for the leader


# -- Redis not ready yet -----------------------------------------------------


class RecordingStop(threading.Event):
    """A stop event whose waits return at once and are recorded — backoff without sleeping."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


class FlakyRedis:
    def __init__(self, failures):
        self.failures = failures
        self.pings = 0

    def ping(self):
        self.pings += 1
        if self.pings <= self.failures:
            import redis

            raise redis.ConnectionError("Connection refused")
        return True


def test_startup_waits_for_redis_with_capped_exponential_backoff():
    from symphony.entry.daemon import wait_for_redis

    stop, client, beats = RecordingStop(), FlakyRedis(failures=7), []
    assert wait_for_redis(client, stop, initial_s=0.2, max_s=5.0, on_attempt=lambda: beats.append(1))
    assert client.pings == 8
    assert stop.waits == [0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]
    assert len(beats) == 8  # the liveness beat stays fresh while waiting


def test_waiting_for_redis_ends_when_the_daemon_stops():
    from symphony.entry.daemon import wait_for_redis

    stop = RecordingStop()
    stop.set()
    assert wait_for_redis(FlakyRedis(failures=10**6), stop) is False


def test_a_step_that_fails_on_redis_does_not_end_the_loop(loop, redis_client):
    import redis

    steps = Steps()
    failed = []

    def step(reasons):
        if not failed:
            failed.append(True)
            raise redis.ConnectionError("Redis is loading the dataset in memory")
        return steps(reasons)

    loop(step)
    assert steps.wait_for(1, 2), "the loop did not recover from a failed step"
    wake.wake(redis_client, NS, wake.DAEMON, "linear:update:ENG-1")
    assert steps.wait_for(2, 2)
