"""Seiji mock: the MCP endpoint and the HTTP API of a Seiji server, from the vendored
`scripts/mocks/specs/seiji-mcp.md` (Seiji's `docs/mcp.md`). JSON-RPC messages are validated
against the vendored MCP 2025-11-25 schema.

`POST /mcp` is Streamable HTTP, stateless, JSON answers only (no SSE, no `Mcp-Session-Id`).
The checks run in Seiji's documented order:

1. `Authorization: Bearer <token>` (the exact `Bearer ` prefix), on `/mcp`, `/v1/apps`,
   `/v1/runs/…`, `/v1/me` and `/v1/leases/…`. Else 401, plain text
   `missing, invalid or disabled bearer token`. The token is checked before the `Host`.
2. `Host` in the allowed hosts (an entry without a port matches every port). Else 403
   `Forbidden: Host header is not allowed`.
3. `GET`/`DELETE /mcp`: 405 with `Allow: POST`.
4. `Accept` lists `application/json` and `text/event-stream`. Else 406.
5. `Content-Type: application/json`. Else 415.
6. `MCP-Protocol-Version`: absent means `2025-03-26`; an unknown one is 400; on `initialize`
   it must match `params.protocolVersion`.

Tools: `budget_status`, `list_device_models`, `acquire_device`, `release_device`,
`run_actions` (records a passed run) and `export_evidence`. A failing tool is a result with
`isError: true`; an unknown tool is JSON-RPC error -32602 `tool not found`. Results carry one
text block of pretty-printed JSON (Markdown for `export_evidence`). Every absolute link is
under the configured publicUrl.

HTTP API: `POST /v1/apps` (raw body, `X-Filename`, read in chunks), `DELETE /v1/apps/{id}`
(`204`, or `404` `no uploaded app "<id>"`), `GET /v1/me`,
`GET /v1/runs/{id}/evidence.zip|report.md|files/{path}`, `POST /v1/leases/{id}/viewer`,
`GET /v1/leases/{id}/screenshot.jpg`, `GET /healthz`. Error bodies are `{"error": "<text>"}`.
An upload past `max_builds` kept builds is `429` with Seiji's text.

Control (`/_control/*`, JSON, no auth):
- `GET /_control/state`: `requests` (method, path, JSON-RPC method, tool, `auth`
  `valid|missing|other`, lowercase header names, never values), `other_bearers` (SHA-256 of
  every other bearer presented), `leases`, `apps` (with the SHA-256 of each upload), `runs`,
  `viewers` (each viewer link asked for) and `receiving` (bytes of the upload in progress).
- `POST /_control/config` `{"public_url": str|null, "acquire_error": str|null,
  "max_builds": int}`.
- `POST /_control/expire_apps`: forget every uploaded build, as Seiji's reaper does
  `appRetentionHours` after an upload.
- `POST /_control/reset`: forget everything; back to the start configuration.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import secrets
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from scripts.mocks.mcp.server import validator

DEFAULT_TOKEN = "sj_mockSeijiToken0123456789abcdefghijklmnopqrs"
USER = "mock-agent"
# rmcp 3.10.0, which Seiji uses, knows these versions; `initialize` answers the latest.
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_VERSION = "2025-11-25"
DEFAULT_VERSION = "2025-03-26"
UNAUTHORIZED = b"missing, invalid or disabled bearer token"
QUOTA = {"monthlyMinutes": 1200, "maxConcurrent": 2, "maxLeaseMinutes": 120, "idleMinutes": 15}
MODELS = ("iphone-16",)
CAPACITY = 4
MAX_BUILDS = 10
MAX_UPLOAD_BYTES = 1 << 30
VIDEO = b"\x00\x00\x00\x18ftypmp42\x00\xff\xfe binary video, not text \x80\x81"

INSTRUCTIONS = """\
Seiji lends you a fresh iOS simulator (or Android emulator) on a shared Mac and records evidence of what you do on it.

Devices are metered: every minute you hold one counts against your monthly budget, so release it when done.

