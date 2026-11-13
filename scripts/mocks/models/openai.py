"""OpenAI Responses API: `POST /v1/responses` (streamed or not) and `GET /v1/models[/{id}]`.

Follows the vendored OpenAPI description (`openai.openapi.yaml.gz`): `Authorization: Bearer`
(the `ApiKeyAuth` scheme), bodies checked against the operation's own request schema,
`ErrorResponse` bodies, and the `ResponseStreamEvent` flow `response.created` →
`response.in_progress` → per output item `response.output_item.added`, its parts/deltas
(`response.output_text.delta`, `response.function_call_arguments.delta`, reasoning summary
deltas) and `response.output_item.done` → `response.completed` with usage.
"""

from __future__ import annotations

import json
import time

from . import schemas
from .script import MockState, Reply, ScriptError, digest, pieces, sign, signed, tokens, tool_input

# What the mock "reasons" before each scripted step (returned as a reasoning summary).
REASONING = "Following the scripted certification turn."
_KEYS_URL = "https://platform.openai.com/account/api-keys"


def error(status: int, message: str, kind: str = "invalid_request_error", param=None, code=None) -> Reply:
    return Reply(status, {"error": {"message": message, "type": kind, "param": param, "code": code}})


def _masked(key: str) -> str:
    return key if len(key) <= 8 else key[:3] + "*" * (len(key) - 7) + key[-4:]


def _schema_error(failure) -> Reply:
    path = schemas.dotted(failure)
    if failure.validator == "additionalProperties":
        extra = sorted(set(failure.instance) - set(failure.schema.get("properties", {})))
        name = ".".join(filter(None, [path, extra[0] if extra else ""]))
        return error(400, f"Unknown parameter: '{name}'.", param=name, code="unknown_parameter")
    if failure.validator == "required":
        missing = failure.message.split("'")[1] if "'" in failure.message else ""
        name = ".".join(filter(None, [path, missing]))
        return error(400, f"Missing required parameter: '{name}'.", param=name, code="missing_required_parameter")
    code = "invalid_type" if failure.validator == "type" else "invalid_value"
    return error(400, f"Invalid value for '{path}': {failure.message}", param=path or None, code=code)


def _as_described(body: dict) -> dict:
    """The body as the description can check it. One known gap: `ReasoningItem` requires
    `id`, but a stateless (`store: false`) replay cannot name an item that was never stored,
    so clients send reasoning back by `encrypted_content` alone (omp 18.20.2 drops the id on
    purpose; OpenAI's Codex never serializes it). Only that case is filled in."""
    if body.get("store") is not False or not isinstance(body.get("input"), list):
        return body
    replayed = [
        {**item, "id": "rs_stateless"}
        if isinstance(item, dict) and item.get("type") == "reasoning" and "id" not in item
        else item
        for item in body["input"]
    ]
    return {**body, "input": replayed}


def _items(body: dict) -> list[dict]:
    return [item for item in body["input"] if isinstance(item, dict)] if isinstance(body.get("input"), list) else []


def _conversation_error(body: dict) -> Reply | None:
    if body.get("previous_response_id") is not None:
        # Nothing is stored by the mock; a chained request names a response it never kept.
        return error(
            400,
            f"Previous response with id '{body['previous_response_id']}' not found.",
            param="previous_response_id",
            code="previous_response_not_found",
        )
    items = _items(body)
    calls = {item.get("call_id") for item in items if item.get("type") == "function_call"}
    for index, item in enumerate(items):
        if item.get("type") == "function_call_output" and item.get("call_id") not in calls:
            return error(
                400,
                f"No tool call found for function call output with call_id {item.get('call_id')}.",
                param=f"input.{index}",
            )
        if item.get("type") == "reasoning" and item.get("encrypted_content") is not None:
            text = "".join(part.get("text", "") for part in item.get("summary", []))
            if not signed(text, item["encrypted_content"]):
                return error(
                    400,
                    "The encrypted content for item " + str(item.get("id")) + " could not be verified.",
                    param=f"input.{index}",
                    code="invalid_encrypted_content",
                )
    return None


