"""Session telemetry: the OTLP fold, the review report, and the park-once guard.

Pure: dictionaries in, dictionaries out. No platform, no network.
"""

import json

import pytest

from symphony import state, telemetry
from symphony.telemetry import fold_export, new_rollup, render_session_report

# -- OTLP/JSON payload helpers ---------------------------------------------
# Mirrors OMP's GenAI spans after decoding, INCLUDING the two encoding rules a hand-written
# reader gets wrong by default: 64-bit ints travel as strings, and attribute values are
# tagged unions.


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}  # NOTE: string
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def span(operation, start_ns=0, end_ns=0, attrs=None, status_code=0, parent=""):
    attrs = {"gen_ai.operation.name": operation, **(attrs or {})}
    return {
        "name": operation,
        "parentSpanId": parent,
        "startTimeUnixNano": str(start_ns),  # NOTE: string
        "endTimeUnixNano": str(end_ns),
        "attributes": [attr(k, v) for k, v in attrs.items()],
        "status": {"code": status_code},
    }


def tool(name, start_ns=0, end_ns=0, status_code=0, **attrs):
    return span("execute_tool", start_ns, end_ns, {"gen_ai.tool.name": name, **attrs}, status_code)


def payload(spans, issue_id="i1", seq=1):
    resource_attrs = []
    if issue_id is not None:
        resource_attrs.append(attr("symphony.issue_id", issue_id))
    if seq is not None:
        resource_attrs.append(attr("symphony.dispatch_seq", seq))
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


MS = 1_000_000  # ns per ms


# -- the fold ---------------------------------------------------------------


def test_fold_counts_each_span_kind():
    roll = fold_export(None, payload([
        span("invoke_agent", 0, 10 * MS, {"omp.gen_ai.agent.step.count": 2}),
        span("chat", 0, 3 * MS, {"gen_ai.request.model": "claude-opus-5"}),
        span("judgment", 0, 1 * MS, {"gen_ai.request.model": "claude-haiku-5"}),
        tool("bash", 0, 2 * MS),
    ]))
    assert roll["interactions"] == 1
    assert roll["turns"] == 2
    assert roll["llm_requests"] == 2
    assert roll["tool_calls"] == 1
    assert roll["spans"] == 4
    assert roll["llm_ms"] == 4
    assert roll["tool_ms"] == 2
    assert roll["by_tool"]["bash"] == {"calls": 1, "ms": 2, "errors": 0, "max_ms": 2}
    assert roll["by_model"]["claude-opus-5"]["requests"] == 1


def test_a_subagent_loop_is_not_another_prompt_but_its_work_counts():
    """Subagents run their own `invoke_agent` under the parent's tool span. Counting it as
    an interaction would inflate prompts and turns; dropping its spans would hide its cost."""
    roll = fold_export(None, payload([
        span("invoke_agent", 0, 10 * MS, {"omp.gen_ai.agent.step.count": 3}),
        span("invoke_agent", 0, 5 * MS, {"omp.gen_ai.agent.step.count": 4}, parent="cGFyZW50"),
        span("chat", 0, MS, {"gen_ai.request.model": "m"}),
    ]))
    assert roll["interactions"] == 1
    assert roll["turns"] == 3
    assert roll["llm_requests"] == 1


def test_fold_accumulates_across_batches():
    roll = None
    for _ in range(3):
        roll = fold_export(roll, payload([tool("bash", 0, 2 * MS)]))
    assert roll["tool_calls"] == 3
    assert roll["by_tool"]["bash"]["calls"] == 3
    assert roll["batches"] == 3


def test_integers_encoded_as_strings_are_read():
    """OTLP/JSON encodes 64-bit fields as strings. Read naively they become 0, and every
    duration in the report silently collapses to zero — a number that looks plausible."""
    roll = fold_export(None, payload([
        span("chat", 1_000 * MS, 4_000 * MS,
             {"gen_ai.usage.input_tokens": 1234, "gen_ai.request.model": "m"}),
    ]))
    assert roll["llm_ms"] == 3_000
    assert roll["by_model"]["m"]["input"] == 1234