Workflow:
1. `budget_status` / `list_device_models` to see what you can use.
2. `acquire_device` (platform `ios`) boots a device just for you (30-90 s) and returns a `lease_id`.
3. Upload your build: `curl -H "Authorization: Bearer $SEIJI_TOKEN" -H "X-Filename: MyApp.zip" --data-binary @MyApp.zip <server>/v1/apps` (iOS: zip/tar.gz of the simulator .app), then `install_app` with the returned `app_id`.
4. Optional: `configure_device` (status bar, dark mode, location, permissions) for clean, reproducible screenshots.
5. `inspect_screen` shows the accessibility tree: use its exact labels and ids in selectors.
6. `run_actions` runs a list of steps (tap, type, scroll, wait_for, assert_visible, screenshot, launch_app, ...) and records a video, the app's logs, crash reports and a screenshot per step. It returns a `run_id`.
   `run_xctest` runs a compiled XCUITest bundle (`build-for-testing`) and keeps the `.xcresult`. `run_flow` runs Maestro YAML when the host has Maestro.
7. Evidence: `get_run` (steps and files), `get_run_artifact` (a screenshot, the log, ...), `export_evidence` (a Markdown summary for the pull request and a download link for evidence.zip).
8. `release_device` when finished. Idle devices are released automatically; runs stay for the retention period.

`lease_id` may be omitted when you hold exactly one device. `bundle_id` defaults to the app installed last on the lease."""

_LEASE_ARG = {"lease_id": {"type": "string",
                           "description": "Lease from `acquire_device`; optional when you hold exactly one device"}}
TOOLS = [
    {"name": "budget_status", "description": "Your device-minute budget for this month, your limits, and the devices you hold.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_device_models", "description": "Platforms and device models this host offers, with current occupancy.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "acquire_device", "description": "Boot a fresh iOS simulator or Android emulator for you.",
     "inputSchema": {"type": "object", "properties": {
         "platform": {"type": "string", "enum": ["ios", "android"]}, "model": {"type": "string"}},
         "required": ["platform"]}},
    {"name": "release_device", "description": "Release a device that you hold.",
     "inputSchema": {"type": "object", "properties": dict(_LEASE_ARG)}},
    {"name": "install_app", "description": "Install an app on a device.",
     "inputSchema": {"type": "object", "properties": {
         **_LEASE_ARG, "app_id": {"type": "string"}, "url": {"type": "string"}}}},
    {"name": "launch_app", "description": "Launch an app on a device.",
     "inputSchema": {"type": "object", "properties": {
         **_LEASE_ARG, "bundle_id": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "run_actions", "description": "Run a sequence of UI steps and record evidence.",
     "inputSchema": {"type": "object", "properties": {
         **_LEASE_ARG, "name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
         "actions": {"type": "array", "items": {"type": "object"}}}, "required": ["actions"]}},
    {"name": "export_evidence", "description": "Evidence for a pull request: a Markdown summary and the evidence.zip link.",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
]
REQUEST_SCHEMAS = {"initialize": "InitializeRequest", "ping": "PingRequest",
                   "tools/list": "ListToolsRequest", "tools/call": "CallToolRequest"}
_FILENAME = re.compile(r"[\x21-\x7e]+\Z")
_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")
_ACTION_KINDS = frozenset({"launch_app", "tap", "type", "scroll", "swipe", "wait_for",
                           "assert_visible", "screenshot", "press_key", "sleep"})


class HttpError(Exception):
    def __init__(self, status: int, body: bytes | dict | None, content_type: str = "text/plain; charset=utf-8",
                 headers: dict | None = None) -> None:
        super().__init__(status)
        self.status, self.body, self.content_type, self.headers = status, body, content_type, headers or {}


class ToolError(Exception):
    """A failing tool: a normal result with `isError: true`."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, status: int = 200) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    parts = urlsplit(f"//{host}")
    name, port = (parts.hostname or "").lower(), parts.port
    for entry in allowed:
        wanted = urlsplit(f"//{entry}")
        if (wanted.hostname or "").lower() == name and (wanted.port is None or wanted.port == port):
            return True
    return False


