"""omp session events and transcript entries → ACP `session/update` payloads.

Ported from omp 18.20.2 `modes/acp/acp-event-mapper.ts` (MIT) and the replay half of
`modes/acp/acp-agent.ts`, adapted to what the bridge sees: RPC frames in `delta` mode (contracts
§2: `message_update` carries only the increment and `message: {role}`), recorded by a runner on
another machine. So there is no local cwd to resolve paths against and no file to stat: tool
locations are the agent's own paths, which is what Tern needs to draw reads and edits natively.

Symphony's own kinds (`symphony.*`) become short notes, and a human input request becomes
a question block people answer by typing in the chat.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

TEXT_LIMIT = 4000
_URL_SUBJECT = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
# omp `read` selectors: `file.py:50-80`, `file.py:raw`, `file.py:-20`, `file.py:5,9`.
_READ_SELECTOR = re.compile(r"(:(raw|-?\d[\d+,\-]*))+$")

STATE_LABEL = {
    "starting": "starting",
    "running": "running",
    "waiting_input": "waiting for an answer",
    "idle": "idle",
    "ended": "ended",
}

COMMANDS = [
    {"name": "interrupt", "description": "Stop the agent's current turn"},
    {"name": "pause", "description": "Pause Symphony: it takes no new tickets"},
    {"name": "resume", "description": "Resume Symphony: it takes tickets again"},
    {"name": "status", "description": "Show the session state, turn, tokens and PR"},
    {"name": "takeover", "description": "Show how to take over the session with omp"},
]


def limit(text: str) -> str:
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


def _str(obj: Any, key: str) -> str | None:
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def text_chunk(text: str, kind: str = "agent_message_chunk", message_id: str | None = None) -> dict:
    upd: dict = {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
    if message_id:
        upd["messageId"] = message_id
    return upd


def note(text: str) -> dict:
    """Symphony's own words in the chat, as a message of their own: Tern glues a chunk without
    its own message id to the agent's next live chunk (headless check)."""
    return text_chunk(text, message_id=f"symphony-{uuid.uuid4().hex}")


# -- tools -------------------------------------------------------------------

def tool_kind(name: str) -> str:
    return {
        "read": "read",
        "write": "edit",
        "edit": "edit",
        "delete": "delete",
        "move": "move",
        "bash": "execute",
        "shell": "execute",
        "exec": "execute",
        "eval": "execute",
        "grep": "search",
        "glob": "search",
        "ast_grep": "search",
        "web_search": "fetch",
        "todo": "think",
    }.get(name, "other")


def _is_command(name: str) -> bool:
    return name in ("bash", "shell", "exec")


