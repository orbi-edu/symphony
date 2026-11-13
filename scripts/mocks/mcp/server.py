"""MCP mock: one Streamable HTTP server (MCP 2025-11-25), validated against the vendored
`scripts/mocks/specs/mcp-2025-11-25-schema.json.gz` and the `mcp-2025-11-25-*.md` pages.

The MCP endpoint is `/mcp` (POST, GET, DELETE):

- Every request needs `Authorization: Bearer <token>` (the `--token` value), so tests can
  prove the Symphony gateway adds it. A missing or wrong token gets 401 with
  `WWW-Authenticate: Bearer`.
- An `Origin` header that is not a localhost origin gets 403 (the transport's security rule).
- POST carries one JSON-RPC message (no batches) and must accept both `application/json` and
  `text/event-stream`. A request gets one JSON object, or an SSE stream when the SSE mode is
  on (a priming event with an id and empty data first, then the response). A notification or
  response gets 202 with no body.
- `initialize` assigns `MCP-Session-Id`. Later requests without it get 400; an unknown or
  deleted session gets 404. An unsupported `MCP-Protocol-Version` gets 400.
- GET with `Accept: text/event-stream` opens the server stream (a priming event, then comment
  keep-alives until the client closes it). DELETE ends the session.
- Served JSON-RPC methods: `initialize`, `ping`, `tools/list`, `tools/call` (tools `echo` and
  `lookup_ticket_context`), `notifications/initialized`, `notifications/cancelled`. Any other
  method gets HTTP 501 with JSON-RPC error -32601. Any other path or HTTP method gets 501.
- A message that fails the schema gets 400 with JSON-RPC error -32600 (request shape) or
  -32602 (params).

Control (`/_control/*`, JSON, no auth):
- `GET /_control/requests`: every recorded MCP request: HTTP method, JSON-RPC method,
  header names (lowercase, never values) and `has_auth`.
- `GET /_control/streams`: `{"open": n}`, the SSE streams still open.
- `POST /_control/config` `{"sse": bool, "sse_delay_s": float}`: the response mode.
- `POST /_control/reset`: forget requests and sessions; back to JSON mode.
"""

from __future__ import annotations

import functools
import gzip
import hmac
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "mcp-2025-11-25-schema.json.gz"
SPEC_URI = "urn:mcp-2025-11-25"
PROTOCOL_VERSION = "2025-11-25"
# lifecycle.md: a server answers with a version it supports. 2025-03-26 is the transport's
# assumed version when no header is sent.
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
DEFAULT_TOKEN = "mock-mcp-token"
ENDPOINT = "/mcp"
KEEPALIVE_S = 0.5
STREAM_CAP_S = 300.0

TOOLS = [
    {
        "name": "echo",
        "title": "Echo",
        "description": "Return the text it is given.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "lookup_ticket_context",
        "title": "Look up ticket context",
        "description": "Return the stored context for a ticket identifier such as ENG-1.",
        "inputSchema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["identifier", "summary"],
        },
    },
]
REQUEST_SCHEMAS = {
    "initialize": "InitializeRequest",
    "ping": "PingRequest",
    "tools/list": "ListToolsRequest",
    "tools/call": "CallToolRequest",
}
NOTIFICATION_SCHEMAS = {
    "notifications/initialized": "InitializedNotification",
    "notifications/cancelled": "CancelledNotification",
}


@functools.cache
def load_spec() -> dict:
    with gzip.open(SPEC_PATH) as fh:
        return json.load(fh)


@functools.cache
def _registry() -> Registry:
    return Registry().with_resource(SPEC_URI, DRAFT202012.create_resource(load_spec()))


@functools.cache
def validator(name: str) -> Draft202012Validator:
    """A validator for one schema definition (`InitializeResult`)."""
    return Draft202012Validator({"$ref": f"{SPEC_URI}#/$defs/{name}"}, registry=_registry())


