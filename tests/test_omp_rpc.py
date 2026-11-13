"""Oh My Pi protocol framing, command correlation, and run outcomes."""

import pytest
from tests.conftest import FakeLineReader, FakeLineWriter

from symphony.omp_rpc import (
    CommandFailed,
    OmpClient,
    ReadTimeout,
    RunOutcome,
    RunWatcher,
    SilenceTimeout,
    split_frames,
)


# -- framing (T013) --------------------------------------------------------
def test_split_frames_splits_on_lf_only():
    frames, rest = split_frames('{"a":1}\n{"b":2}\n{"c":')
    assert frames == ['{"a":1}', '{"b":2}']
    assert rest == '{"c":'


def test_split_frames_does_not_split_on_unicode_separators():
    """The engine's spec is explicit that U+2028/U+2029 are ordinary payload characters.
    Node's `readline` gets this wrong; we must not."""
    payload = '{"text":"line still same frame still same"}'
    frames, rest = split_frames(payload + "\n")
    assert frames == [payload]
    assert rest == ""


def test_split_frames_strips_trailing_cr():
    frames, _ = split_frames('{"a":1}\r\n')
    assert frames == ['{"a":1}']


def test_split_frames_ignores_blank_lines():
    frames, _ = split_frames('\n\n{"a":1}\n\n')
    assert frames == ['{"a":1}']


# -- envelope (T013) -------------------------------------------------------
def test_command_correlates_response_by_id_and_returns_data():
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    r.push({"type": "agent_start"})  # an event arriving first must not be lost
    r.push(
        {
            "id": "c1",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionFile": "/s.jsonl"},
        }
    )
    assert client.command("get_state", timeout_s=1)["sessionFile"] == "/s.jsonl"
    sent = w.sent()[0]
    assert sent["type"] == "get_state" and "id" in sent
    assert "jsonrpc" not in sent  # NOT JSON-RPC 2.0


def test_events_queued_during_a_command_are_replayed_not_dropped():
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    r.push({"type": "agent_start"})
    r.push({"id": "c1", "type": "response", "command": "get_state", "success": True, "data": {}})
    client.command("get_state", timeout_s=1)
    assert next(iter(client.events(1)))["type"] == "agent_start"


def test_failed_command_raises_with_the_engine_message():
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    r.push(
        {
            "id": "c1",
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "No API key found for acme.",
        }
    )
    with pytest.raises(CommandFailed) as exc:
        client.command("prompt", {"message": "hi"}, timeout_s=1)
    assert "No API key" in str(exc.value)


def test_parse_failure_response_without_id_is_tolerated():
    """A malformed input line yields a failure response carrying NO id. The client must
    not crash trying to correlate it."""
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    r.push({"type": "response", "command": "parse", "success": False, "error": "bad json"})
    r.push({"id": "c1", "type": "response", "command": "get_state", "success": True, "data": {}})
    assert client.command("get_state", timeout_s=1) == {}


def test_silence_raises_distinct_timeouts():
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    with pytest.raises(ReadTimeout):
        client.command("get_state", timeout_s=1)
    with pytest.raises(SilenceTimeout):
        list(client.events(1))


# -- protocol v2, host tools, concurrency ------------------------------------
def _chunks(frame: dict, size: int) -> list[dict]:
    import base64
    import json

    raw = json.dumps(frame).encode()
    parts = [raw[i : i + size] for i in range(0, len(raw), size)]
    return [
        {"type": "rpc_chunk", "chunkId": "rpc-1", "index": i, "count": len(parts),
         "byteLength": len(raw), "data": base64.b64encode(p).decode()}
        for i, p in enumerate(parts)
    ]


def test_chunked_frames_are_reassembled_into_one_message():
    """Protocol v2 splits big frames (a long get_entries) instead of eliding them."""
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    big = {"id": "c1", "type": "response", "command": "get_entries", "success": True,
           "data": {"entries": [{"id": "e1", "text": "x" * 500}]}}
    for chunk in _chunks(big, 64):
        r.push(chunk)
    data = client.command("get_entries", timeout_s=1)
    assert data["entries"][0]["text"] == "x" * 500


def test_an_interrupted_chunk_sequence_is_a_protocol_error():
    from symphony.omp_rpc import OmpError

    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    first, second = _chunks({"type": "agent_start", "pad": "y" * 200}, 100)[:2]
    r.push(first)
    r.push({**second, "chunkId": "rpc-other"})
    with pytest.raises(OmpError, match="mismatch"):
        next(iter(client.events(1)))