def test_integers_encoded_as_numbers_are_also_read():
    """Not every producer stringifies. Both encodings must work."""
    raw = payload([span("chat", 0, 0, {"gen_ai.request.model": "m"})])
    sp = raw["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    sp["startTimeUnixNano"] = 1_000 * MS
    sp["endTimeUnixNano"] = 3_000 * MS
    assert fold_export(None, raw)["llm_ms"] == 2_000


def test_error_spans_are_counted():
    roll = fold_export(None, payload([
        tool("bash", 0, MS, status_code=2),
        tool("bash", 0, MS, status_code=2, **{"omp.gen_ai.tool.status": "timeout"}),
        tool("bash", 0, MS),
    ]))
    assert roll["errors"] == 2
    assert roll["by_tool"]["bash"]["errors"] == 2
    assert roll["by_tool"]["bash"]["calls"] == 3


def test_a_span_that_ends_before_it_starts_does_not_subtract():
    """Clock adjustment mid-span. A negative duration would silently reduce the totals,
    and a large enough one would make a slow run look fast."""
    roll = fold_export(None, payload([span("chat", 9_000 * MS, 1_000 * MS, {})]))
    assert roll["llm_ms"] == 0
    assert telemetry.wall_ms(roll) >= 0


def test_the_rollup_stays_bounded_as_spans_multiply():
    """SC-004. The rollup lives in shared state; a four-hour session must not be able to
    grow it. Compared by serialized size, which is what actually gets stored."""
    small = fold_export(None, payload([tool(f"t{i % 4}", 0, MS) for i in range(100)]))
    big = small
    for _ in range(20):
        big = fold_export(big, payload([tool(f"t{i % 4}", 0, MS) for i in range(1_000)]))
    assert big["tool_calls"] == 20_100
    assert len(json.dumps(big)) <= len(json.dumps(small)) * 2


def test_unbounded_tool_names_collapse_into_one_bucket():
    """A misbehaving extension registering a name per call must not grow state without
    bound. Correctness of the report degrades; the size does not."""
    roll = None
    for i in range(500):
        roll = fold_export(roll, payload([tool(f"x{i}", 0, MS)]))
    assert len(roll["by_tool"]) <= telemetry.MAX_TOOLS + 1
    assert roll["by_tool"][telemetry.OTHER]["calls"] > 0
    assert sum(t["calls"] for t in roll["by_tool"].values()) == 500


def test_no_content_reaches_the_rollup_even_at_full_capture():
    """SC-005 / INV-4. Raising capture for a debugging session must not put prompt text
    into orchestrator state — and from there into a ticket comment. Enforced by the rollup
    having nowhere to put it, not by a filter."""
    secret = "SUPER-SECRET-PROMPT-TEXT"
    roll = fold_export(None, payload([
        tool("bash", 0, MS, **{
            "gen_ai.tool.call.arguments": secret,
            "gen_ai.tool.call.result": secret,
        }),
        span("chat", 0, MS, {
            "gen_ai.request.model": "m",
            "gen_ai.system_instructions": secret,
            "gen_ai.input.messages": secret,
            "gen_ai.output.messages": secret,
        }),
    ]))
    assert secret not in json.dumps(roll)


def test_malformed_payloads_are_dropped_not_raised():
    """A batch we cannot read is a batch we drop. Raising on a path the agent cannot see
    would turn an observability glitch into a 500 on a hot endpoint."""
    for bad in ({}, {"resourceSpans": None}, {"resourceSpans": [None]},
                {"resourceSpans": [{"scopeSpans": [{"spans": [None, 7, "x"]}]}]},
                {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": None}]}]}]}):
        assert fold_export(None, bad)["spans"] == 0


# -- attempt attribution ----------------------------------------------------


def test_attempt_attributes_are_read_from_the_resource():
    assert telemetry.attempt_attrs(payload([], issue_id="abc", seq=7)) == ("abc", 7)


def test_a_payload_without_attribution_is_refused_not_guessed():
    """Guessing would file one attempt's spans under another attempt's rollup."""
    assert telemetry.attempt_attrs(payload([], issue_id=None)) == (None, None)
    assert telemetry.attempt_attrs({}) == (None, None)


# -- has_recorded_run -------------------------------------------------------


def test_a_claim_that_never_ran_has_nothing_to_report():
    assert not telemetry.has_recorded_run({"tokens": {"total": 0}})
    assert not telemetry.has_recorded_run({})


@pytest.mark.parametrize("entry", [
    {"sandbox_name": "sb-1"},
    {"session_id": "s1"},
    {"tokens": {"total": 10}},
    {"tokens": {"input": 5, "output": 0, "total": 0}},
])
def test_evidence_of_a_run_is_enough_to_report(entry):
    assert telemetry.has_recorded_run(entry)


# -- the report -------------------------------------------------------------


def entry_with(**over):
    base = {
        "runs": 2,
        "claimed_at": 1_000_000,
        "updated_at": 1_000_000 + 90_000,
        "sandbox_name": "sb-1",
        "tokens": {"input": 120_000, "output": 8_000, "cache_read": 4_000,
                   "cache_write": 0, "total": 132_000, "cost_estimate": 1.2345},
    }
    base.update(over)
    return base


def test_the_report_carries_runs_tokens_and_cost():
    text = render_session_report(entry_with())
    assert "**2**" in text                 # runs
    assert "132 000" in text               # total tokens, grouped
    assert "1.2345" in text                # cost
    assert "1m 30s" in text                # elapsed


def test_the_cost_is_always_labelled_an_estimate():
    """FR-005. The number comes from a bundled price table, not from billing. Presented
    bare, it reads as an invoice — and it is not one."""
    text = render_session_report(entry_with())
    assert "estimate" in text.lower()
    assert "not a billed charge" in text


def test_a_run_with_no_tokens_says_so_rather_than_reporting_zero():
    """An attempt that died before its first model request. `0 tokens` reads as a free
    successful run; it was neither."""
    text = render_session_report(entry_with(tokens={"total": 0, "cost_estimate": 0.0}))
    assert "none recorded" in text
    assert "no model request completed" in text


def test_the_report_works_with_no_rollup_at_all():
    """US1 must ship without US2: telemetry off, unreachable, or pointed at the operator's
    own collector all land here."""
    text = render_session_report(entry_with(), None)
    assert "132 000" in text
    assert "Session trace" not in text


def test_the_trace_section_appears_when_there_is_one():
    roll = fold_export(None, payload([
        span("chat", 0, 3_000 * MS, {"gen_ai.request.model": "m"}),
        tool("bash", 0, 61_000 * MS),
        tool("read", 0, 500 * MS),
    ]))
    text = render_session_report(entry_with(), roll)
    assert "Session trace" in text
    assert "`bash`" in text
    assert "1m 01s" in text  # the slow tool is legible as time, not as milliseconds


def test_an_empty_rollup_reports_absence_rather_than_zeros():
    text = render_session_report(entry_with(), new_rollup())
    assert "No trace data" in text


def test_state_keys_for_telemetry_are_namespaced():
    assert state.telemetry_key("i1").startswith(state.TELEMETRY_PREFIX)
    assert state.telemetry_key("i1") != state.issue_key("i1")
