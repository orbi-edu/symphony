"""Shared fakes: KV with put-if-absent, clock, JSON-RPC streams, tracker, and a disposable Redis."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections import deque

import pytest
import redis

from symphony.trackers.memory import MemoryTracker


@pytest.fixture(scope="session")
def _redis_server():
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server is required for Redis-backed stores")
    directory = tempfile.TemporaryDirectory(prefix="sym-", dir="/tmp")
    socket = f"{directory.name}/redis.sock"
    process = subprocess.Popen(
        [binary, "--port", "0", "--unixsocket", socket, "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(unix_socket_path=socket, decode_responses=True, socket_timeout=5)
    deadline = time.monotonic() + 5
    while True:
        try:
            client.ping()
            break
        except redis.ConnectionError:
            if time.monotonic() >= deadline or process.poll() is not None:
                pytest.fail("Disposable Redis did not start")
            time.sleep(0.02)
    try:
        yield socket
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)
        directory.cleanup()


@pytest.fixture
def redis_client(_redis_server):
    client = redis.Redis(unix_socket_path=_redis_server, decode_responses=True, socket_timeout=5)
    client.flushdb()
    try:
        yield client
    finally:
        client.close()


class FakeKV:
    """The KV semantics that matter here (RedisKV in production): atomic put-if-absent
    and opaque keys."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.put_log: list[tuple[str, bool]] = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def put(self, key, value, *, skip_if_exists: bool = False) -> bool:
        if skip_if_exists and key in self.data:
            self.put_log.append((key, False))
            return False
        self.data[key] = value
        self.put_log.append((key, True))
        return True

    def delete(self, key) -> None:
        self.data.pop(key, None)

    def keys(self):
        return list(self.data.keys())


class FakeClock:
    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.ms = start_ms

    def now_ms(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += ms


class FakeLineWriter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str) -> None:
        self.lines.append(line)

    def sent(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


class FakeLineReader:
    """read_line(timeout) → next line or None (= silence timeout)."""

    def __init__(self) -> None:
        self.queue: deque[str] = deque()

    def push(self, obj: dict) -> None:
        self.queue.append(json.dumps(obj))

    def read_line(self, timeout_s: float) -> str | None:
        if self.queue:
            return self.queue.popleft()
        return None


@pytest.fixture
def kv() -> FakeKV:
    return FakeKV()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker() -> MemoryTracker:
    return MemoryTracker()