def test_negotiation_falls_back_to_v1_when_the_engine_refuses():
    w, r = FakeLineWriter(), FakeLineReader()
    client = OmpClient(w, r)
    r.push({"id": "c1", "type": "response", "command": "negotiate_protocol", "success": False,
            "error": "Unsupported RPC protocol version: 2"})
    assert client.negotiate(1) == 1
    assert w.sent()[0] == {"id": "c1", "type": "negotiate_protocol", "protocolVersion": 2}


def test_host_tool_result_matches_the_engine_wire_shape():
    w, r = FakeLineWriter(), FakeLineReader()
    OmpClient(w, r).host_tool_result("h1", "ok", is_error=False)
    assert w.sent() == [{"type": "host_tool_result", "id": "h1",
                         "result": {"content": [{"type": "text", "text": "ok"}]}, "isError": False}]


def test_concurrent_writers_never_overlap_or_reuse_ids():
    """The intent pump and host-tool workers write while the turn loop owns the reader;
    a transport write split by another thread's would corrupt both frames."""
    import threading
    import time

    class OverlapDetector(FakeLineWriter):
        def __init__(self):
            super().__init__()
            self.inside = 0
            self.overlaps = 0

        def write_line(self, line):
            self.inside += 1
            if self.inside > 1:
                self.overlaps += 1
            time.sleep(0)  # yield mid-write: an unguarded writer would interleave here
            super().write_line(line)
            self.inside -= 1

    w, r = OverlapDetector(), FakeLineReader()
    client = OmpClient(w, r)

    def burst():
        for _ in range(200):
            client.send("steer", {"message": "m"})
            client.host_tool_result("h", "ok")

    threads = [threading.Thread(target=burst) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert w.overlaps == 0
    ids = [m["id"] for m in w.sent() if m["type"] == "steer"]
    assert len(ids) == 800 and len(set(ids)) == 800


def test_terminate_delegates_to_the_process_seam():
    class Proc:
        grace = None

        def terminate(self, grace_s):
            Proc.grace = grace_s
            return True

    w, r = FakeLineWriter(), FakeLineReader()
    assert OmpClient(w, r).terminate(3) is None  # no process: nothing to signal
    assert OmpClient(w, r, Proc()).terminate(3) is True and Proc.grace == 3


# -- run outcome -----------------------------------------------------------
def _agent_end(stop_reason, is_terminal=True, error=None):
    msg = {"role": "assistant", "stopReason": stop_reason}
    if error:
        msg["errorMessage"] = error
    return {"type": "agent_end", "isTerminal": is_terminal, "messages": [{"role": "user"}, msg]}


def test_terminal_agent_end_completes_without_a_later_event():
    w = RunWatcher("p1")
    assert w.observe({"type": "agent_start"}) is None
    assert w.observe(_agent_end("stop")) == RunOutcome(status="completed")


def test_prompt_acceptance_does_not_hide_a_later_scheduling_failure():
    w = RunWatcher("p1")
    assert w.observe({"id": "p1", "type": "response", "command": "prompt", "success": True}) is None
    outcome = w.observe(
        {
            "id": "p1",
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "No API key found for acme.",
        }
    )
    assert outcome.status == "failed"
    assert "No API key" in outcome.error_message


def test_rejection_terminates_without_lifecycle_events():
    w = RunWatcher("p1")
    outcome = w.observe(
        {
            "id": "p1",
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "invalid model",
        }
    )
    assert outcome == RunOutcome(status="failed", error_message="invalid model")


@pytest.mark.parametrize(
    "completion",
    [
        {
            "id": "p1",
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"agentInvoked": False},
        },
        {"id": "p1", "type": "prompt_result", "agentInvoked": False},
    ],
)
def test_local_only_prompt_finishes_without_lifecycle_events(completion):
    w = RunWatcher("p1")
    assert w.observe({**completion, "id": "other"}) is None
    assert w.observe(completion) == RunOutcome(status="completed")


def test_retry_notifications_do_not_end_the_prompt_before_its_final_verdict():
    w = RunWatcher("p1")
    assert w.observe({"type": "auto_retry_start", "attempt": 1}) is None
    assert w.observe(_agent_end("error", is_terminal=False, error="transient")) is None
    assert w.observe({"type": "agent_start"}) is None
    assert w.observe({"type": "auto_retry_end", "success": True, "attempt": 1}) is None
    assert w.observe(_agent_end("stop")) == RunOutcome(status="completed")


