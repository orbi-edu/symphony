"""Forward-only consumption from Oh My Pi's live RPC message events.

Assistant messages carry final usage; task tool results carry their nested usage
in details. Streaming updates and terminal agent snapshots repeat those messages
and must not be counted again. Resumed history is not an event backfill.

`get_session_stats` describes the active session context, which may lose older
messages on compaction. It is only a reconciliation source, never a billing delta.
Auxiliary model calls without a usage-bearing RPC message are not represented.
"""

from __future__ import annotations

import json
import re

from . import metrics, state

# Costs are computed by the engine from a bundled price table, not reported by the
# provider's billing. Every surface that shows this must label it an estimate (FR-031).
COST_IS_ESTIMATE = True

_ZERO: dict = dict.fromkeys(state.TOKEN_FIELDS, 0)


def _normalize(raw: dict) -> dict:
    return {
        "input": raw.get("input", 0),
        "output": raw.get("output", 0),
        "cache_read": raw.get("cacheRead", 0),
        "cache_write": raw.get("cacheWrite", 0),
        "total": raw.get("totalTokens", 0),
        "cost_estimate": (raw.get("cost") or {}).get("total", 0.0),
    }


def usage_delta(msg: dict) -> dict | None:
    """Extract the normalized consumption delta from one engine message, or None.

    Only final assistant and task tool messages carry billable usage in OMP.
    Streaming usage is partial, and agent_end repeats already streamed messages.
    """
    if msg.get("type") != "message_end":
        return None
    message = msg.get("message") or {}
    if message.get("role") == "assistant":
        raw = message.get("usage")
    elif message.get("role") == "toolResult" and message.get("toolName") == "task":
        details = message.get("details")
        raw = details.get("usage") if isinstance(details, dict) else None
    else:
        return None
    return _normalize(raw) if isinstance(raw, dict) else None


class UsageLedger:
    """Forward-only accumulator for one attempt.

    `totals` is everything seen since construction. `drain()` returns what has not yet
    been flushed to shared state and resets that buffer — the runner flushes on its
    heartbeat throttle, exactly as before.
    """

    def __init__(self) -> None:
        self.totals: dict = dict(_ZERO)
        self._pending: dict = dict(_ZERO)

    def observe(self, msg: dict) -> dict | None:
        delta = usage_delta(msg)
        if delta is None:
            return None
        self.totals = state.add_tokens(self.totals, delta)
        self._pending = state.add_tokens(self._pending, delta)
        return delta

    def drain(self) -> dict:
        pending, self._pending = self._pending, dict(_ZERO)
        return pending

    def reconcile(self, session_stats: dict) -> dict:
        """Compare our forward-only totals against the active session snapshot.

        Drift is expected after resume or compaction: the snapshot's context is
        not this attempt's consumption. Report it without correcting the ledger.
        """
        reported = session_stats.get("tokens") or {}
        drift: dict = {}
        for field in ("input", "output"):
            if field in reported and reported[field] != self.totals.get(field, 0):
                drift[field] = {"ledger": self.totals.get(field, 0), "engine": reported[field]}
        return drift


# --------------------------------------------------------------------------
# provider rate limits (internals §10)
# --------------------------------------------------------------------------
# omp's retry notice carries the provider's error text; these are how 429s and Bedrock
# throttling read across providers.
_THROTTLED = re.compile(r"\b429\b|throttl|rate[ _-]?limit|too many requests|quota", re.IGNORECASE)


def ratelimits_key(namespace: str) -> str:
    return f"sym:{namespace}:ratelimits"


def limit_from_usage_state(usage_limit: dict | None, now_ms: int) -> dict | None:
    """`get_state().usageLimit` → a §10 record. Absent outside wrap-up/low priority."""
    if not isinstance(usage_limit, dict) or not usage_limit.get("stage"):
        return None
    doc: dict = {"stage": usage_limit["stage"], "observed_at": now_ms, "source": "usage_limit"}
    if isinstance(usage_limit.get("resetsAtSec"), (int, float)):
        doc["reset_at"] = int(usage_limit["resetsAtSec"] * 1000)
    if isinstance(usage_limit.get("allowanceLeftPercent"), (int, float)):
        doc["remaining"] = usage_limit["allowanceLeftPercent"]
        doc["limit"] = 100
    return doc


def limit_from_retry(event: dict, now_ms: int) -> dict | None:
    """An `auto_retry_start` caused by 429/throttling → a §10 record; other retries → None."""
    if event.get("type") != "auto_retry_start":
        return None
    if not _THROTTLED.search(str(event.get("errorMessage") or "")):
        return None
    doc: dict = {"stage": "throttled", "observed_at": now_ms, "source": "retry"}
    if isinstance(event.get("delayMs"), (int, float)):
        doc["retry_after_ms"] = int(event["delayMs"])
    return doc


def record_rate_limit(client, namespace: str, provider: str, model: str, doc: dict) -> None:
    """Write the latest observation for `<provider>/<model>` (field of the §10 hash) and count
    it (`symphony_rate_limit_events_total`)."""
    client.hset(ratelimits_key(namespace), f"{provider}/{model}", json.dumps(doc))
    metrics.count_rate_limit(client, namespace, provider, doc.get("stage"))