class McpError(Exception):
    def __init__(self, status: int, code: int | None, message: str, rpc_id: Any = None,
                 headers: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers or {}
        self.body = None
        if code is not None:
            # transports.md: an error body MAY be a JSON-RPC error response without an id.
            self.body = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
            if rpc_id is not None:
                self.body["id"] = rpc_id


def _local_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


class McpMock:
    def __init__(self, token: str = DEFAULT_TOKEN) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.requests: list[dict] = []
            self.sessions: dict[str, dict] = {}
            self.sse = False
            self.sse_delay_s = 0.0
            self.open_streams = 0

    # -- checks ---------------------------------------------------------------
    def record(self, verb: str, headers, rpc_method: str | None) -> None:
        names = sorted({name.lower() for name in headers})
        with self.lock:
            self.requests.append({
                "method": verb, "rpc_method": rpc_method, "headers": names,
                "has_auth": "authorization" in names,
            })

    def authorize(self, headers) -> None:
        origin = headers.get("Origin")
        if origin is not None and not _local_origin(origin):
            raise McpError(403, -32600, "Origin is not allowed.")
        scheme, _, presented = (headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented, self.token):
            raise McpError(401, None, "unauthorized",
                           headers={"WWW-Authenticate": 'Bearer realm="mcp-mock"'})

    def session(self, headers, *, initializing: bool = False) -> dict | None:
        if initializing:
            return None
        sid = headers.get("MCP-Session-Id")
        if not sid:
            raise McpError(400, -32600, "Bad Request: MCP-Session-Id header is required.")
        with self.lock:
            state = self.sessions.get(sid)
        if state is None:
            raise McpError(404, -32001, "Session not found.")
        version = headers.get("MCP-Protocol-Version")
        if version is not None and version not in SUPPORTED_VERSIONS:
            raise McpError(400, -32600, f"Unsupported MCP-Protocol-Version: {version}")
        return state

    @staticmethod
    def check_message(message: Any) -> tuple[str, str]:
        """('request'|'notification'|'response', method) after schema validation."""
        if not isinstance(message, dict):
            raise McpError(400, -32600, "The body must be one JSON-RPC message.")
        if not validator("JSONRPCMessage").is_valid(message):
            raise McpError(400, -32600, "Invalid JSON-RPC message.", message.get("id"))
        if "method" not in message:
            return "response", ""
        method = message["method"]
        if "id" in message:
            schema = REQUEST_SCHEMAS.get(method)
            if schema is None:
                raise McpError(501, -32601, f"Method not found: {method}", message["id"])
            errors = list(validator(schema).iter_errors(message))
            if errors:
                raise McpError(400, -32602, f"Invalid params: {errors[0].message[:200]}",
                               message["id"])
            return "request", method
        schema = NOTIFICATION_SCHEMAS.get(method)
        if schema is None:
            raise McpError(501, -32601, f"Method not found: {method}")
        if not validator(schema).is_valid(message):
            raise McpError(400, -32602, "Invalid notification params.")
        return "notification", method

    # -- methods --------------------------------------------------------------
    def initialize(self, params: dict) -> tuple[dict, str]:
        requested = params["protocolVersion"]
        version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
        sid = uuid.uuid4().hex
        with self.lock:
            self.sessions[sid] = {"version": version, "initialized": False}
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "symphony-mcp-mock", "version": "1.0.0"},
            "instructions": "A mock MCP server for Symphony tests.",
        }
        return result, sid

    @staticmethod
    def call_tool(params: dict) -> dict:
        name, arguments = params["name"], params.get("arguments") or {}
        if name == "echo":
            if not isinstance(arguments.get("text"), str):
                raise McpError(400, -32602, "echo needs a text string.")
            return {"content": [{"type": "text", "text": arguments["text"]}], "isError": False}
        if name == "lookup_ticket_context":
            identifier = arguments.get("identifier")
            if not isinstance(identifier, str):
                raise McpError(400, -32602, "lookup_ticket_context needs an identifier string.")
            summary = f"{identifier}: no linked incidents; owner team ENG."
            return {
                "content": [{"type": "text", "text": summary}],
                "structuredContent": {"identifier": identifier, "summary": summary},
                "isError": False,
            }
        # tools.md: an unknown tool is a protocol error.
        raise McpError(400, -32602, f"Unknown tool: {name}")


