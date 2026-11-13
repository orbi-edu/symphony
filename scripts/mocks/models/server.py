"""HTTP server: vendor routes under `/anthropic` and `/openai`, control API under `/_mock`.

Control API (JSON):
- `GET /_mock/health` → `{ok: true}`
- `GET /_mock/state` → keys, served models and script
- `PUT /_mock/keys` `{anthropic?, openai?}` — the API keys each vendor accepts
- `PUT /_mock/models` `{anthropic?: [ids], openai?: [ids]}` — the models each vendor serves
- `PUT /_mock/script` `{steps: [{text?, tool?: {name, input}}]}` — the scripted turn
- `GET /_mock/requests[?vendor=anthropic|openai]` → every vendor request, headers and body
- `DELETE /_mock/requests` — forget them
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import anthropic, openai, schemas
from .script import MockState, Reply

_VENDORS = {"/anthropic": ("anthropic", anthropic.handle), "/openai": ("openai", openai.handle)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ModelsServer

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's signature
        pass

    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def _send_json(self, status: int, body, headers: dict | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_events(self, reply: Reply) -> None:
        self.send_response(reply.status)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.end_headers()
        for event, data in reply.events or []:
            frame = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def _dispatch(self) -> None:
        url = urlsplit(self.path)
        raw = self._body()
        if url.path.startswith("/_mock/"):
            return self._control(url, raw)
        prefix = next((p for p in _VENDORS if url.path == p or url.path.startswith(p + "/")), None)
        if prefix is None:
            return self._send_json(404, {"error": f"no mock route for {url.path}"})
        vendor, handler = _VENDORS[prefix]
        headers = {name.lower(): value for name, value in self.headers.items()}
        reply = handler(self.server.state, self.command, url.path[len(prefix) :], headers, raw)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        self.server.state.record(
            {
                "vendor": vendor,
                "method": self.command,
                "path": url.path,
                "headers": headers,
                "body": body,
                "status": reply.status,
                "error": reply.body if reply.status >= 400 else None,
            }
        )
        if reply.events is not None:
            return self._send_events(reply)
        return self._send_json(reply.status, reply.body, reply.headers)

    def _control(self, url, raw: bytes) -> None:
        state = self.server.state
        route = (self.command, url.path)
        try:
            document = json.loads(raw) if raw else None
            if route == ("GET", "/_mock/health"):
                return self._send_json(200, {"ok": True})
            if route == ("GET", "/_mock/state"):
                return self._send_json(200, {"keys": state.keys, "models": state.models, "script": state.script})
            if route == ("PUT", "/_mock/keys"):
                state.configure(keys=document)
            elif route == ("PUT", "/_mock/models"):
                state.configure(models=document)
            elif route == ("PUT", "/_mock/script"):
                state.configure(script=document)
            elif route == ("GET", "/_mock/requests"):
                vendor = parse_qs(url.query).get("vendor", [None])[0]
                with state.lock:
                    entries = [e for e in state.requests if vendor in (None, e["vendor"])]
                return self._send_json(200, {"requests": entries})
            elif route == ("DELETE", "/_mock/requests"):
                state.reset_requests()
            else:
                return self._send_json(404, {"error": f"no control route {self.command} {url.path}"})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        return self._send_json(200, {"ok": True})

    do_GET = do_POST = do_PUT = do_DELETE = _dispatch


class ModelsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state: MockState) -> None:
        self.state = state
        super().__init__(address, _Handler)


def serve(port: int, state_dir: Path | None, host: str = "127.0.0.1") -> ModelsServer:
    """Bind (port 0 picks a free one) with the specs loaded, so the first request is fast."""
    schemas.openai_spec()
    schemas.anthropic_schema()
    return ModelsServer((host, port), MockState(state_dir))
