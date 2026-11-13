"""Forward-only consumption from Oh My Pi's final usage-bearing messages."""

import json

import pytest

from symphony.usage import (
    UsageLedger,
    limit_from_retry,
    limit_from_usage_state,
    ratelimits_key,
    record_rate_limit,
    usage_delta,
)


def _assistant(inp, out, cache_read=0, cache_write=0, cost=0.0):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "totalTokens": inp + out + cache_read + cache_write,
                "cost": {"total": cost},
            },
        },
    }


def test_usage_delta_normalizes_engine_shape():
    d = usage_delta(_assistant(10, 5, cache_read=2, cache_write=1, cost=0.25))
    assert d == {
        "input": 10,
        "output": 5,
        "cache_read": 2,
        "cache_write": 1,
        "total": 18,
        "cost_estimate": 0.25,
    }


def test_message_update_is_ignored():
    """Streaming usage is partial; accumulating it would over-count."""
    assert (
        usage_delta(
            {"type": "message_update", "message": {"role": "assistant", "usage": {"input": 999}}}
        )
        is None
    )


def test_events_without_usage_are_ignored():
    assert usage_delta({"type": "agent_start"}) is None
    assert usage_delta({"type": "message_end", "message": {"role": "user"}}) is None


def test_task_usage_counts_once_from_details_not_a_repeated_snapshot():
    event = {
        "type": "message_end",
        "message": {
            "role": "toolResult",
            "toolName": "task",
            "details": {
                "usage": {"input": 3, "output": 1, "totalTokens": 4, "cost": {"total": 0.01}}
            },
        },
    }
    ledger = UsageLedger()
    ledger.observe(event)
    ledger.observe({"type": "agent_end", "isTerminal": True, "messages": [event["message"]]})
    assert ledger.totals["input"] == 3
    assert ledger.totals["cost_estimate"] == 0.01


def test_unrelated_or_invalid_tool_details_are_not_billable_usage():
    assert (
        usage_delta(
            {
                "type": "message_end",
                "message": {
                    "role": "toolResult",
                    "toolName": "read",
                    "details": {"usage": {"input": 99}},
                },
            }
        )
        is None
    )
    assert (
        usage_delta(
            {
                "type": "message_end",
                "message": {
                    "role": "toolResult",
                    "toolName": "task",
                    "details": "task failed",
                },
            }
        )
        is None
    )


def test_ledger_accumulates_forward_only():
    led = UsageLedger()
    led.observe(_assistant(10, 5, cost=0.1))
    led.observe(_assistant(20, 8, cost=0.2))
    assert led.totals["input"] == 30
    assert led.totals["output"] == 13
    assert round(led.totals["cost_estimate"], 4) == 0.3


def test_drain_returns_and_clears_pending():
    """The runner flushes pending usage to shared state on a throttle."""
    led = UsageLedger()
    led.observe(_assistant(10, 5, cost=0.1))
    pending = led.drain()
    assert pending["input"] == 10
    assert led.drain()["input"] == 0  # drained
    assert led.totals["input"] == 10  # totals are cumulative, unaffected by drain


def test_reconcile_reports_drift_against_the_engine_snapshot():
    """Session context can include resumed history or exclude compacted messages;
    discrepancies are reported without changing billed attempt consumption."""
    led = UsageLedger()
    led.observe(_assistant(10, 5, cost=0.1))
    drift = led.reconcile({"tokens": {"input": 10, "output": 5}})
    assert drift == {}
    drift = led.reconcile({"tokens": {"input": 999, "output": 5}})
    assert "input" in drift
    assert led.totals["input"] == 10


# -- provider rate limits (internals §10) -------------------------------------------
def test_a_low_priority_usage_limit_becomes_a_record():
    doc = limit_from_usage_state(
        {"stage": "low_priority", "resetsAtSec": 1_800_000_000, "allowanceLeftPercent": 12}, 5
    )
    assert doc == {"stage": "low_priority", "reset_at": 1_800_000_000_000, "remaining": 12,
                   "limit": 100, "observed_at": 5, "source": "usage_limit"}


def test_a_wrap_up_without_reset_and_no_limit_at_all():
    assert limit_from_usage_state({"stage": "wrap_up", "extraUsage": False}, 7) == {
        "stage": "wrap_up", "observed_at": 7, "source": "usage_limit"
    }
    assert limit_from_usage_state(None, 7) is None


@pytest.mark.parametrize(
    "message",
    [
        "429 Too Many Requests",
        "ThrottlingException: Too many tokens, please wait before trying again.",
        "rate_limit_error: This request would exceed your rate limit",
        "ServiceQuotaExceededException",
    ],
)
def test_retries_caused_by_throttling_are_recorded(message):
    event = {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 4000,
             "errorMessage": message}
    assert limit_from_retry(event, 9) == {
        "stage": "throttled", "retry_after_ms": 4000, "observed_at": 9, "source": "retry"
    }


def test_other_retries_and_events_are_not_rate_limits():
    assert limit_from_retry({"type": "auto_retry_start", "errorMessage": "socket hang up"}, 1) is None
    assert limit_from_retry({"type": "auto_retry_end", "success": True}, 1) is None


def test_records_land_in_the_contract_hash(redis_client):
    record_rate_limit(redis_client, "ns", "amazon-bedrock", "m-1", {"stage": "throttled"})
    assert json.loads(redis_client.hget(ratelimits_key("ns"), "amazon-bedrock/m-1")) == {
        "stage": "throttled"
    }
    assert ratelimits_key("ns") == "sym:ns:ratelimits"