def _eval_text(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    cells = args.get("cells") if isinstance(args.get("cells"), list) else (
        [args] if isinstance(args.get("code"), str) else [])
    lines: list[str] = []
    for cell in cells:
        code = _str(cell, "code")
        if not code:
            continue
        language = _str(cell, "language") or "?"
        title = _str(cell, "title")
        lines += [f"[{language}] {title}" if title else f"[{language}]", code]
    return limit("\n".join(lines)) if lines else None


def _start_text(name: str, args: Any) -> str | None:
    if _is_command(name):
        command = _str(args, "command")
        return limit(f"$ {command}") if command else None
    if name == "eval":
        return _eval_text(args)
    return None


def _start_content(name: str, args: Any) -> list[dict]:
    """Body a tool call opens with. Unlike omp's mapper, a shell command is not repeated as
    `$ cmd` text: Tern already draws `rawInput.command` as the block's head (headless check),
    so the echo only doubled it. Eval cells have no such head and keep their code."""
    text = _eval_text(args) if name == "eval" else None
    return [_text_content(text)] if text else []


def tool_title(name: str, args: Any, intent: str | None = None) -> str:
    start = _start_text(name, args)
    if start:
        return start
    if intent and intent.strip():
        return intent.strip()
    subject = _str(args, "path") or _str(args, "command") or _str(args, "pattern") or _str(args, "query")
    if subject:
        return subject if _URL_SUBJECT.match(subject) else f"{name}: {subject}"
    return name


def _text_content(text: str) -> dict:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _locations(args: Any, name: str | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for key in ("path", "oldPath", "newPath"):
        raw = _str(args, key)
        if not raw or _URL_SUBJECT.match(raw):
            continue
        if key == "path" and name == "read":
            raw = _READ_SELECTOR.sub("", raw)
        if raw and raw not in seen:
            seen.add(raw)
            out.append({"path": raw})
    return out


def _result_locations(result: Any) -> list[dict]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return []
    out = _locations(details)
    seen = {loc["path"] for loc in out}
    for key in ("resolvedPath",):
        raw = _str(details, key)
        if raw and raw not in seen:
            seen.add(raw)
            out.append({"path": raw})
    for entry in details.get("perFileResults") or []:
        raw = _str(entry, "path")
        if raw and raw not in seen:
            seen.add(raw)
            out.append({"path": raw})
    return out


def _diffs(result: Any) -> list[dict]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return []
    entries = details.get("perFileResults")
    blocks = []
    for entry in entries if isinstance(entries, list) else [details]:
        if not isinstance(entry, dict) or entry.get("isError") is True:
            continue
        path = _str(entry, "path")
        old, new = entry.get("oldText"), entry.get("newText")
        if path and (isinstance(old, str) or isinstance(new, str)):
            blocks.append({"type": "diff", "path": path,
                           "oldText": old if isinstance(old, str) else None,
                           "newText": new if isinstance(new, str) else ""})
    return blocks


def _content_blocks(value: Any) -> list | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        return value["content"]
    return None


def _block_content(block: Any) -> dict | None:
    kind = _str(block, "type")
    if kind == "text":
        text = _str(block, "text")
        return _text_content(limit(text)) if text else None
    if kind in ("image", "audio"):
        data, mime = _str(block, "data"), _str(block, "mimeType")
        if data and mime:
            return {"type": "content", "content": {"type": kind, "data": data, "mimeType": mime}}
        return None
    if kind == "resource_link":
        uri, name = _str(block, "uri"), _str(block, "name")
        return {"type": "content", "content": {"type": "resource_link", "uri": uri, "name": name}} \
            if uri and name else None
    return None


def _readable_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return limit(text) if text else None
    if not isinstance(value, dict):
        return None
    direct = _str(value, "text") or _str(value, "errorMessage") or _str(value, "message")
    if direct:
        return limit(direct.strip()) or None
    blocks = _content_blocks(value)
    if blocks is not None:
        text = "\n".join(t for b in blocks if (t := _str(b, "text")))
        return limit(text.strip()) if text.strip() else None
    details = value.get("details")
    if isinstance(details, dict) and (details.get("images") or details.get("terminalId")):
        return None
    try:
        return limit(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return None


def _result_content(value: Any) -> list[dict]:
    content = [c for b in (_content_blocks(value) or []) if (c := _block_content(b))]
    text = _readable_text(value)
    if text and not any(c["content"].get("text") == text for c in content if c["content"]["type"] == "text"):
        content.append(_text_content(text))
    return content


def _merge(start: list[dict], result: list[dict]) -> list[dict]:
    merged = list(start)
    texts = {c["content"].get("text") for c in start if c.get("type") == "content"}
    for item in result:
        if item.get("type") == "content" and item["content"].get("text") in texts:
            continue
        merged.append(item)
    return merged


def tool_call_start(call_id: str, name: str, args: Any, intent: str | None = None,
                    status: str = "pending") -> dict:
    upd: dict = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": tool_title(name, args, intent),
        "kind": tool_kind(name),
        "status": status,
        "rawInput": args if args is not None else {},
    }
    start = _start_content(name, args)
    if start:
        upd["content"] = start
    locations = _locations(args, name)
    if locations:
        upd["locations"] = locations
    return upd


def tool_call_end(call_id: str, name: str, args: Any, result: Any, is_error: bool) -> list[dict]:
    content = _merge(_start_content(name, args), _diffs(result) + _result_content(result))
    upd: dict = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "failed" if is_error else "completed",
        "rawOutput": result,
    }
    if content:
        upd["content"] = content
    locations = _result_locations(result)
    if locations:
        upd["locations"] = locations
    out = [upd]
    plan = _todo_plan(name, result, is_error)
    if plan is not None:
        out.append(plan)
    return out


def _todo_plan(name: str, result: Any, is_error: bool) -> dict | None:
    if name != "todo" or is_error or not isinstance(result, dict):
        return None
    details = result.get("details")
    phases = details.get("phases") if isinstance(details, dict) else None
    if not isinstance(phases, list):
        return None
    entries = []
    for phase in phases:
        for task in (phase.get("tasks") if isinstance(phase, dict) else None) or []:
            content = _str(task, "content")
            if content:
                entries.append({"content": content, "priority": "medium",
                                "status": _todo_status(task.get("status"))})
    return {"sessionUpdate": "plan", "entries": entries}


def _todo_status(status: Any) -> str:
    return {"in_progress": "in_progress", "completed": "completed", "abandoned": "completed"}.get(
        status, "pending")


# -- messages ----------------------------------------------------------------

def message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(limit(t) for b in content if (t := _str(b, "text")))


def question_block(request: dict) -> str:
    """The block shown when the agent asks a human (`symphony_ask`)."""
    lines = ["---", "**The agent asks**", "", str(request.get("question", "")).strip()]
    options = [o for o in request.get("options") or [] if isinstance(o, str)]
    if options:
        lines.append("")
        lines += [f"{i}. {opt}" for i, opt in enumerate(options, 1)]
    lines += ["", "_Answer here. Your next message in this chat is the answer._", "", "---"]
    return "\n".join(lines)


class LiveMapper:
    """Stateful mapping of one session's live event stream. `echo()` registers text this
    bridge just sent, so the agent's echo of that user message is not drawn twice."""

    def __init__(self) -> None:
        self._text_emitted: set[str] = set()
        self._tool_args: dict[str, tuple[str, Any]] = {}
        self._echo: list[str] = []
        self.meta: dict = {}

    def echo(self, text: str) -> None:
        self._echo.append(text.strip())
        del self._echo[:-20]

    def map(self, kind: str, data: dict) -> list[dict]:
        handler = getattr(self, "_on_" + kind.replace(".", "_"), None)
        return handler(data) if handler else []

    # omp frames ---------------------------------------------------------
    def _on_message_start(self, data: dict) -> list[dict]:
        message = data.get("message") or {}
        if message.get("role") not in ("user", "developer", "custom"):
            return []
        text = message_text(message).strip()
        if not text:
            return []
        if text in self._echo:
            self._echo.remove(text)
            return []
        return [text_chunk(text, "user_message_chunk", data.get("messageId"))]

    def _on_message_update(self, data: dict) -> list[dict]:
        if (data.get("message") or {}).get("role") != "assistant":
            return []
        mid = data.get("messageId")
        ev = data.get("assistantMessageEvent") or {}
        etype = ev.get("type")
        if etype == "text_delta":
            text, kind = ev.get("delta") or "", "agent_message_chunk"
            if text and mid:
                self._text_emitted.add(mid)
        elif etype == "thinking_delta":
            text, kind = ev.get("delta") or "", "agent_thought_chunk"
        elif etype == "done":
            if mid in self._text_emitted:
                return []
            text, kind = message_text(ev.get("message")), "agent_message_chunk"
            if text and mid:
                self._text_emitted.add(mid)
        elif etype == "error":
            err = ev.get("error") or {}
            text, kind = (err.get("errorMessage") or "Unknown error"), "agent_message_chunk"
            if mid:
                self._text_emitted.add(mid)
        elif etype == "image_end" and isinstance(ev.get("content"), dict):
            return [{"sessionUpdate": "agent_message_chunk", "content": ev["content"]}]
        else:
            return []
        return [text_chunk(text, kind, mid)] if text else []

    def _on_message_end(self, data: dict) -> list[dict]:
        message = data.get("message") or {}
        mid = data.get("messageId")
        if message.get("role") != "assistant" or mid in self._text_emitted:
            return []
        text = message_text(message)
        if not text:
            return []
        if mid:
            self._text_emitted.add(mid)
        return [text_chunk(text, "agent_message_chunk", mid)]

    def _on_tool_execution_start(self, data: dict) -> list[dict]:
        call_id, name = data.get("toolCallId"), data.get("toolName")
        if not call_id or not name:
            return []
        args = data.get("args")
        self._tool_args[call_id] = (name, args)
        return [tool_call_start(call_id, name, args, data.get("intent"), status="in_progress")]

    def _on_tool_execution_update(self, data: dict) -> list[dict]:
        call_id, name = data.get("toolCallId"), data.get("toolName")
        if not call_id or not name:
            return []
        args = data.get("args", self._tool_args.get(call_id, (name, None))[1])
        partial = data.get("partialResult")
        content = _merge(_start_content(name, args), _result_content(partial))
        upd: dict = {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                     "status": _update_status(partial), "rawOutput": partial}
        if content:
            upd["content"] = content
        locations = _locations(args, name)
        if locations:
            upd["locations"] = locations
        return [upd]

    def _on_tool_execution_end(self, data: dict) -> list[dict]:
        call_id, name = data.get("toolCallId"), data.get("toolName")
        if not call_id or not name:
            return []
        args = data.get("args", self._tool_args.pop(call_id, (name, None))[1])
        return tool_call_end(call_id, name, args, data.get("result"), bool(data.get("isError")))

    def _on_todo_reminder(self, data: dict) -> list[dict]:
        entries = [{"content": t.get("content", ""), "priority": "medium",
                    "status": _todo_status(t.get("status"))} for t in data.get("todos") or []]
        return [{"sessionUpdate": "plan", "entries": entries}]

    def _on_todo_auto_clear(self, data: dict) -> list[dict]:
        return [{"sessionUpdate": "plan", "entries": []}]

    # Symphony kinds -----------------------------------------------------
    def _on_symphony_state(self, data: dict) -> list[dict]:
        state = data.get("state")
        self.meta["state"] = state
        if state == "ended":
            return [note("_The session ended._")]
        return []

    def _on_symphony_input_requested(self, data: dict) -> list[dict]:
        self.meta["input_request"] = data
        return [note(question_block(data))]

    def _on_symphony_input_resolved(self, data: dict) -> list[dict]:
        self.meta["input_request"] = None
        by = data.get("by") or "someone"
        if data.get("answer") is None:
            return [note(f"_No answer in time. The agent continues with its best guess ({by})._")]
        return [note(f"_The agent received the answer (from {by})._")]

    def _on_symphony_intent_applied(self, data: dict) -> list[dict]:
        if data.get("ignored"):
            return [note("_Message ignored. This attempt is no longer the current one for the ticket._")]
        return []


def _update_status(partial: Any) -> str:
    details = partial.get("details") if isinstance(partial, dict) else None
    state = _str((details or {}).get("async") if isinstance(details, dict) else None, "state")
    if state == "completed":
        return "completed"
    return "failed" if state in ("failed", "cancelled") else "in_progress"


# -- transcript replay -------------------------------------------------------

def replay(entries: list[dict]) -> list[dict]:
    """ACP updates for a recorded transcript (omp `SessionEntry` list; only `message` entries
    carry conversation)."""
    out: list[dict] = []
    started: dict[str, Any] = {}
    for i, entry in enumerate(entries):
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        mid = entry.get("id") or f"r{i}"
        if role == "assistant":
            out += _replay_assistant(message, mid, started)
        elif role in ("user", "developer", "custom", "hookMessage", "bashExecution",
                      "pythonExecution", "compactionSummary"):
            text = message_text(message)
            if text:
                out.append(text_chunk(text, "user_message_chunk", mid))
        elif role == "toolResult" and message.get("toolCallId") and message.get("toolName"):
            call_id, name = message["toolCallId"], message["toolName"]
            if call_id not in started:
                details = message.get("details")
                path = _str(details, "path")
                out.append(tool_call_start(call_id, name, {"path": path} if path else {}))
            result = {"content": message.get("content"), "details": message.get("details"),
                      "errorMessage": message.get("errorMessage")}
            out += tool_call_end(call_id, name, started.get(call_id), result,
                                 message.get("isError") is True)
    return out


def _replay_assistant(message: dict, mid: str, started: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for item in message.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and _str(item, "text"):
            out.append(text_chunk(item["text"], "agent_message_chunk", mid))
        elif kind == "thinking" and _str(item, "thinking") and item["thinking"].strip():
            out.append(text_chunk(item["thinking"].strip(),
                                  "agent_message_chunk" if item.get("narration") else "agent_thought_chunk",
                                  mid))
        elif kind == "image" and _str(item, "data") and _str(item, "mimeType"):
            out.append({"sessionUpdate": "agent_message_chunk", "messageId": mid,
                        "content": {"type": "image", "data": item["data"], "mimeType": item["mimeType"]}})
        elif kind in ("toolCall", "tool_use") and _str(item, "id") and _str(item, "name"):
            args = item.get("arguments", item.get("input", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            started[item["id"]] = args
            out.append(tool_call_start(item["id"], item["name"], args))
    if not out and _str(message, "errorMessage") and message.get("stopReason") != "aborted":
        out.append(text_chunk(message["errorMessage"], "agent_message_chunk", mid))
    return out
