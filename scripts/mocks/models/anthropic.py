"""Anthropic Messages API: `POST /v1/messages`, streamed or not.

Follows the vendored reference (`anthropic-*.md` in `scripts/mocks/specs/`): the API key in
`x-api-key` (or `Authorization: Bearer`), a required `anthropic-version`, a JSON body checked
against the transcribed schema, documented `{"type": "error", …}` bodies, and the SSE event
flow `message_start` → content blocks (`content_block_start`/`_delta`/`_stop`, a `ping`) →
`message_delta` (stop reason + cumulative usage) → `message_stop`.
"""

from __future__ import annotations

import json

from . import schemas
from .script import MockState, Reply, ScriptError, digest, pieces, sign, signed, tokens, tool_input

VERSIONS = ("2023-01-01", "2023-06-01", "2026-10-21")
# The thinking text the mock "thinks" before each scripted step (summarized display).
THINKING = "Following the scripted certification turn."


def error(status: int, kind: str, message: str, request_id: str) -> Reply:
    return Reply(
        status,
        {"type": "error", "error": {"type": kind, "message": message}, "request_id": request_id},
        headers={"request-id": request_id},
    )


def _schema_message(failure) -> str:
    path = schemas.dotted(failure)
    if failure.validator == "additionalProperties":
        extra = sorted(set(failure.instance) - set(failure.schema.get("properties", {})))
        return f"{'.'.join(filter(None, [path, extra[0] if extra else '']))}: Extra inputs are not permitted"
    if failure.validator == "required":
        missing = failure.message.split("'")[1] if "'" in failure.message else ""
        return f"{'.'.join(filter(None, [path, missing]))}: Field required"
    return f"{path or 'body'}: {failure.message}"


def _conversation_error(body: dict) -> str | None:
    """Documented cross-field rules the schema cannot express."""
    messages = body["messages"]
    if not messages:
        return "messages: at least one message is required"
    thinking = body.get("thinking") or {}
    if thinking.get("type") == "enabled" and thinking["budget_tokens"] >= body["max_tokens"]:
        return "`max_tokens` must be greater than `thinking.budget_tokens`"
    for index, message in enumerate(messages):
        content = message["content"] if isinstance(message["content"], list) else []
        if message["role"] == "assistant":
            for position, block in enumerate(content):
                if block["type"] == "thinking" and not signed(block["thinking"], block["signature"]):
                    return f"messages.{index}.content.{position}: Invalid `signature` in `thinking` block"
            uses = [block["id"] for block in content if block["type"] == "tool_use"]
            if uses:
                following = messages[index + 1] if index + 1 < len(messages) else None
                answered = {
                    block.get("tool_use_id")
                    for block in (following or {}).get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                }
                if following is None or not set(uses) <= answered:
                    missing = ", ".join(sorted(set(uses) - answered))
                    return (
                        f"messages.{index + 1}: `tool_use` ids were found without `tool_result` "
                        f"blocks immediately after: {missing}. Each `tool_use` block must have a "
                        "corresponding `tool_result` block in the next message."
                    )
    return None


def handle(state: MockState, method: str, path: str, headers: dict, raw: bytes) -> Reply:
    request_id = "req_" + digest(raw + path.encode())[:24]
    if path != "/v1/messages" or method != "POST":
        return error(404, "not_found_error", "Not Found", request_id)
    api_key, authorization = headers.get("x-api-key"), headers.get("authorization")
    if api_key is None and authorization is None:
        return error(401, "authentication_error", "x-api-key header is required", request_id)
    if api_key is not None and api_key != state.keys["anthropic"]:
        return error(401, "authentication_error", "invalid x-api-key", request_id)
    if authorization is not None and authorization != f"Bearer {state.keys['anthropic']}":
        return error(401, "authentication_error", "invalid authorization header", request_id)
    version = headers.get("anthropic-version")
    if version is None:
        return error(400, "invalid_request_error", "anthropic-version: header is required", request_id)
    if version not in VERSIONS:
        return error(400, "invalid_request_error", f"anthropic-version: invalid version {version!r}", request_id)
    if not (headers.get("content-type") or "").startswith("application/json"):
        return error(400, "invalid_request_error", "content-type: must be application/json", request_id)
    try:
        body = json.loads(raw)
    except ValueError:
        return error(400, "invalid_request_error", "There was an error parsing the body of your request", request_id)
    validator = schemas.anthropic_validator(
        "request", beta="anthropic-beta" in headers, sampling=version != "2026-10-21"
    )
    failure = schemas.first_error(validator, body)
    if failure is not None:
        return error(400, "invalid_request_error", _schema_message(failure), request_id)
    problem = _conversation_error(body)
    if problem is not None:
        return error(400, "invalid_request_error", problem, request_id)
    if body["model"] not in state.models["anthropic"]:
        return error(404, "not_found_error", f"model: {body['model']}", request_id)

    tool_results = sum(
        1
        for message in body["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "tool_result"
    )
    step = state.step(tool_results)
    content = []
    thinking = body.get("thinking") or {}
    if thinking.get("type") in ("enabled", "adaptive"):
        text = "" if thinking.get("display") == "omitted" else THINKING
        content.append({"type": "thinking", "thinking": text, "signature": sign(text)})
    if step.text is not None:
        content.append({"type": "text", "text": step.text, "citations": None})
    if step.tool is not None:
        declared = {
            tool["name"]: tool["input_schema"] for tool in body.get("tools", []) if "input_schema" in tool
        }
        try:
            arguments = tool_input(step, declared)
        except ScriptError as exc:
            return error(500, "api_error", str(exc), request_id)
        content.append(
            {"type": "tool_use", "id": "toolu_" + digest(raw)[:24], "name": step.tool, "input": arguments}
        )
    usage = {
        "input_tokens": tokens(raw),
        "output_tokens": tokens(content),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
        "service_tier": "standard",
    }
    message = {
        "id": "msg_" + digest(raw)[24:48],
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": content,
        "stop_reason": "tool_use" if step.tool is not None else "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }
    if not body.get("stream"):
        return Reply(200, message, headers={"request-id": request_id})
    return Reply(200, events=stream(message), headers={"request-id": request_id})


def stream(message: dict) -> list[tuple[str, dict]]:
    """The documented event flow for a finished `message`."""
    start = {**message, "content": [], "stop_reason": None, "usage": {**message["usage"], "output_tokens": 1}}
    events: list[tuple[str, dict]] = [("message_start", {"type": "message_start", "message": start})]
    for index, block in enumerate(message["content"]):
        if block["type"] == "thinking":
            opening = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [{"type": "thinking_delta", "thinking": part} for part in pieces(block["thinking"], 16)]
            deltas.append({"type": "signature_delta", "signature": block["signature"]})
        elif block["type"] == "text":
            opening = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": part} for part in pieces(block["text"], 8)]
        else:
            opening = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": ""}] + [
                {"type": "input_json_delta", "partial_json": part}
                for part in pieces(json.dumps(block["input"]), 16)
            ]
        events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": opening}))
        if index == 0:
            events.append(("ping", {"type": "ping"}))
        events.extend(
            ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta})
            for delta in deltas
        )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    usage = message["usage"]
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": {
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
                    "cache_read_input_tokens": usage["cache_read_input_tokens"],
                },
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return events
