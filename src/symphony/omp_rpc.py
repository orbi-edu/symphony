"""Newline-delimited JSON client for Oh My Pi's RPC mode — pure over streams.

Uses the supported v1 transport; commands and events are single JSONL frames.

This is NOT JSON-RPC 2.0. The engine speaks its own envelope:

    command   host   → engine  {"id": ..., "type": ..., ...}
    response  engine → host    {"id": ..., "type": "response", "command": ..., "success": ...}
    event     engine → host    {"type": ..., ...}   (no id, not correlated)

Timeout semantics carried over from the previous client:
- `read_timeout` bounds a synchronous command → response;
- `turn_timeout` is the maximum stream SILENCE during a run, reset by every message —
  NOT a cap on run duration.
"""

from __future__ import annotations

import base64
import json
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol


class LineWriter(Protocol):
    def write_line(self, line: str) -> None: ...


class LineReader(Protocol):
    def read_line(self, timeout_s: float) -> str | None:
        """Next frame, or None on silence (never raises on silence)."""
        ...


class EngineProcess(Protocol):
    def terminate(self, grace_s: float) -> bool:
        """SIGTERM the engine and wait up to `grace_s` for it to exit; True once it has."""
        ...


class OmpError(Exception):
    pass


class ReadTimeout(OmpError):
    pass


class SilenceTimeout(OmpError):
    pass


class CommandFailed(OmpError):
    """The engine accepted the frame and answered `success: false`."""


def split_frames(buffer: str) -> tuple[list[str], str]:
    """Split a byte-stream buffer into complete frames, LF ONLY.

    The engine's framing is deliberately LF-only: payload strings may contain other
    Unicode separators (U+2028, U+2029) as ordinary characters. Splitting on those —
    which is what Node's `readline` does, and why the engine's own docs warn against
    it — would corrupt any frame carrying such text. A trailing CR is stripped so
    CRLF input is tolerated.

    Returns (complete frames, remainder to carry into the next read).
    """
    parts = buffer.split("\n")
    remainder = parts.pop()
    frames = [line[:-1] if line.endswith("\r") else line for line in parts]
    return [f for f in frames if f.strip()], remainder


# Protocol v2 (`negotiate_protocol`) splits frames over 1 MiB into `rpc_chunk` lines
# instead of dropping their payload; this is the engine's own reassembly ceiling.
MAX_REASSEMBLED_BYTES = 64 * 1024 * 1024


class _ChunkAssembler:
    """Reassembles one `rpc_chunk` sequence at a time, as the engine emits them."""

    def __init__(self) -> None:
        self._id: str | None = None
        self._parts: list[bytes] = []
        self._size = 0
        self._count = 0
        self._length = 0

    def push(self, frame: dict) -> dict | None:
        chunk_id, index = frame.get("chunkId"), frame.get("index")
        count, length = frame.get("count"), frame.get("byteLength")
        if not isinstance(count, int) or not isinstance(length, int) or not isinstance(index, int):
            raise OmpError("invalid rpc chunk metadata")
        if length > MAX_REASSEMBLED_BYTES:
            raise OmpError("rpc chunk sequence exceeds the reassembly limit")
        if index == 0:
            if self._id is not None:
                raise OmpError("rpc chunk sequence interrupted")
            self._id, self._parts, self._size = chunk_id, [], 0
            self._count, self._length = count, length
        elif chunk_id != self._id or index != len(self._parts):
            raise OmpError("rpc chunk sequence mismatch")
        try:
            data = base64.b64decode(frame.get("data") or "", validate=True)
        except ValueError as exc:
            raise OmpError("invalid rpc chunk data") from exc
        self._parts.append(data)
        self._size += len(data)
        if self._size > self._length:
            raise OmpError("rpc chunk sequence exceeds its declared length")
        if len(self._parts) < self._count:
            return None
        payload = b"".join(self._parts)
        self._id, self._parts = None, []
        if len(payload) != self._length:
            raise OmpError("rpc chunk sequence length mismatch")
        return _decode(payload.decode("utf-8"))


def _decode(line: str) -> dict:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        raise OmpError(f"malformed frame: {line[:200]!r}") from exc
    if not isinstance(msg, dict):
        raise OmpError(f"unexpected frame payload: {line[:200]!r}")
    return msg