def _sanitize(name: str) -> str:
    cleaned = _SANITIZE.sub("_", name).lstrip(".")
    return cleaned or "app"


def _text(value: Any) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


class SeijiMock:
    def __init__(self, token: str = DEFAULT_TOKEN, *, public_url: str | None = None,
                 allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")) -> None:
        self.token = token
        self.allowed_hosts = tuple(allowed_hosts)
        self.start_public_url = public_url
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.public_url = self.start_public_url
            self.acquire_error: str | None = None
            self.max_builds = MAX_BUILDS
            self.requests: list[dict] = []
            self.other_bearers: list[str] = []
            self.leases: dict[str, dict] = {}
            self.apps: dict[str, dict] = {}
            self.runs: dict[str, dict] = {}
            self.viewers: list[dict] = []
            self.receiving = 0

    def state(self) -> dict:
        with self.lock:
            return {
                "requests": list(self.requests), "other_bearers": list(self.other_bearers),
                "leases": [dict(lease) for lease in self.leases.values()],
                "apps": [dict(app) for app in self.apps.values()],
                "runs": [{k: run[k] for k in ("run_id", "lease_id", "name", "status")}
                         for run in self.runs.values()],
                "viewers": list(self.viewers), "receiving": self.receiving,
                "public_url": self.public_url, "max_builds": self.max_builds,
            }

    def base(self) -> str:
        return (self.public_url or "").rstrip("/")

    # -- checks ---------------------------------------------------------------
    def authorize(self, headers) -> str:
        """`valid`, or raises 401. A bearer that is not the token is recorded by its hash."""
        header = headers.get("Authorization")
        if header is None:
            raise HttpError(401, UNAUTHORIZED)
        if header.startswith("Bearer ") and hmac.compare_digest(header[7:], self.token):
            return "valid"
        if header.startswith("Bearer "):
            with self.lock:
                self.other_bearers.append(hashlib.sha256(header[7:].encode()).hexdigest())
        raise HttpError(401, UNAUTHORIZED)

    # -- leases ---------------------------------------------------------------
    def _active(self) -> list[dict]:
        return [lease for lease in self.leases.values() if lease["status"] in ("booting", "ready")]

    def resolve(self, lease_id: Any) -> dict:
        """Seiji's `lease_id` rule."""
        active = self._active()
        if lease_id is None:
            if not active:
                raise ToolError("you hold no device; call acquire_device first")
            if len(active) > 1:
                raise ToolError(f"you hold {len(active)} devices; pass lease_id")
            return active[0]
        found = next((lease for lease in active if lease["lease_id"] == lease_id), None)
        if found is None:
            raise ToolError(f'no active lease "{lease_id}" for you')
        return found

    @staticmethod
    def view(lease: dict) -> dict:
        return {key: lease[key] for key in ("lease_id", "platform", "model", "device_id", "status",
                                             "started_at", "ends_at_latest", "idle_release_minutes")}

    # -- tools ----------------------------------------------------------------
    def call_tool(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise RpcError(-32602, "tool not found")
        try:
            with self.lock:
                return handler(args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

    def _tool_budget_status(self, args: dict) -> dict:
        used = 0.0
        return _text({"user": USER, "month": time.strftime("%Y-%m", time.gmtime()),
                      "used_minutes": used, "monthly_minutes": QUOTA["monthlyMinutes"],
                      "remaining_minutes": QUOTA["monthlyMinutes"] - used,
                      "max_concurrent": QUOTA["maxConcurrent"],
                      "max_lease_minutes": QUOTA["maxLeaseMinutes"],
                      "idle_minutes": QUOTA["idleMinutes"],
                      "active_leases": [self.view(lease) for lease in self._active()]})

    def _tool_list_device_models(self, args: dict) -> dict:
        return _text([{"platform": "ios", "models": list(MODELS), "default_model": MODELS[0],
                       "capacity": CAPACITY, "in_use": len(self._active())}])

    def _tool_acquire_device(self, args: dict) -> dict:
        platform = args.get("platform")
        if platform is None:
            raise RpcError(-32602, "failed to deserialize parameters: missing field `platform`")
        if platform not in ("ios", "android"):
            raise RpcError(-32602, f"failed to deserialize parameters: unknown variant `{platform}`, "
                                   "expected `ios` or `android`")
        model = args.get("model")
        if platform == "android":
            if model is None:
                raise ToolError("android is not available on this host")
            raise ToolError(f'unknown android model "{model}"; available: []')
        if model is not None and model not in MODELS:
            raise ToolError(f'unknown ios model "{model}"; available: {json.dumps(list(MODELS))}')
        if len(self._active()) >= QUOTA["maxConcurrent"]:
            raise ToolError(f"you already hold {len(self._active())} device(s), your limit is "
                            f"{QUOTA['maxConcurrent']}; release one first")
        if self.acquire_error:
            raise ToolError(self.acquire_error)
        now = int(time.time())
        lease_id = f"l_{secrets.token_hex(6)}"
        lease = {"lease_id": lease_id, "platform": "ios", "model": model or MODELS[0],
                 "device_id": secrets.token_hex(16).upper(), "status": "ready", "started_at": now,
                 "ends_at_latest": now + QUOTA["maxLeaseMinutes"] * 60,
                 "idle_release_minutes": QUOTA["idleMinutes"], "end_reason": None}
        self.leases[lease_id] = lease
        return _text(self.view(lease))

    def _tool_release_device(self, args: dict) -> dict:
        lease = self.resolve(args.get("lease_id"))
        lease["status"], lease["end_reason"] = "released", "released"
        return _text(f"released {lease['lease_id']}")

    def _tool_install_app(self, args: dict) -> dict:
        lease = self.resolve(args.get("lease_id"))
        app_id = args.get("app_id")
        url = args.get("url")
        if not app_id and not url:
            raise ToolError("pass app_id or url")
        if app_id and app_id not in self.apps:
            raise ToolError(f'no uploaded app "{app_id}"')
        return _text(f"installed on {lease['lease_id']}")

    def _tool_launch_app(self, args: dict) -> dict:
        lease = self.resolve(args.get("lease_id"))
        bundle_id = args.get("bundle_id") or "com.example.app"
        return _text(f"launched {bundle_id} on {lease['lease_id']}")

    def _tool_run_actions(self, args: dict) -> dict:
        actions = args.get("actions")
        if not isinstance(actions, list):
            raise RpcError(-32602, "failed to deserialize parameters: missing field `actions`")
        if not 1 <= len(actions) <= 200:
            raise ToolError("run_actions takes 1-200 actions")
        for index, action in enumerate(actions, 1):
            kind = next(iter(action), None) if isinstance(action, dict) and len(action) == 1 else None
            if kind not in _ACTION_KINDS:
                raise RpcError(-32602, f"failed to deserialize parameters: action {index} is not valid")
        lease = self.resolve(args.get("lease_id"))
        name = args.get("name")
        if name is not None and not (isinstance(name, str) and 1 <= len(name) <= 200):
            raise ToolError("name must be 1-200 characters")
        run_id = f"r_{secrets.token_hex(6)}"
        steps = [{"index": i, "step": next(iter(a)), "status": "passed", "duration_ms": 100,
                  "error": None, "files": [f"steps/{i:03d}-{next(iter(a))}.png"]}
                 for i, a in enumerate(actions)]
        run = {"run_id": run_id, "lease_id": lease["lease_id"], "name": name, "status": "passed",
               "model": lease["model"], "steps": steps, "actions": actions}
        self.runs[run_id] = run
        return _text({"run_id": run_id, "status": "passed",
                      "verdict": f"{len(steps)} of {len(steps)} steps passed", "steps": steps,
                      "files": len(steps) + 3, "video": True, "crash_reports": 0, "dropped_files": 0,
                      "warnings": [], "manifest_sha256": self.manifest_sha(run),
                      "evidence_zip": f"{self.base()}/v1/runs/{run_id}/evidence.zip",
                      "next": "get_run for every file, get_run_artifact to look at one, "
                              "export_evidence for a PR summary"})

    def _tool_export_evidence(self, args: dict) -> dict:
        run_id = args.get("run_id")
        if not isinstance(run_id, str):
            raise RpcError(-32602, "failed to deserialize parameters: missing field `run_id`")
        return _text(self.report(self.run(run_id)))

    # -- runs -----------------------------------------------------------------
    def run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise ToolError(f'no run "{run_id}" for you')
        return run

    def manifest(self, run: dict) -> bytes:
        files = [{"path": "video.mp4", "size": len(VIDEO), "sha256": hashlib.sha256(VIDEO).hexdigest()},
                 {"path": "app.log", "size": len(self.app_log(run))}]
        return json.dumps({"run_id": run["run_id"], "lease_id": run["lease_id"], "status": run["status"],
                           "files": files}, indent=2).encode()

    def manifest_sha(self, run: dict) -> str:
        return hashlib.sha256(self.manifest(run)).hexdigest()

    def app_log(self, run: dict) -> bytes:
        return f"run {run['run_id']} on {run['model']}\nsee {self.base()}/v1/runs/{run['run_id']}/files/video.mp4\n".encode()

    def report(self, run: dict) -> str:
        base = f"{self.base()}/v1/runs/{run['run_id']}"
        title = run["name"] or "actions run"
        rows = "\n".join(f"| {s['index'] + 1} | ✅ `{json.dumps(a, separators=(',', ':'))[:120]}` | passed | 0.1s |  |"
                         for s, a in zip(run["steps"], run["actions"], strict=True))
        return (f"### ✅ {title}\n\n**passed**: {len(run['steps'])} of {len(run['steps'])} steps passed\n\n"
                f"- Device: {run['model']} (ios, com.apple.CoreSimulator.SimRuntime.iOS-18-4)\n"
                f"- Run: `{run['run_id']}` (1s), lease `{run['lease_id']}`\n\n"
                f"| # | Step | Status | Time | Error |\n|---|---|---|---|---|\n{rows}\n\n"
                f"Files:\n- [app.log]({base}/files/app.log) ({len(self.app_log(run))} bytes)\n"
                f"- [video.mp4]({base}/files/video.mp4) ({len(VIDEO)} bytes)\n\n"
                f"Evidence: [evidence.zip]({base}/evidence.zip) · manifest SHA-256 "
                f"`{self.manifest_sha(run)}` · 2 files\n")

    def evidence_zip(self, run: dict) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"{run['run_id']}/manifest.json", self.manifest(run))
            archive.writestr(f"{run['run_id']}/video.mp4", VIDEO)
            archive.writestr(f"{run['run_id']}/app.log", self.app_log(run))
        return buffer.getvalue()


def make_handler(mock: SeijiMock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _reply(self, status: int, body: bytes | dict | list | None = None,
                   content_type: str = "application/json", headers: dict | None = None) -> None:
            raw = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _error(self, status: int, text: str) -> None:
            self._reply(status, {"error": text})

        def _record(self, path: str, auth: str, rpc_method: str | None = None, tool: str | None = None) -> None:
            with mock.lock:
                mock.requests.append({"method": self.command, "path": path, "rpc_method": rpc_method,
                                      "tool": tool, "auth": auth,
                                      "headers": sorted({name.lower() for name in self.headers})})

        def _chunks(self):
            """The request body in chunks: by Content-Length, or chunked transfer coding."""
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return
                    left = size
                    while left:
                        piece = self.rfile.read(min(left, 65536))
                        if not piece:
                            return
                        left -= len(piece)
                        yield piece
                    self.rfile.readline()
            left = int(self.headers.get("Content-Length") or 0)
            while left > 0:
                piece = self.rfile.read(min(left, 65536))
                if not piece:
                    return
                left -= len(piece)
                yield piece

        def _body(self) -> bytes:
            return b"".join(self._chunks())

        def do_GET(self):  # noqa: N802
            self._dispatch()

        def do_POST(self):  # noqa: N802
            self._dispatch()

        def do_DELETE(self):  # noqa: N802
            self._dispatch()

        def do_PUT(self):  # noqa: N802
            self._dispatch()

        def do_HEAD(self):  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            path = urlsplit(self.path).path
            try:
                if path.startswith("/_control/"):
                    return self._control(path[len("/_control/"):])
                if path == "/healthz" and self.command == "GET":
                    return self._reply(200, b"ok", "text/plain; charset=utf-8")
                if path == "/mcp":
                    return self._mcp()
                if path.startswith(("/v1/apps", "/v1/runs/", "/v1/me", "/v1/leases/")):
                    return self._api(path)
                self._body()
                return self._reply(404, b"Not Found", "text/plain; charset=utf-8")
            except HttpError as exc:
                body = exc.body
                content_type = exc.content_type if not isinstance(body, dict) else "application/json"
                return self._reply(exc.status, body, content_type, exc.headers)

        # -- MCP ----------------------------------------------------------------
        def _mcp(self) -> None:
            raw = self._body()
            auth = "missing" if self.headers.get("Authorization") is None else "other"
            try:
                auth = mock.authorize(self.headers)
            finally:
                if auth != "valid":
                    self._record("/mcp", auth)
            if not _host_allowed(self.headers.get("Host") or "", mock.allowed_hosts):
                self._record("/mcp", auth)
                raise HttpError(403, b"Forbidden: Host header is not allowed")
            if self.command != "POST":
                self._record("/mcp", auth)
                raise HttpError(405, b"Method Not Allowed", headers={"Allow": "POST"})
            accept = [part.split(";")[0].strip().lower() for part in (self.headers.get("Accept") or "").split(",")]
            if "application/json" not in accept or "text/event-stream" not in accept:
                self._record("/mcp", auth)
                raise HttpError(406, b"Not Acceptable: Client must accept both application/json and "
                                     b"text/event-stream")
            media = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if media != "application/json":
                self._record("/mcp", auth)
                raise HttpError(415, b"Unsupported Media Type: Content-Type must be application/json")
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                self._record("/mcp", auth)
                raise HttpError(400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "Parse error"}}) from None
            method = message.get("method") if isinstance(message, dict) else None
            params = message.get("params") if isinstance(message, dict) and isinstance(message.get("params"), dict) else {}
            tool = params.get("name") if method == "tools/call" else None
            self._record("/mcp", auth, method, tool if isinstance(tool, str) else None)
            version = self.headers.get("MCP-Protocol-Version")
            if version is not None and version not in SUPPORTED_VERSIONS:
                raise HttpError(400, f"Bad Request: Unsupported MCP-Protocol-Version: {version}".encode())
            if not isinstance(message, dict) or not validator("JSONRPCMessage").is_valid(message):
                raise HttpError(400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32600, "message": "Invalid Request"}})
            if "method" not in message:
                raise HttpError(400, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": "stateless server does not accept JSON-RPC responses"}})
            if "id" not in message:
                return self._reply(202)
            rpc_id = message["id"]
            if method == "initialize" and version is not None and version != params.get("protocolVersion"):
                raise HttpError(400, {"jsonrpc": "2.0", "id": rpc_id, "error": {
                    "code": -32600,
                    "message": f"Invalid Request: MCP-Protocol-Version header ({version}) does not "
                               f"match initialize params.protocolVersion ({params.get('protocolVersion')})"}})
            schema = REQUEST_SCHEMAS.get(method)
            try:
                if schema is None:
                    raise RpcError(-32601, "Method not found", 404)
                if not validator(schema).is_valid(message):
                    raise RpcError(-32602, "Invalid params")
                result = self._method(method, params)
            except RpcError as exc:
                return self._reply(exc.status, {"jsonrpc": "2.0", "id": rpc_id,
                                                "error": {"code": exc.code, "message": exc.message}})
            self._reply(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result})

        def _method(self, method: str, params: dict) -> dict:
            if method == "initialize":
                requested = params["protocolVersion"]
                version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
                return {"protocolVersion": version, "capabilities": {"tools": {}},
                        "serverInfo": {"name": "seiji", "version": "0.1.0"}, "instructions": INSTRUCTIONS}
            if method == "ping":
                return {}
            if method == "tools/list":
                return {"tools": TOOLS}
            return mock.call_tool(params["name"], params.get("arguments") or {})

        # -- HTTP API -----------------------------------------------------------
        def _api(self, path: str) -> None:
            auth = "missing" if self.headers.get("Authorization") is None else "other"
            try:
                auth = mock.authorize(self.headers)
            finally:
                self._record(path, auth)
            parts = path.strip("/").split("/")
            if parts == ["v1", "apps"] and self.command == "POST":
                return self._upload()
            if parts[:2] == ["v1", "apps"] and len(parts) == 3 and self.command == "DELETE":
                self._body()
                with mock.lock:
                    if mock.apps.pop(parts[2], None) is None:
                        return self._error(404, f'no uploaded app "{parts[2]}"')
                return self._reply(204)
            if parts == ["v1", "me"] and self.command in ("GET", "HEAD"):
                self._body()
                return self._reply(200, {"user": USER, "quota": QUOTA})
            if parts[:2] == ["v1", "runs"] and len(parts) >= 4 and self.command in ("GET", "HEAD"):
                self._body()
                return self._run_file(parts[2], parts[3:])
            if parts[:2] == ["v1", "leases"] and len(parts) == 4:
                return self._lease_route(parts[2], parts[3])
            self._body()
            raise HttpError(404, b"Not Found")

        def _upload(self) -> None:
            filename = self.headers.get("X-Filename")
            if filename is None or not _FILENAME.fullmatch(filename):
                self._body()
                return self._error(400, "X-Filename header is required (e.g. MyApp.zip or app-debug.apk)")
            with mock.lock:
                count, limit = len(mock.apps), mock.max_builds
            if count >= limit:
                self._body()
                return self._error(429, f"you already keep {count} builds, the limit is {limit}; "
                                        "they expire 72h after upload")
            digest, size = hashlib.sha256(), 0
            with mock.lock:
                mock.receiving = 0
            for piece in self._chunks():
                size += len(piece)
                if size > MAX_UPLOAD_BYTES:
                    return self._error(413, f"upload exceeds maxUploadBytes ({MAX_UPLOAD_BYTES} bytes)")
                digest.update(piece)
                with mock.lock:
                    mock.receiving = size
            if size == 0:
                return self._error(400, "empty upload")
            app_id = f"a_{secrets.token_hex(6)}"
            app = {"app_id": app_id, "filename": _sanitize(filename), "size": size,
                   "sha256": digest.hexdigest(),
                   "chunked": "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()}
            with mock.lock:
                mock.apps[app_id] = app
                mock.receiving = 0
            self._reply(200, {"app_id": app_id, "filename": app["filename"], "size": size})

        def _run_file(self, run_id: str, rest: list[str]) -> None:
            with mock.lock:
                try:
                    run = mock.run(run_id)
                except ToolError as exc:
                    return self._error(404, str(exc))
                if rest == ["evidence.zip"]:
                    body, kind = mock.evidence_zip(run), "application/zip"
                    headers = {"Content-Disposition": f'attachment; filename="{run_id}-evidence.zip"'}
                elif rest == ["report.md"]:
                    body, kind = mock.report(run).encode(), "text/markdown; charset=utf-8"
                    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
                elif rest[:1] == ["files"] and "/".join(rest[1:]) in ("manifest.json", "app.log", "video.mp4"):
                    name = "/".join(rest[1:])
                    body, kind = {"manifest.json": (mock.manifest(run), "application/json"),
                                  "app.log": (mock.app_log(run), "text/plain; charset=utf-8"),
                                  "video.mp4": (VIDEO, "video/mp4")}[name]
                    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
                    if name == "video.mp4":
                        headers["Content-Disposition"] = 'attachment; filename="video.mp4"'
                elif rest[:1] == ["files"]:
                    return self._error(404, f'the run has no file "{"/".join(rest[1:])}"')
                else:
                    raise HttpError(404, b"Not Found")
            self._reply(200, body, kind, headers)

        def _lease_route(self, lease_id: str, action: str) -> None:
            raw = self._body()
            if action == "viewer" and self.command == "POST":
                control = True
                if raw.strip():
                    try:
                        body = json.loads(raw)
                    except ValueError as exc:
                        return self._error(400, f"invalid body: {exc}")
                    if not isinstance(body, dict) or set(body) - {"control"} or not isinstance(
                            body.get("control", True), bool):
                        return self._error(400, "invalid body: expected {\"control\": <bool>}")
                    control = body.get("control", True)
                with mock.lock:
                    try:
                        lease = mock.resolve(lease_id)
                    except ToolError as exc:
                        return self._error(404, str(exc))
                    if lease["platform"] != "ios":
                        return self._error(404, "the screen viewer supports iOS simulators only for now")
                    mock.viewers.append({"lease_id": lease_id, "control": control})
                    url = f"{mock.base()}/view/open/{secrets.token_urlsafe(24)}"
                return self._reply(200, {"url": url, "expires_in": 60, "control": control,
                                         "lease_id": lease_id}, headers={"Cache-Control": "no-store"})
            if action == "screenshot.jpg" and self.command == "GET":
                with mock.lock:
                    try:
                        mock.resolve(lease_id)
                    except ToolError as exc:
                        return self._error(404, str(exc))
                return self._reply(200, b"\xff\xd8\xff\xe0 mock jpeg \xff\xd9", "image/jpeg",
                                   {"Cache-Control": "no-store"})
            raise HttpError(404, b"Not Found")

        # -- control ------------------------------------------------------------
        def _control(self, name: str) -> None:
            raw = self._body()
            if self.command == "GET" and name == "state":
                return self._reply(200, mock.state())
            if self.command == "POST" and name == "reset":
                mock.reset()
                return self._reply(200, {"ok": True})
            if self.command == "POST" and name == "expire_apps":
                with mock.lock:
                    mock.apps.clear()
                return self._reply(200, {"ok": True})
            if self.command == "POST" and name == "config":
                data = json.loads(raw or b"{}")
                with mock.lock:
                    if "public_url" in data:
                        mock.public_url = data["public_url"]
                    if "acquire_error" in data:
                        mock.acquire_error = data["acquire_error"]
                    if "max_builds" in data:
                        mock.max_builds = int(data["max_builds"])
                return self._reply(200, {"public_url": mock.public_url, "acquire_error": mock.acquire_error,
                                         "max_builds": mock.max_builds})
            return self._reply(404, {"error": "not served by the Seiji mock"})

    return Handler


def serve(port: int, token: str = DEFAULT_TOKEN, host: str = "127.0.0.1", *,
          public_url: str | None = None,
          allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")) -> ThreadingHTTPServer:
    """A Seiji mock on `host:port` (0 picks a port). `public_url` defaults to the listen
    origin, so links are absolute; `POST /_control/config {"public_url": null}` makes them
    relative, as Seiji without `listen.publicUrl`."""
    mock = SeijiMock(token, allowed_hosts=allowed_hosts)
    server = ThreadingHTTPServer((host, port), make_handler(mock))
    server.daemon_threads = True
    mock.start_public_url = public_url or f"http://{host}:{server.server_port}"
    mock.reset()
    server.mock = mock  # type: ignore[attr-defined]
    return server