def _model(model_id: str) -> dict:
    return {"id": model_id, "object": "model", "created": 1767225600, "owned_by": "openai"}


def handle(state: MockState, method: str, path: str, headers: dict, raw: bytes) -> Reply:
    authorization = headers.get("authorization") or ""
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        return error(
            401,
            "You didn't provide an API key. You need to provide your API key in an Authorization "
            "header using Bearer auth (i.e. Authorization: Bearer YOUR_KEY), or as the password "
            "field (with blank username) if you're accessing the API from your browser and are "
            f"prompted for a username and password. You can obtain an API key from {_KEYS_URL}.",
        )
    key = authorization[7:]
    if key != state.keys["openai"]:
        return error(
            401,
            f"Incorrect API key provided: {_masked(key)}. You can find your API key at {_KEYS_URL}.",
            code="invalid_api_key",
        )
    served = state.models["openai"]
    if method == "GET" and path == "/v1/models":
        return Reply(200, {"object": "list", "data": [_model(model) for model in served]})
    if method == "GET" and path.startswith("/v1/models/"):
        model = path.removeprefix("/v1/models/")
        if model not in served:
            return error(
                404,
                f"The model '{model}' does not exist or you do not have access to it.",
                param="model",
                code="model_not_found",
            )
        return Reply(200, _model(model))
    if method != "POST" or path != "/v1/responses":
        return error(404, f"Invalid URL ({method} {path})")
    try:
        body = json.loads(raw)
    except ValueError:
        return error(
            400,
            "We could not parse the JSON body of your request. (HINT: This likely means you aren't "
            "using your HTTP library correctly. The OpenAI API expects a JSON payload, but what was "
            "sent was not valid JSON.)",
        )
    failure = schemas.first_error(
        schemas.openai_request_validator("/responses", "post"), _as_described(body)
    )
    if failure is not None:
        return _schema_error(failure)
    unknown = sorted(set(body) - schemas.openai_request_properties("/responses", "post"))
    if unknown:
        return error(400, f"Unknown parameter: '{unknown[0]}'.", param=unknown[0], code="unknown_parameter")
    problem = _conversation_error(body)
    if problem is not None:
        return problem
    if body.get("model") not in served:
        return error(
            404,
            f"The model '{body.get('model')}' does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )

    tool_results = sum(1 for item in _items(body) if item.get("type") == "function_call_output")
    step = state.step(tool_results)
    seed = digest(raw)
    output = []
    reasoning = body.get("reasoning") or {}
    if reasoning.get("effort") not in (None, "none"):
        summary = [{"type": "summary_text", "text": REASONING}] if reasoning.get("summary") else []
        item = {"type": "reasoning", "id": "rs_" + seed[:24], "summary": summary}
        # Stateless responses carry it by default; `include` still asks for it explicitly.
        if body.get("store") is False or "reasoning.encrypted_content" in (body.get("include") or []):
            item["encrypted_content"] = sign("".join(part["text"] for part in summary))
        output.append(item)
    if step.text is not None:
        output.append(
            {
                "type": "message",
                "id": "msg_" + seed[24:48],
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": step.text, "annotations": [], "logprobs": []}],
            }
        )
    if step.tool is not None:
        tools = {tool["name"]: tool for tool in body.get("tools", []) if tool.get("type") == "function"}
        try:
            arguments = tool_input(step, {name: _strict_completion(tool) for name, tool in tools.items()})
        except ScriptError as exc:
            return error(500, str(exc), kind="server_error")
        if tools[step.tool].get("strict"):
            # Strict function calling always emits every declared property (null when unused).
            properties = (tools[step.tool].get("parameters") or {}).get("properties", {})
            arguments = {name: arguments.get(name) for name in properties}
        output.append(
            {
                "type": "function_call",
                "id": "fc_" + seed[:24],
                "call_id": "call_" + seed[24:48],
                "name": step.tool,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        )
    created = int(time.time())
    usage = {
        "input_tokens": tokens(raw),
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": tokens(output),
        "output_tokens_details": {"reasoning_tokens": tokens(REASONING) if output and output[0]["type"] == "reasoning" else 0},
    }
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    final = response(body, "resp_" + seed[:48], created, "completed", output, usage)
    if not body.get("stream"):
        return Reply(200, final)
    return Reply(200, events=stream(body, final))


def _strict_completion(tool: dict) -> dict:
    """The schema a scripted input must meet: strict tools require every property, which the
    mock fills with null itself, so the script only states what it uses."""
    parameters = dict(tool.get("parameters") or {"type": "object"})
    if tool.get("strict"):
        parameters["required"] = [
            name for name in parameters.get("required", []) if not _admits_null(parameters["properties"][name])
        ]
    return parameters


def _admits_null(schema: dict) -> bool:
    return schema.get("type") == "null" or "null" in (schema.get("type") or []) or any(
        _admits_null(option) for option in schema.get("anyOf", [])
    )


def response(body: dict, response_id: str, created: int, status: str, output: list, usage) -> dict:
    """A `Response` object echoing the request's settings, as the API does."""
    reasoning = body.get("reasoning") or {}
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "access_programs": None,
        "background": False,
        "completed_at": created if status == "completed" else None,
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "max_tool_calls": body.get("max_tool_calls"),
        "model": body["model"],
        "output": output,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "previous_response_id": None,
        "prompt_cache_key": body.get("prompt_cache_key"),
        "reasoning": {"effort": reasoning.get("effort"), "summary": reasoning.get("summary")},
        "safety_identifier": body.get("safety_identifier"),
        "service_tier": "default",
        "store": body.get("store", True),
        "temperature": body.get("temperature", 1.0),
        "text": body.get("text") or {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools", []),
        "top_logprobs": body.get("top_logprobs", 0),
        "top_p": body.get("top_p", 1.0),
        "truncation": body.get("truncation", "disabled"),
        "usage": usage,
        "user": body.get("user"),
        "metadata": body.get("metadata") or {},
    }


def stream(body: dict, final: dict) -> list[tuple[str, dict]]:
    """The documented `ResponseStreamEvent` sequence for a finished response."""
    events: list[tuple[str, dict]] = []

    def emit(kind: str, **data) -> None:
        events.append((kind, {"type": kind, "sequence_number": len(events), **data}))

    pending = {**final, "status": "in_progress", "completed_at": None, "output": [], "usage": None}
    emit("response.created", response=pending)
    emit("response.in_progress", response=pending)
    for index, item in enumerate(final["output"]):
        if item["type"] == "reasoning":
            emit("response.output_item.added", output_index=index, item={**item, "summary": []})
            for position, part in enumerate(item["summary"]):
                where = {"item_id": item["id"], "output_index": index, "summary_index": position}
                emit("response.reasoning_summary_part.added", **where, part={"type": "summary_text", "text": ""})
                for delta in pieces(part["text"], 16):
                    emit("response.reasoning_summary_text.delta", **where, delta=delta)
                emit("response.reasoning_summary_text.done", **where, text=part["text"])
                emit("response.reasoning_summary_part.done", **where, part=part)
        elif item["type"] == "message":
            emit("response.output_item.added", output_index=index, item={**item, "status": "in_progress", "content": []})
            for position, part in enumerate(item["content"]):
                where = {"item_id": item["id"], "output_index": index, "content_index": position}
                emit("response.content_part.added", **where, part={**part, "text": ""})
                for delta in pieces(part["text"], 8):
                    emit("response.output_text.delta", **where, delta=delta, logprobs=[])
                emit("response.output_text.done", **where, text=part["text"], logprobs=[])
                emit("response.content_part.done", **where, part=part)
        else:
            emit("response.output_item.added", output_index=index, item={**item, "status": "in_progress", "arguments": ""})
            where = {"item_id": item["id"], "output_index": index}
            for delta in pieces(item["arguments"], 16):
                emit("response.function_call_arguments.delta", **where, delta=delta)
            emit("response.function_call_arguments.done", **where, name=item["name"], arguments=item["arguments"])
        emit("response.output_item.done", output_index=index, item=item)
    emit("response.completed", response=final)
    return events