class OmpClient:
    """One engine conversation. Reading belongs to a single thread (the runner's turn
    loop); writing is safe from any thread — the intent pump and host-tool workers write
    steer/abort/tool results while the loop is blocked reading."""

    def __init__(
        self, writer: LineWriter, reader: LineReader, process: EngineProcess | None = None
    ) -> None:
        self._writer = writer
        self._reader = reader
        self._process = process
        self._next_id = 0
        self._pending: deque[dict] = deque()
        self._write_lock = threading.Lock()
        self._chunks = _ChunkAssembler()

    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            self._writer.write_line(line)

    def _read(self, timeout_s: float) -> dict | None:
        """Next logical message (chunk sequences reassembled), or None on silence."""
        while True:
            line = self._reader.read_line(timeout_s)
            if line is None:
                return None
            msg = _decode(line)
            if msg.get("type") != "rpc_chunk":
                return msg
            whole = self._chunks.push(msg)
            if whole is not None:
                return whole

    def send(self, type_: str, params: dict | None = None) -> str:
        """Fire-and-forget command. Returns the id, for later correlation."""
        with self._write_lock:
            self._next_id += 1
            req_id = f"c{self._next_id}"
            self._writer.write_line(
                json.dumps({"id": req_id, "type": type_, **(params or {})}, ensure_ascii=False)
            )
        return req_id

    def command(self, type_: str, params: dict | None = None, timeout_s: float = 5.0) -> dict:
        """Synchronous command → response. Messages arriving in between are queued for
        `events()`, never dropped. Only the reading thread may call this."""
        req_id = self.send(type_, params)
        while True:
            msg = self._read(timeout_s)
            if msg is None:
                raise ReadTimeout(f"no response to {type_!r} within {timeout_s}s")
            if msg.get("type") == "response" and msg.get("id") == req_id:
                if not msg.get("success"):
                    raise CommandFailed(f"{type_} failed: {msg.get('error')}")
                data = msg.get("data")
                return data if isinstance(data, dict) else {}
            # includes the id-less parse-failure response, which cannot be correlated
            self._pending.append(msg)

    def respond(self, req_id: Any, payload: dict) -> None:
        self._send({"id": req_id, **payload})

    def host_tool_result(self, call_id: str, text: str, *, is_error: bool = False) -> None:
        """Complete a pending `host_tool_call` (rpc-types.ts `RpcHostToolResult`)."""
        self._send(
            {
                "type": "host_tool_result",
                "id": call_id,
                "result": {"content": [{"type": "text", "text": text}]},
                "isError": is_error,
            }
        )

    def negotiate(self, timeout_s: float) -> int:
        """Opt into protocol v2 so frames over 1 MiB arrive chunked instead of elided."""
        try:
            self.command("negotiate_protocol", {"protocolVersion": 2}, timeout_s)
        except CommandFailed:
            return 1
        return 2

    def terminate(self, grace_s: float) -> bool | None:
        """Graceful engine stop: SIGTERM and wait, so the last telemetry batch is flushed
        (closing stdin alone exits in ~40 ms and drops it). None without a process seam."""
        if self._process is None:
            return None
        return self._process.terminate(grace_s)

    def events(self, silence_timeout_s: float) -> Iterator[dict]:
        """Stream of engine messages. Every message resets the silence timer; an
        overrun raises SilenceTimeout (an attempt failure, never a crash)."""
        while True:
            if self._pending:
                yield self._pending.popleft()
                continue
            msg = self._read(silence_timeout_s)
            if msg is None:
                raise SilenceTimeout(f"engine stream silent for {silence_timeout_s}s")
            yield msg


# --------------------------------------------------------------------------
# run outcome classification
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RunOutcome:
    status: str  # completed | failed | aborted | truncated
    error_message: str | None = None


# engine stopReason → our attempt-level verdict
_STOP_REASON_STATUS = {
    "stop": "completed",
    "toolUse": "completed",
    "error": "failed",
    "aborted": "aborted",
    "length": "truncated",
}


class RunWatcher:
    """Classify one prompt using OMP's terminal session event.

    OMP defers `agent_end` until prompt unwind and marks scheduled retries,
    compaction continuations, and queued work with `isTerminal: false`. Retry
    and compaction notifications are not completion signals themselves.
    A matching rejection or local-only result can finish without an agent turn.
    """

    def __init__(self, prompt_id: str) -> None:
        self._prompt_id = prompt_id
        self._last_assistant: dict | None = None

    def observe(self, msg: dict) -> RunOutcome | None:
        """Feed one engine message. Returns the verdict once the run is over."""
        msg_type = msg.get("type")
        if msg_type == "agent_start":
            self._last_assistant = None
        elif msg_type == "message_end":
            message = msg.get("message") or {}
            if message.get("role") == "assistant":
                self._last_assistant = message

        if (
            msg_type == "response"
            and msg.get("command") == "prompt"
            and msg.get("id") == self._prompt_id
        ):
            if not msg.get("success"):
                # Scheduling errors can follow an immediate acceptance response.
                return RunOutcome(
                    status="failed", error_message=str(msg.get("error") or "prompt rejected")
                )
            if (msg.get("data") or {}).get("agentInvoked") is False:
                return RunOutcome(status="completed")
            return None

        if msg_type == "prompt_result" and msg.get("id") == self._prompt_id:
            if msg.get("agentInvoked") is False:
                return RunOutcome(status="completed")
            return None

        if msg_type == "agent_end" and msg.get("isTerminal") is not False:
            # The v1 last-resort overflow frame drops terminal metadata. Never
            # mistake a possibly nonterminal retry for a completed prompt.
            if "messageCount" in msg and "isTerminal" not in msg:
                return RunOutcome(
                    status="failed", error_message="RPC agent_end exceeded the transport limit"
                )
            return self._verdict(msg)

        return None

    def _verdict(self, event: dict) -> RunOutcome:
        messages = event.get("messages") or []
        assistant = None
        for message in reversed(messages):
            if not isinstance(message, dict):
                return RunOutcome(
                    status="failed", error_message="RPC agent_end messages were truncated"
                )
            if message.get("role") == "assistant":
                assistant = message
                break
        # OMP compacts oversized terminal frames by omitting the prefix already
        # delivered through message_end. The retained suffix wins when present.
        if assistant is None and event.get("messageCount", len(messages)) > len(messages):
            assistant = self._last_assistant
        if assistant is None:
            return RunOutcome(status="failed", error_message="run ended with no assistant message")
        if assistant.get("rpcFrameElidedKeys"):
            return RunOutcome(status="failed", error_message="RPC assistant verdict was truncated")
        reason = assistant.get("stopReason") or "stop"
        status = _STOP_REASON_STATUS.get(reason, "failed")
        error = assistant.get("errorMessage") if status == "failed" else None
        if status == "failed" and not error:
            error = f"run ended with stopReason {reason!r}"
        return RunOutcome(status=status, error_message=error)