def make_handler(mock: McpMock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _send(self, status: int, data: Any = None, headers: dict | None = None) -> None:
            raw = b"" if data is None else json.dumps(data).encode()
            self.send_response(status)
            if data is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _event(self, event_id: str | None, data: str) -> None:
            frame = (f"id: {event_id}\n" if event_id else "") + f"data: {data}\n\n"
            self._chunk(frame.encode())

        def _chunk(self, raw: bytes) -> None:
            self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
            self.wfile.flush()

        def _open_sse(self, headers: dict | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            with mock.lock:
                mock.open_streams += 1

        def _close_sse(self) -> None:
            with mock.lock:
                mock.open_streams -= 1
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

        def do_PUT(self):  # noqa: N802
            self._dispatch("PUT")

        def do_PATCH(self):  # noqa: N802
            self._dispatch("PATCH")

        def _dispatch(self, verb: str) -> None:
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if path.startswith("/_control/"):
                return self._control(verb, path[len("/_control/"):], raw)
            if path != ENDPOINT or verb not in ("GET", "POST", "DELETE"):
                return self._send(501, {"error": "not served by the MCP mock"})
            try:
                if verb == "POST":
                    return self._post(raw)
                mock.record(verb, self.headers, None)
                mock.authorize(self.headers)
                if verb == "DELETE":
                    mock.session(self.headers)
                    with mock.lock:
                        mock.sessions.pop(self.headers["MCP-Session-Id"], None)
                    return self._send(200)
                return self._listen()
            except McpError as exc:
                return self._send(exc.status, exc.body, exc.headers)

        def _post(self, raw: bytes) -> None:
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                mock.record("POST", self.headers, None)
                raise McpError(400, -32700, "Parse error.") from None
            rpc_method = message.get("method") if isinstance(message, dict) else None
            mock.record("POST", self.headers, rpc_method)
            mock.authorize(self.headers)
            accept = self.headers.get("Accept", "")
            if "application/json" not in accept or "text/event-stream" not in accept:
                raise McpError(406, -32600, "Accept must list application/json and text/event-stream.")
            kind, method = mock.check_message(message)
            state = mock.session(self.headers, initializing=method == "initialize")
            if kind != "request":
                if method == "notifications/initialized" and state is not None:
                    state["initialized"] = True
                return self._send(202)
            sid = self.headers.get("MCP-Session-Id")
            params = message.get("params") or {}
            if method == "initialize":
                result, sid = mock.initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            else:
                try:
                    result = mock.call_tool(params)
                except McpError as exc:
                    exc.body["id"] = message["id"]
                    raise
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            headers = {"MCP-Session-Id": sid} if method == "initialize" else {}
            if not mock.sse:
                return self._send(200, response, headers)
            self._open_sse(headers)
            try:
                self._event(f"{sid}-{uuid.uuid4().hex[:8]}", "")
                time.sleep(mock.sse_delay_s)
                self._event(f"{sid}-{uuid.uuid4().hex[:8]}", json.dumps(response))
            except OSError:
                pass
            finally:
                self._close_sse()
            self.close_connection = True

        def _listen(self) -> None:
            if "text/event-stream" not in self.headers.get("Accept", ""):
                raise McpError(406, -32600, "Accept must list text/event-stream.")
            mock.session(self.headers)
            sid = self.headers["MCP-Session-Id"]
            self._open_sse()
            started = time.monotonic()
            try:
                self._event(f"{sid}-listen", "")
                while time.monotonic() - started < STREAM_CAP_S:
                    time.sleep(KEEPALIVE_S)
                    with mock.lock:
                        if sid not in mock.sessions:
                            break
                    self._chunk(b": keep-alive\n\n")
            except OSError:
                pass
            finally:
                self._close_sse()
            self.close_connection = True

        def _control(self, verb: str, name: str, raw: bytes) -> None:
            if verb == "GET" and name == "requests":
                with mock.lock:
                    return self._send(200, {"requests": list(mock.requests)})
            if verb == "GET" and name == "streams":
                with mock.lock:
                    return self._send(200, {"open": mock.open_streams})
            if verb == "POST" and name == "reset":
                mock.reset()
                return self._send(200, {"ok": True})
            if verb == "POST" and name == "config":
                data = json.loads(raw or b"{}")
                with mock.lock:
                    mock.sse = bool(data.get("sse", mock.sse))
                    mock.sse_delay_s = float(data.get("sse_delay_s", mock.sse_delay_s))
                return self._send(200, {"sse": mock.sse, "sse_delay_s": mock.sse_delay_s})
            return self._send(501, {"error": "not served by the MCP mock"})

    return Handler


def serve(port: int, token: str = DEFAULT_TOKEN, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(McpMock(token)))
    server.daemon_threads = True
    return server
