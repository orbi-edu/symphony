"""Wake-ups: webhooks and humans make the daemon and the merger act now, not at the next poll.

A wake is an entry on a Redis list. Waiting is a blocking pop with the poll interval as
timeout, so a missed or absent webhook only costs latency, never correctness: the poll still
runs. Lists rather than pub/sub so a wake that arrives while the loop is busy is not lost.
"""

from __future__ import annotations

import time

DAEMON = "daemon"
MERGER = "merger"
MAX_PENDING = 100
# A blocking pop must return before the client's own socket timeout: otherwise the client
# raises mid-wait, and a wake the server delivers to that abandoned pop is lost.
_SOCKET_MARGIN_S = 1.0


def _key(namespace: str, target: str) -> str:
    return f"sym:{namespace}:wake:{target}"


def wake(client, namespace: str, target: str, reason: str) -> None:
    key = _key(namespace, target)
    pipe = client.pipeline()
    pipe.lpush(key, reason)
    pipe.ltrim(key, 0, MAX_PENDING - 1)
    pipe.execute()


def _max_block_s(client) -> float | None:
    kwargs = getattr(getattr(client, "connection_pool", None), "connection_kwargs", {}) or {}
    socket_timeout = kwargs.get("socket_timeout")
    if not socket_timeout:
        return None
    return max(float(socket_timeout) - _SOCKET_MARGIN_S, 0.5)


def wait(client, namespace: str, target: str, timeout_s: float) -> list[str]:
    """Block until a wake arrives or the timeout passes; returns every pending reason.

    The wait is split into pops shorter than the client's socket timeout, so any Redis client
    configuration waits the full interval without errors and without losing a wake."""
    key = _key(namespace, target)
    cap = _max_block_s(client)
    deadline = time.monotonic() + max(timeout_s, 0.01)
    first = None
    while first is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        block = remaining if cap is None else min(remaining, cap)
        # BRPOP takes whole or fractional seconds; never 0, which would block forever.
        first = client.brpop(key, timeout=max(block, 0.01))
    reasons = [first[1]]
    while (more := client.rpop(key)) is not None:
        reasons.append(more)
    return reasons
