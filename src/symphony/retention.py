"""How long ended sessions and audit entries stay in Redis (WORKFLOW `retention:`).

The lease holder publishes the WORKFLOW values to `sym:<ns>:retention` on each tick
(`admin_commands.process_commands`). Every writer reads them from there, so the runner, the
merger, the assistant and the API apply the same values without reading the WORKFLOW. An
absent or unreadable record means the defaults.
"""

from __future__ import annotations

import json

SESSIONS_DAYS = 7
AUDIT_DAYS = 30
# The archive CronJob runs once a day. Two days is the shortest window in which it always
# sees an entry before Redis drops it.
MIN_DAYS = 2
MAX_SESSIONS_DAYS = 365
MAX_AUDIT_DAYS = 3650
DAY_S = 24 * 3600


def key(namespace: str) -> str:
    return f"sym:{namespace}:retention"


def publish(client, namespace: str, sessions_days: int, audit_days: int) -> None:
    client.set(
        key(namespace),
        json.dumps({"sessions_days": sessions_days, "audit_days": audit_days}, sort_keys=True),
    )


def read(client, namespace: str) -> dict:
    """`{sessions_days, audit_days}`: the published values, else the defaults."""
    out = {"sessions_days": SESSIONS_DAYS, "audit_days": AUDIT_DAYS}
    try:
        doc = json.loads(client.get(key(namespace)) or "{}")
    except ValueError:
        return out
    bounds = {"sessions_days": MAX_SESSIONS_DAYS, "audit_days": MAX_AUDIT_DAYS}
    for name, high in bounds.items():
        value = doc.get(name) if isinstance(doc, dict) else None
        if type(value) is int and MIN_DAYS <= value <= high:
            out[name] = value
    return out


def sessions_s(client, namespace: str) -> int:
    return read(client, namespace)["sessions_days"] * DAY_S


def audit_ms(client, namespace: str) -> int:
    return read(client, namespace)["audit_days"] * DAY_S * 1000
