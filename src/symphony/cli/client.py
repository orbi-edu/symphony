"""HTTP client for the control API `/v1` (docs/api.md) and its SSE event stream."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import quote

import httpx

from symphony.cli.creds import Credential

USER_AGENT = "symphony-cli/0.1"
OVERVIEW = "/v1/overview"

# What this process learned from the API, for the update check after the command
# (symphony/cli/selfupdate.py): the credential of the last answered request, and the version
# the server runs when an overview was read.
seen: dict = {"cred": None, "version": None}

# Reason lengths the API enforces, checked here first so a person gets a clear message before
# anything is sent: (minimum, maximum, minimum counted after trimming spaces).
REASON_RULES = {
    # board.CommandBody (raw 8-1000) + Control.command (8-1000 after strip)
    "command": (8, 1000, True),
    "config": (1, 500, False),     # admin.ConfigBody / TeamConfig.publish
    "interrupt": (0, 1000, False),  # sessions.InterruptBody
    "merger": (1, 1000, True),     # merger.ActionBody (raw 1-1000) + Control.merger_action (strip)
}


def reason_problem(kind: str, reason: str) -> str | None:
    """The problem with `reason` for an API body of `kind`, or None when it is valid."""
    low, high, trimmed = REASON_RULES[kind]
    size = len(reason.strip()) if trimmed else len(reason)
    if size < low:
        return f"The reason needs at least {low} characters (it has {size})."
    if len(reason) > high:
        return f"The reason can have at most {high} characters (it has {len(reason)})."
    return None


class ApiError(Exception):
    """A non-2xx answer (or no answer at all: status 0) with the API's `{error:{code,message}}`."""

    def __init__(self, status: int, code: str, message: str, body: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.body = body if body is not None else {"error": {"code": code, "message": message}}


@dataclass
class Response:
    status: int
    body: object
    headers: httpx.Headers


def sid_path(sid: str) -> str:
    """Session ids carry `:` (`<issue_id>:<seq>`, `merger:<pr>:<role>:<n>`)."""
    return quote(sid, safe="")


def _decode(resp: httpx.Response) -> object:
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}


def _error_from(resp: httpx.Response, body: object) -> ApiError:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return ApiError(resp.status_code, str(err.get("code", resp.status_code)),
                        str(err.get("message", "")), body)
    message = resp.reason_phrase or f"HTTP {resp.status_code}"
    return ApiError(resp.status_code, str(resp.status_code), message,
                    {"error": {"code": str(resp.status_code), "message": message}})