def test_exhausted_retry_preserves_the_final_assistant_error():
    w = RunWatcher("p1")
    assert w.observe(_agent_end("error", is_terminal=False, error="first failure")) is None
    assert (
        w.observe({"type": "auto_retry_end", "success": False, "finalError": "retry exhausted"})
        is None
    )
    outcome = w.observe(_agent_end("error", error="529 overloaded_error"))
    assert outcome == RunOutcome(status="failed", error_message="529 overloaded_error")


@pytest.mark.parametrize("will_retry", [False, True])
def test_compaction_end_is_not_a_prompt_completion_signal(will_retry):
    w = RunWatcher("p1")
    assert w.observe({"type": "auto_compaction_start", "reason": "overflow"}) is None
    assert (
        w.observe({"type": "auto_compaction_end", "aborted": False, "willRetry": will_retry})
        is None
    )
    if will_retry:
        assert w.observe(_agent_end("error", is_terminal=False, error="context overflow")) is None
        assert w.observe({"type": "agent_start"}) is None
    assert w.observe(_agent_end("stop")) == RunOutcome(status="completed")


def test_aborted_compaction_waits_for_the_terminal_agent_verdict():
    w = RunWatcher("p1")
    assert w.observe({"type": "auto_compaction_end", "aborted": True, "willRetry": False}) is None
    assert w.observe(_agent_end("aborted")).status == "aborted"


def test_queued_continuation_waits_even_after_a_successful_nonterminal_end():
    w = RunWatcher("p1")
    assert w.observe(_agent_end("stop", is_terminal=False)) is None
    assert w.observe(_agent_end("error", error="follow-up failed")).status == "failed"


def test_aborted_and_length_are_distinct_from_failure():
    assert RunWatcher("p1").observe(_agent_end("aborted")).status == "aborted"
    assert RunWatcher("p1").observe(_agent_end("length")).status == "truncated"


def test_verdict_reads_the_last_assistant_not_the_last_message():
    outcome = RunWatcher("p1").observe(
        {
            "type": "agent_end",
            "isTerminal": True,
            "messages": [
                {"role": "assistant", "stopReason": "error", "errorMessage": "old"},
                {"role": "assistant", "stopReason": "stop"},
                {"role": "toolResult"},
            ],
        }
    )
    assert outcome.status == "completed"


def test_terminal_end_without_an_assistant_does_not_report_success():
    outcome = RunWatcher("p1").observe({"type": "agent_end", "isTerminal": True, "messages": []})
    assert outcome.status == "failed"


def test_nonterminal_run_still_fails_on_stream_silence():
    writer, reader = FakeLineWriter(), FakeLineReader()
    reader.push(_agent_end("error", is_terminal=False, error="transient"))
    watcher = RunWatcher("p1")
    with pytest.raises(SilenceTimeout):
        for event in OmpClient(writer, reader).events(1):
            assert watcher.observe(event) is None


def test_compacted_terminal_frame_uses_streamed_assistant_but_retained_suffix_wins():
    watcher = RunWatcher("p1")
    watcher.observe({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
    compacted = {"type": "agent_end", "isTerminal": True, "messages": [], "messageCount": 3}
    assert watcher.observe(compacted).status == "completed"
    suffix = {"role": "assistant", "stopReason": "error", "errorMessage": "final failure"}
    assert watcher.observe({**compacted, "messages": [suffix]}) == RunOutcome(
        "failed", "final failure"
    )


def test_compacted_retry_cannot_reuse_the_previous_run_assistant():
    watcher = RunWatcher("p1")
    watcher.observe({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
    assert watcher.observe(_agent_end("stop", is_terminal=False)) is None
    watcher.observe({"type": "agent_start"})
    outcome = watcher.observe(
        {"type": "agent_end", "isTerminal": True, "messages": [], "messageCount": 1}
    )
    assert outcome.status == "failed"


def test_v1_overflow_without_terminal_metadata_fails_closed():
    watcher = RunWatcher("p1")
    watcher.observe({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
    outcome = watcher.observe({"type": "agent_end", "messages": [], "messageCount": 100})
    assert outcome.status == "failed"
