"""Session routes: list, detail, export, human messages, and the live SSE event tail."""

from __future__ import annotations

import json
import re
from functools import partial
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from ..admin_auth import Caller
from ..sessions import EV_STATE
from .auth import Authenticator
from .control import Control, check_sid, etag_of
from .errors import invalid

_STREAM_ID = re.compile(r"\d{1,20}-\d{1,20}\Z")


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    mode: Literal["auto", "steer", "follow_up"] = "auto"


class InterruptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, Field(max_length=1000)] = ""


class EventStreamResponse(StreamingResponse):
    """Always races the stream against `http.disconnect`, whatever ASGI version the server
    reports, so a client that goes away cancels the blocking XREAD at once instead of at the
    next ping write."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as group:

            async def run(func) -> None:
                await func()
                group.cancel_scope.cancel()

            group.start_soon(run, partial(self.stream_response, send))
            await run(partial(self.listen_for_disconnect, receive))


def not_modified(request: Request, etag: str) -> bool:
    candidates = request.headers.get("if-none-match", "")
    return any(tag.strip() in (etag, "*") for tag in candidates.split(",")) if candidates else False


def json_with_etag(request: Request, document) -> Response:
    etag = etag_of(document)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if not_modified(request, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(document, headers=headers)


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/sessions")
    viewer = Depends(auth.dependency("viewer"))
    operator = Depends(auth.dependency("operator"))
    services = control.s

    @router.get("")
    def list_sessions(
        request: Request,
        state: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        _: Caller = viewer,
    ):
        states = [item for item in state.split(",") if item] if state else None
        return json_with_etag(request, {"items": control.list_sessions(states, limit)})

    @router.get("/{sid}")
    def get_session(
        sid: str,
        transcript_limit: Annotated[int, Query(ge=0, le=4000)] = 4000,
        _: Caller = viewer,
    ):
        return control.get_session(sid, transcript_limit)

    @router.get("/{sid}/export.jsonl")
    def export(sid: str, _: Caller = viewer):
        return PlainTextResponse(
            control.export_jsonl(sid),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{_filename(sid)}.jsonl"'},
        )

    @router.post("/{sid}/messages", status_code=202)
    def message(sid: str, body: MessageBody, caller: Caller = operator):
        return control.send_message(caller, sid, body.text, body.mode)

    @router.post("/{sid}/interrupt", status_code=202)
    def interrupt(sid: str, body: InterruptBody, caller: Caller = operator):
        return control.interrupt(caller, sid, body.reason)

    @router.get("/{sid}/events")
    async def events(
        sid: str,
        request: Request,
        after: Annotated[str | None, Query(max_length=41)] = None,
        _: Caller = viewer,
    ):
        """SSE tail of the session's event stream.

        Resumes after `Last-Event-ID` (or `?after=`, for clients that cannot set headers on the
        first connect; `0-0` replays the whole retained stream). Without either it starts at
        the current end: a new viewer replays the transcript from `GET /v1/sessions/{sid}` and
        tails from its `last_event_id`. Delivery is a blocking XREAD; with nothing new for
        `ping_interval_s` a `: ping` comment keeps proxies from closing the connection. The
        stream ends after the session's `ended` state event.
        """
        check_sid(sid)
        aclient = services.aclient
        key = services.sessions.events_key(sid)
        await run_in_threadpool(control.meta, sid)  # the uniform 404 for an unknown session
        cursor = request.headers.get("last-event-id") or after
        if cursor is not None and not _STREAM_ID.fullmatch(cursor):
            raise invalid("Last-Event-ID must be a stream id like 1700000000000-0")
        if cursor is None:
            newest = await aclient.xrevrange(key, count=1)
            cursor = newest[0][0] if newest else "0-0"
        block_ms = max(1, int(services.ping_interval_s * 1000))

        async def stream():
            position = cursor
            yield "retry: 1000\n\n"
            while True:
                result = await aclient.xread({key: position}, count=500, block=block_ms)
                if not result:
                    yield ": ping\n\n"
                    continue
                chunk = []
                ended = False
                for event_id, fields in result[0][1]:
                    position = event_id
                    kind = fields.get("kind", "message")
                    data = fields.get("data", "{}")
                    chunk.append(f"id: {event_id}\nevent: {kind}\ndata: {data}\n\n")
                    if kind == EV_STATE and json.loads(data).get("state") == "ended":
                        ended = True
                yield "".join(chunk)
                if ended:
                    return

        return EventStreamResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def _filename(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", sid)
