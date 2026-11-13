"""MCP over streamable HTTP (protocol 2025-06-18): Symphony as tools in everyone's omp.

`POST /mcp` takes one JSON-RPC message and answers with one JSON body (no server-initiated
stream, so `GET /mcp` is 405). Authentication is the same bearer token as `/v1`; tools that
change anything need the operator role and refuse with a tool error otherwise.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..admin_auth import Caller
from ..mcp_servers import McpError
from .auth import Authenticator
from .control import Control
from .errors import ApiError

PROTOCOL = "2025-06-18"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "symphony", "title": "Symphony", "version": __version__}


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    schema["additionalProperties"] = False
    return schema


_SID = {"type": "string", "description": "Session id, e.g. '<issue_id>:<seq>'"}
_REASON = {"type": "string", "description": "Why (recorded in the audit log), 8+ characters"}

TOOLS: list[dict] = [
    {
        "name": "list_sessions",
        "description": "List Symphony agent sessions (newest first) with ticket, state, turn, "
        "model, tokens and last message.",
        "inputSchema": _schema({
            "state": {"type": "string", "enum": ["starting", "running", "waiting_input", "idle", "ended"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
        }),
        "role": "viewer",
    },
    {
        "name": "get_session",
        "description": "One session's metadata and the tail of its transcript.",
        "inputSchema": _schema({
            "sid": _SID,
            "transcript_limit": {"type": "integer", "minimum": 0, "maximum": 200, "default": 40},
        }, ["sid"]),
        "role": "viewer",
    },
    {
        "name": "send_message",
        "description": "Send a message to a running agent. mode auto answers a pending "
        "question, steers a running turn, or queues a follow-up.",
        "inputSchema": _schema({
            "sid": _SID,
            "text": {"type": "string"},
            "mode": {"type": "string", "enum": ["auto", "steer", "follow_up"], "default": "auto"},
        }, ["sid", "text"]),
        "role": "operator",
    },
    {
        "name": "interrupt",
        "description": "Abort the agent's current turn.",
        "inputSchema": _schema({"sid": _SID, "reason": {"type": "string"}}, ["sid"]),
        "role": "operator",
    },
    {
        "name": "list_issues",
        "description": "Tickets Symphony is tracking: Symphony status, Linear state, session.",
        "inputSchema": _schema(),
        "role": "viewer",
    },
    {
        "name": "overview",
        "description": "Daemon health, counts by phase, tokens and cost today, merger status.",
        "inputSchema": _schema(),
        "role": "viewer",
    },
    {
        "name": "pause",
        "description": "Pause new dispatches (running agents continue).",
        "inputSchema": _schema({"reason": _REASON}, ["reason"]),
        "role": "operator",
    },
    {
        "name": "resume",
        "description": "Resume dispatching.",
        "inputSchema": _schema({"reason": _REASON}, ["reason"]),
        "role": "operator",
    },
    {
        "name": "hold",
        "description": "Take a ticket over: Symphony stops working on it (a running attempt is "
        "interrupted, the workspace is kept) until it is released.",
        "inputSchema": _schema({
            "issue": {"type": "string", "description": "Ticket identifier (ENG-123) or issue id"},
            "reason": _REASON,
        }, ["issue", "reason"]),
        "role": "operator",
    },
    {
        "name": "release",
        "description": "Give a held ticket back to Symphony.",
        "inputSchema": _schema({
            "issue": {"type": "string", "description": "Ticket identifier (ENG-123) or issue id"},
            "reason": _REASON,
        }, ["issue", "reason"]),
        "role": "operator",
    },
    {
        "name": "merger_status",
        "description": "Merger pipeline per repository: liveness, PRs in review, parked, the "
        "merge train, batch and queue.",
        "inputSchema": _schema(),
        "role": "viewer",
    },
    {
        "name": "setup_status",
        "description": "First-run setup checklist: which steps (tracker, GitHub App, "
        "repositories, models, notifications, webhooks) are done, and what is missing.",
        "inputSchema": _schema(),
        "role": "viewer",
    },
    {
        "name": "mcp_servers",
        "description": "MCP servers the agents get (Admin › MCP): name, transport, roles and "
        "whether each is enabled. Read-only.",
        "inputSchema": _schema(),
        "role": "viewer",
    },
]
_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _rpc_error(mid, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _text(value: Any, *, error: bool = False) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if not isinstance(value, str) and not error:
        result["structuredContent"] = value if isinstance(value, dict) else {"items": value}
    if error:
        result["isError"] = True
    return result


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ApiError(422, "invalid_request", f"{key} must be an integer in {low}..{high}")
    return value


def _str(args: dict, key: str, *, required: bool = True) -> str:
    value = args.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value):
        raise ApiError(422, "invalid_request", f"{key} must be a non-empty string")
    return value


def call_tool(control: Control, caller: Caller, name: str, args: dict) -> Any:
    if name == "list_sessions":
        state = args.get("state")
        return control.list_sessions([state] if state else None, _int(args, "limit", 20, 1, 200))
    if name == "get_session":
        return control.get_session(
            _str(args, "sid"), _int(args, "transcript_limit", 40, 0, 200)
        )
    if name == "send_message":
        return control.send_message(
            caller, _str(args, "sid"), _str(args, "text"), args.get("mode", "auto")
        )
    if name == "interrupt":
        return control.interrupt(caller, _str(args, "sid"), _str(args, "reason", required=False))
    if name == "list_issues":
        return control.issues()
    if name == "overview":
        return control.overview()
    if name in ("pause", "resume"):
        return control.command(caller, name, _str(args, "reason"))
    if name in ("hold", "release"):
        return control.command(caller, name, _str(args, "reason"), _str(args, "issue"))
    if name == "merger_status":
        return control.merger_status()
    if name == "setup_status":
        return control.setup.status()
    if name == "mcp_servers":
        if control.s.mcp is None:
            raise ApiError(503, "mcp_unavailable", "MCP server storage is not reachable")
        try:
            servers = control.s.mcp.list()
        except McpError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None
        return [
            {key: server[key] for key in ("name", "transport", "roles", "enabled")}
            for server in servers
        ]
    raise KeyError(name)


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter()

    @router.get("/mcp")
    def no_stream():
        return Response(status_code=405, headers={"Allow": "POST"})

    @router.delete("/mcp")
    def no_session():
        return Response(status_code=405, headers={"Allow": "POST"})

    @router.post("/mcp")
    async def mcp(request: Request):
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in SUPPORTED:
            return _rpc_error(None, -32600, f"unsupported MCP protocol version {version}", 400)
        caller = await _authenticate(auth, request)
        try:
            message = json.loads(await request.body())
        except ValueError:
            return _rpc_error(None, -32700, "parse error", 400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "expected one JSON-RPC 2.0 message", 400)
        mid = message.get("id")
        method = message.get("method")
        if "id" not in message or method is None:
            # Notifications and responses (e.g. notifications/initialized) need no answer.
            return Response(status_code=202)
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            result = {
                "protocolVersion": requested if requested in SUPPORTED else PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER,
                "instructions": "Symphony runs autonomous coding agents for Linear tickets. "
                "Use overview/list_sessions to see what is happening and send_message to talk "
                "to an agent.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [{k: v for k, v in tool.items() if k != "role"} for tool in TOOLS]
            }
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            tool = _BY_NAME.get(name)
            if tool is None or not isinstance(args, dict):
                return _rpc_error(mid, -32602, f"unknown tool {name!r}")
            if not caller.allows(tool["role"]):
                result = _text(f"forbidden: {name} requires the {tool['role']} role", error=True)
            else:
                try:
                    result = _text(await run_in_threadpool(call_tool, control, caller, name, args))
                except ApiError as exc:
                    result = _text(f"{exc.code}: {exc.message}", error=True)
        else:
            return _rpc_error(mid, -32601, f"method not found: {method}")
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})

    return router


async def _authenticate(auth: Authenticator, request: Request) -> Caller:
    return await run_in_threadpool(auth.caller, request)