class Api:
    def __init__(self, cred: Credential, *, timeout: float = 15.0) -> None:
        self.cred = cred
        self._http = httpx.Client(
            base_url=cred.url,
            headers={"Authorization": f"Bearer {cred.token}", "User-Agent": USER_AGENT},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def raw(self, method: str, path: str, *, json_body: object = None,
            headers: dict[str, str] | None = None, params: dict | None = None) -> Response:
        """One request; raises `ApiError` for non-2xx answers and for an unreachable server."""
        try:
            resp = self._http.request(
                method.upper(), path, json=json_body, headers=headers,
                params={k: v for k, v in (params or {}).items() if v is not None},
            )
        except httpx.HTTPError as exc:
            raise ApiError(0, "unreachable", f"Cannot connect to {self.cred.url}.") from exc
        body = _decode(resp)
        if resp.status_code >= 300:
            raise _error_from(resp, body)
        seen["cred"] = self.cred
        if path.split("?")[0] == OVERVIEW and isinstance(body, dict) and isinstance(body.get("version"), str):
            seen["version"] = body["version"]
        return Response(resp.status_code, body, resp.headers)

    def get(self, path: str, **params) -> object:
        return self.raw("GET", path, params=params).body

    def post(self, path: str, body: object, headers: dict[str, str] | None = None) -> object:
        return self.raw("POST", path, json_body=body, headers=headers).body

    # -- typed helpers ---------------------------------------------------
    def me(self) -> dict:
        return self.get("/v1/me")

    def session(self, sid: str) -> dict:
        return self.get(f"/v1/sessions/{sid_path(sid)}")

    def sessions(self, *, state: str | None = None, limit: int = 50) -> list[dict]:
        return self.get("/v1/sessions", state=state, limit=limit).get("items", [])

    def send_message(self, sid: str, text: str, mode: str = "auto") -> dict:
        return self.post(f"/v1/sessions/{sid_path(sid)}/messages", {"text": text, "mode": mode})

    def interrupt(self, sid: str, reason: str) -> dict:
        return self.post(f"/v1/sessions/{sid_path(sid)}/interrupt", {"reason": reason})

    def command(self, action: str, target: str | None, reason: str) -> dict:
        body = {"action": action, "reason": reason}
        if target:
            body["target"] = target
        return self.post("/v1/commands", body)

    def export(self, sid: str) -> bytes:
        try:
            resp = self._http.get(f"/v1/sessions/{sid_path(sid)}/export.jsonl")
        except httpx.HTTPError as exc:
            raise ApiError(0, "unreachable", f"Cannot connect to {self.cred.url}.") from exc
        if resp.status_code >= 300:
            raise _error_from(resp, _decode(resp))
        return resp.content

    # Backups can be large and slow: no read timeout, and the body streams to or from a file.
    _LONG = httpx.Timeout(15.0, read=None, write=None)

    def download(self, path: str, dest: BinaryIO) -> httpx.Headers:
        """Stream a GET body into `dest`; returns the response headers."""
        try:
            with self._http.stream("GET", path, timeout=self._LONG) as resp:
                if resp.status_code >= 300:
                    resp.read()
                    raise _error_from(resp, _decode(resp))
                for chunk in resp.iter_bytes(1024 * 1024):
                    dest.write(chunk)
                return resp.headers
        except httpx.HTTPError as exc:
            raise ApiError(0, "unreachable", f"Cannot connect to {self.cred.url}.") from exc

    def upload(self, path: str, source: BinaryIO, params: dict | None = None) -> object:
        """POST a file as the request body (`application/gzip`), streamed."""
        size = source.seek(0, 2)
        source.seek(0)

        def chunks() -> Iterator[bytes]:
            while chunk := source.read(1024 * 1024):
                yield chunk

        try:
            resp = self._http.post(
                path, content=chunks(), timeout=self._LONG,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers={"Content-Type": "application/gzip", "Content-Length": str(size)},
            )
        except httpx.HTTPError as exc:
            raise ApiError(0, "unreachable", f"Cannot connect to {self.cred.url}.") from exc
        body = _decode(resp)
        if resp.status_code >= 300:
            raise _error_from(resp, body)
        return body

    def resolve(self, ref: str) -> dict:
        """A session meta from a session id or a ticket identifier (`ENG-12`): a ticket resolves
        to its newest live session, else its newest one."""
        if ":" in ref:
            return self.session(ref)["meta"]
        wanted = ref.upper()
        matches = [m for m in self.sessions(limit=200) if str(m.get("identifier", "")).upper() == wanted]
        if not matches:
            raise ApiError(404, "no_session", f"No session for {ref}.")
        live = [m for m in matches if m.get("state") != "ended"]
        return (live or matches)[0]


@dataclass(frozen=True)
class SseEvent:
    id: str | None
    kind: str
    data: dict


def parse_sse(lines: Iterator[str]) -> Iterator[SseEvent]:
    """Server-sent events from text lines; comments (`: ping`) are skipped."""
    eid: str | None = None
    kind = "message"
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                raw = "\n".join(data)
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"text": raw}
                yield SseEvent(eid, kind, payload if isinstance(payload, dict) else {"value": payload})
            eid, kind, data = None, "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            eid = value
        elif field == "event":
            kind = value
        elif field == "data":
            data.append(value)


class EventTail:
    """Follows `GET /v1/sessions/{sid}/events` from `after`, reconnecting with `Last-Event-ID`
    when the connection drops. The server pings every 15 s, so a read idle for 45 s is a dead
    connection. One instance is one connection; `stop()` closes it from any thread."""

    READ_TIMEOUT = 45.0
    MAX_BACKOFF = 5.0

    def __init__(self, cred: Credential, sid: str, after: str | None,
                 *, on_error: Callable[[Exception], None] | None = None) -> None:
        self.cred = cred
        self.sid = sid
        self.last_id = after
        self._stop = threading.Event()
        self._on_error = on_error
        self._resp: httpx.Response | None = None
        self._http = httpx.Client(
            base_url=cred.url,
            headers={"Authorization": f"Bearer {cred.token}", "User-Agent": USER_AGENT,
                     "Accept": "text/event-stream"},
            timeout=httpx.Timeout(10.0, read=self.READ_TIMEOUT),
        )

    def stop(self) -> None:
        self._stop.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - closing from another thread may race the reader
                pass

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def __iter__(self) -> Iterator[SseEvent]:
        backoff = 0.1
        path = f"/v1/sessions/{sid_path(self.sid)}/events"
        try:
            while not self._stop.is_set():
                headers = {"Last-Event-ID": self.last_id} if self.last_id else {}
                try:
                    with self._http.stream("GET", path, headers=headers) as resp:
                        self._resp = resp
                        if resp.status_code >= 300:
                            resp.read()
                            err = _error_from(resp, _decode(resp))
                            if resp.status_code in (401, 403, 404):
                                raise err
                            raise httpx.HTTPError(f"{resp.status_code} {err.message}")
                        backoff = 0.1
                        for ev in parse_sse(resp.iter_lines()):
                            if ev.id:
                                self.last_id = ev.id
                            yield ev
                            # The API closes the stream after the `ended` state; so does the tail.
                            if ev.kind == "symphony.state" and ev.data.get("state") == "ended":
                                return
                except ApiError:
                    raise
                except (httpx.HTTPError, httpx.StreamError, OSError, RuntimeError) as exc:
                    if self._stop.is_set():
                        return
                    if self._on_error is not None:
                        self._on_error(exc)
                finally:
                    self._resp = None
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.MAX_BACKOFF)
        finally:
            self._http.close()
