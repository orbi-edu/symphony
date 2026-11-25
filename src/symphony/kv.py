"""RedisKV — the production backend for the `state.KV` protocol.

Replaces the Modal port's `modal.Dict`. The mapping is exact, which is the point:

- `put(skip_if_exists=True)`  →  `SET key value NX`  (atomic put-if-absent — the ONE
  conditional primitive the whole fencing design is built on, §3.7 / constitution IV);
- 7-day idle expiry            →  `EX` on every write plus a best-effort `EXPIRE` on
  every read, so an entry the reconciliation reads each tick never expires while
  tracked, and stale `toolcall:*`/`dispatch:*` residue ages out on its own;
- values                       →  JSON documents (the state schema is already
  JSON-shaped dicts and scalars).

Runner writes and daemon leases use atomic compare-and-set operations in production.
The pure core retains a sequential fallback for injected in-memory stores.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import rollups

DEFAULT_TTL_S = 7 * 24 * 3600  # modal.Dict's 7-day idle expiry, kept for parity
# Keys that must never expire, so reads never put a TTL on them: dispatch generations (an
# expired tombstone would let an old Job address a reopened issue), the lease (it carries
# its own expiry) and human holds (internals §12: held until explicitly released).
NO_TTL_PREFIXES = ("generation:", "lock:", "hold:")


class RedisKV:
    def __init__(self, client=None, url: str | None = None, ttl_s: int = DEFAULT_TTL_S) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(
                url or os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_timeout=10.0,
                socket_connect_timeout=5.0,
            )
        self._r = client
        self._ttl_s = ttl_s

    @property
    def client(self):
        return self._r

    @property
    def ttl_s(self) -> int:
        return self._ttl_s


    def next_generation(self, key: str) -> int:
        # No TTL: an expired tombstone would let an old Job address a reopened issue.
        return int(self._r.incr(key))

    def remember_generation(self, key: str, seq: int) -> None:
        self._r.eval("""
            local current = tonumber(redis.call('GET', KEYS[1]) or '0')
            if tonumber(ARGV[1]) > current then redis.call('SET', KEYS[1], ARGV[1]) end
            return 1
        """, 1, key, seq)
    def renew_lease(self, key: str, owner: str, ttl_ms: int, *, require_live: bool = False) -> bool:
        return bool(self._r.eval("""
            local t = redis.call('TIME')
            local now = t[1] * 1000 + math.floor(t[2] / 1000)
            local raw = redis.call('GET', KEYS[1])
            if raw then
                local lease = cjson.decode(raw)
                if ARGV[3] == '1' and (lease.owner ~= ARGV[1] or lease.lease_expires <= now) then
                    return 0
                end
                if lease.owner ~= ARGV[1] and lease.lease_expires > now then return 0 end
            elseif ARGV[3] == '1' then
                return 0
            end
            redis.call('SET', KEYS[1], cjson.encode({
                owner=ARGV[1], lease_expires=now + tonumber(ARGV[2])
            }), 'PX', ARGV[2])
            return 1
        """, 1, key, owner, ttl_ms, int(require_live)))

    def release_lease(self, key: str, owner: str) -> bool:
        """Deletes the lease only while `owner` still holds it (compare-and-delete)."""
        return bool(self._r.eval("""
            local raw = redis.call('GET', KEYS[1])
            if not raw then return 0 end
            if cjson.decode(raw).owner ~= ARGV[1] then return 0 end
            redis.call('DEL', KEYS[1])
            return 1
        """, 1, key, owner))

    def fenced_put(self, key: str, value: Any, expected: dict, token_key: str) -> bool:
        return bool(self._r.eval("""
            local raw = redis.call('GET', KEYS[1])
            if not raw or redis.call('EXISTS', KEYS[2]) == 0 then return 0 end
            local current = cjson.decode(raw)
            local expected = cjson.decode(ARGV[2])
            for k, v in pairs(expected) do
                if current[k] ~= v then return 0 end
            end
            redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
            return 1
        """, 2, key, token_key, json.dumps(value), json.dumps(expected), self._ttl_s))

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._r.get(key)
        if raw is None:
            return default
        if not key.startswith(NO_TTL_PREFIXES):
            try:
                # reads renew the idle TTL, mirroring the Dict's read-or-write expiry clock
                self._r.expire(key, self._ttl_s)
            except Exception:  # noqa: BLE001 — TTL refresh is hygiene, never worth a failed read
                pass
        return json.loads(raw)

    def put(self, key: str, value: Any, *, skip_if_exists: bool = False) -> bool:
        raw = json.dumps(value)
        if skip_if_exists:
            return bool(self._r.set(key, raw, nx=True, ex=self._ttl_s))
        self._r.set(key, raw, ex=self._ttl_s)
        return True

    def delete(self, key: str) -> None:
        self._r.delete(key)

    def keys(self) -> list[str]:
        # SCAN, not KEYS: the state space is small (one entry per tracked issue plus
        # tokens), but the daemon shares this Redis with nothing that deserves a stall.
        return list(self._r.scan_iter(count=500))

    def bump_rollup(self, at_ms: int, fields, *, once: str | None = None) -> bool:
        """A daily rollup bump (`rollups.bump`): HINCRBY in one script, never expired."""
        return rollups.bump(self._r, at_ms, fields, once=once)


class LeaseKV:
    """One daemon tick's optimistic writes, fenced by its authoritative Redis lease.

    A runner can finish while the daemon is reading the tracker. Comparing the exact
    read snapshot prevents that stale tick from resurrecting the old generation.
    Conflicts abort the tick; the next tick reconciles fresh state.
    """

    def __init__(self, kv: RedisKV, owner: str) -> None:
        self.base, self.owner = kv, owner
        self._read: dict[str, str | bytes | None] = {}

    def get(self, key: str, default=None):
        raw = self.base.client.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode()
        self._read[key] = raw
        if raw is None:
            return default
        if not key.startswith(NO_TTL_PREFIXES):
            self.base.client.expire(key, self.base.ttl_s)
        return json.loads(raw)

    def put(self, key: str, value, *, skip_if_exists=False):
        return self._mutate(key, json.dumps(value), skip_if_exists=skip_if_exists)

    def delete(self, key: str):
        self._mutate(key, None)

    def _mutate(self, key, raw, *, skip_if_exists=False):
        from .state import LOCK_KEY

        with self.base.client.pipeline() as pipe:
            pipe.watch(LOCK_KEY, key)
            lease_raw = pipe.get(LOCK_KEY)
            lease = json.loads(lease_raw) if lease_raw else None
            seconds, micros = pipe.time()
            now = seconds * 1000 + micros // 1000
            if not lease or lease.get("owner") != self.owner or lease.get("lease_expires", 0) <= now:
                raise RuntimeError("Daemon lease lost")
            current = pipe.get(key)
            if isinstance(current, bytes):
                current = current.decode()
            if skip_if_exists and current is not None:
                return False
            if key in self._read and current != self._read[key]:
                raise RuntimeError("Lifecycle changed during reconciliation")
            pipe.multi()
            if raw is None:
                pipe.delete(key)
            else:
                pipe.set(key, raw, ex=self.base.ttl_s)
            pipe.execute()
            self._read[key] = raw
            return True

    def keys(self):
        return self.base.keys()

    def next_generation(self, key):
        return self.base.next_generation(key)

    def remember_generation(self, key, seq):
        self.base.remember_generation(key, seq)

    def bump_rollup(self, at_ms, fields, *, once=None):
        # Counters, not lifecycle: the once-id, not the lease, keeps a stale tick from
        # counting an event twice.
        return self.base.bump_rollup(at_ms, fields, once=once)
