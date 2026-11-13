"""A stopping daemon hands its lease back, so a rollout or a setup restart does not leave the
next daemon waiting out the lease TTL (found on kind: about 60 s of a 92 s dispatch)."""

from __future__ import annotations

from symphony import state
from symphony.kv import RedisKV


def test_only_the_holder_releases_the_lease(redis_client):
    kv = RedisKV(redis_client)
    assert state.renew_lease(kv, "daemon-a", 1_000)
    assert not state.release_lease(kv, "daemon-b")  # someone else's lease stays
    assert kv.get(state.LOCK_KEY)["owner"] == "daemon-a"
    assert state.release_lease(kv, "daemon-a")
    assert kv.get(state.LOCK_KEY) is None
    assert not state.release_lease(kv, "daemon-a")  # nothing left to release


def test_a_released_lease_is_taken_at_once_by_the_next_daemon(redis_client):
    kv = RedisKV(redis_client)
    assert state.renew_lease(kv, "old", 60_000)
    assert not state.renew_lease(kv, "new", 60_000)  # without a release: wait out the TTL
    state.release_lease(kv, "old")
    assert state.renew_lease(kv, "new", 60_000)


def test_the_sequential_fallback_matches(redis_client):
    """Injected stores without the atomic method get the same rule."""

    class Plain:
        def __init__(self):
            self.data = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def put(self, key, value, **_):
            self.data[key] = value
            return True

        def delete(self, key):
            self.data.pop(key, None)

    kv = Plain()
    state.renew_lease(kv, "a", 1_000_000_000_000_000, 1_000)
    assert not state.release_lease(kv, "b") and state.release_lease(kv, "a")
    assert kv.get(state.LOCK_KEY) is None
