"""Sandbox gateway — the only Symphony service a workspace pod can reach (internals §9, port 8090).

Two routes:

- `POST /v1/traces`, the engine's native OTLP/HTTP exporter. It authorizes with the
  attempt's own bearer (the entry's `bridge_token`, rotated per dispatch and cleared when the
  attempt ends) and fences on the attempt's `dispatch_seq`, so a sandbox from a superseded
  dispatch — which still holds a once-valid token — cannot contaminate the current rollup.
  Every rejection is a silent drop by design: the exporter runs on a background batch
  processor and swallows its own failures, so nothing this route does can change an
  attempt's outcome.
- `/v1/mcp/<name>[/<path>]`, any method: the MCP proxy (`mcp_servers`). The bearer must be
  a live MCP token whose role the enabled server allows; anything else is 403
  `{"error": "mcp_forbidden"}`, without saying which check failed. The gateway drops the
  agent's `Authorization`, adds the server's stored headers, and streams both ways.
- `/v1/mcp/<name>/http/<path>`, any method: the server's companion HTTP API (`http_base`),
  with the same checks and header swap. Uploads stream through. Viewer routes (`view/…`) are
  never proxied: they take no token.

For a server with `http_base`, JSON and text answers of both routes have the links of
`http_base` and `rewrite_origins` pointed at the gateway (bodies up to 8 MiB). For a server
with `cleanup`, the MCP proxy records the ids that `acquire_tool` returns under the session
token and forgets those that `release_tool` releases. For a preset with `builds_path` (Seiji
`/v1/apps`), the companion route records the build id of each successful upload under the
session token, for the session's cleanup to delete. A background sweep releases what
sessions with an expired token still hold.

No model or tracker credential is served here. Models are reached by API key staged
privately per attempt (internals §13), and `linear_graphql` is a runner host tool
(internals §3). The only credentials the gateway holds are the MCP servers' stored headers,
and it sends them only to their own server.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import threading
from collections.abc import AsyncIterator, Callable

import anyio
import httpx
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import mcp_servers, state, telemetry

_log = structlog.get_logger()
_FORBIDDEN = {"error": "mcp_forbidden"}
_METHODS = ["GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"]
SWEEP_INTERVAL_S = 60.0

# What a conforming OTLP client reads as "delivered, nothing rejected", in the encoding it
# sent: an empty ExportTraceServiceResponse.
_ACCEPTED = {telemetry.PROTOBUF: b"", telemetry.JSON: b'{"partialSuccess":{}}'}


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def _authorized(entry: dict | None, presented: str | None) -> bool:
    """Constant time, so a wrong token leaks nothing through timing."""
    expected = (entry or {}).get("bridge_token")
    return bool(
        entry
        and isinstance(expected, str)
        and expected
        and presented
        and hmac.compare_digest(expected, presented)
    )


class _Proxied(StreamingResponse):
    """Streams the upstream body and stops (closing the upstream) as soon as the client
    disconnects, whatever the server's ASGI version."""

    async def __call__(self, scope, receive, send) -> None:
        async with anyio.create_task_group() as group:

            async def stream() -> None:
                try:
                    await self.stream_response(send)
                except OSError:
                    pass
                group.cancel_scope.cancel()

            group.start_soon(stream)
            await self.listen_for_disconnect(receive)
            group.cancel_scope.cancel()


def build_app(
    kv_provider: Callable[[], state.KV],
    *,
    namespace: str | None = None,
    mcp: mcp_servers.McpRepository | None = None,
    sweep_interval_s: float | None = SWEEP_INTERVAL_S,
) -> FastAPI:
    upstream_client: list[httpx.AsyncClient] = []

    def redis_client():
        return getattr(kv_provider(), "client", None)

    def sweep_once() -> None:
        """One pass of the held-resource sweep, by one gateway replica at a time."""
        redis = redis_client()
        if mcp is None or redis is None:
            return
        lock = f"sym:{namespace}:mcp:sweep-lock"
        if not redis.set(lock, "1", nx=True, ex=max(1, int((sweep_interval_s or 60) * 0.9))):
            return
        mcp_servers.sweep_held(redis, namespace, mcp)

    stop_sweep = threading.Event()

    def sweeper() -> None:
        """Its own thread, so it runs whatever lifespan the server gives the app."""
        while not stop_sweep.wait(sweep_interval_s):
            try:
                sweep_once()
            except Exception as exc:  # noqa: BLE001 — the next pass tries again
                _log.warning("mcp_sweep_failed", error=type(exc).__name__)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            stop_sweep.set()
            if upstream_client:
                await upstream_client.pop().aclose()

    api = FastAPI(title="symphony sandbox gateway", lifespan=lifespan)
    api.state.stop_sweep = stop_sweep
    if namespace is None:
        from .edge import pod_namespace

        namespace = pod_namespace()
    if sweep_interval_s and mcp is not None:
        threading.Thread(target=sweeper, name="mcp-sweep", daemon=True).start()

    def client() -> httpx.AsyncClient:
        if not upstream_client:
            upstream_client.append(mcp_servers.http_client())
        return upstream_client[0]

    def authorize(name: str, token: str | None) -> tuple[dict, dict, dict] | None:
        """(definition, stored headers, token record) when every check passes, else None."""
        if mcp is None:
            return None
        redis = redis_client()
        if redis is None:
            return None
        record = mcp_servers.token_record(redis, namespace, token)
        if record is None:
            return None
        definition = mcp.get(name)
        role = record.get("role")
        if (
            definition is None
            or not definition.get("enabled", True)
            or definition.get("transport") not in mcp_servers.REMOTE
            or role not in definition.get("roles", ())
        ):
            return None
        mcp_servers.count_call(redis, namespace, name, role)
        return definition, mcp.headers(name), record

    async def allowed_for(request: Request, name: str):
        try:
            return await run_in_threadpool(authorize, name, _bearer(request))
        except Exception:  # noqa: BLE001 — an unreadable store or Redis never opens the door
            return None

    def http_url(request: Request, name: str) -> str:
        """`{http_url}` as this agent reaches the gateway."""
        return f"{str(request.base_url).rstrip('/')}/v1/mcp/{name}/http"

    def observe(token: str, record: dict, name: str, cleanup: dict, sent, answer) -> None:
        found = mcp_servers.observe_call(sent, answer, cleanup)
        if found is None:
            return
        kind, lease_id, extra = found
        redis = redis_client()
        if kind == "acquired":
            mcp_servers.record_held(redis, namespace, token, record.get("session"), name,
                                    lease_id, extra)
        else:
            mcp_servers.forget_held(redis, namespace, token, name, lease_id)

    async def observed(token, record, name, cleanup, sent, answer) -> None:
        try:
            await run_in_threadpool(observe, token, record, name, cleanup, sent, answer)
        except Exception as exc:  # noqa: BLE001 — the answer still reaches the agent
            _log.warning("mcp_lease_record_failed", server=name, error=type(exc).__name__)

    def record_build(token: str, record: dict, name: str, preset, answer) -> None:
        build_id = mcp_servers.uploaded_build(preset, answer)
        if build_id is not None:
            mcp_servers.record_build(redis_client(), namespace, token, record.get("session"),
                                     name, build_id)

    async def recorded_build(token, record, name, preset, answer) -> None:
        try:
            await run_in_threadpool(record_build, token, record, name, preset, answer)
        except Exception as exc:  # noqa: BLE001 — the answer still reaches the agent
            _log.warning("mcp_build_record_failed", server=name, error=type(exc).__name__)

    def relayed(upstream: httpx.Response, body: AsyncIterator[bytes], headers: dict,
                ) -> _Proxied:
        async def relay():
            try:
                async for chunk in body:
                    yield chunk
            finally:
                await upstream.aclose()

        return _Proxied(relay(), status_code=upstream.status_code, headers=headers)

    async def buffered_or_streamed(upstream: httpx.Response, headers: dict, rewrite,
                                   on_message=None):
        """A JSON or text answer: rewritten (and handed to `on_message`) when it fits the
        rewrite limit, else streamed unchanged."""
        try:
            head, rest = await mcp_servers.read_limited(upstream)
        except BaseException:
            await upstream.aclose()
            raise
        if rest is not None:

            async def chained():
                if head:
                    yield head
                async for chunk in rest:
                    yield chunk

            return relayed(upstream, chained(), headers)
        await upstream.aclose()
        if on_message is not None:
            try:
                message = json.loads(head)
            except ValueError:
                message = None
            await on_message(message)
        body = mcp_servers.rewrite_bytes(head, rewrite) if rewrite else head
        return Response(content=body, status_code=upstream.status_code, headers=headers)

    async def proxy(request: Request, name: str, rest: str = ""):
        allowed = await allowed_for(request, name)
        if allowed is None:
            return JSONResponse(status_code=403, content=_FORBIDDEN)
        return await forward_mcp(request, name, rest, allowed)

    async def forward_mcp(request: Request, name: str, rest: str, allowed):
        definition, stored, record = allowed
        token = _bearer(request)
        has_body = request.method not in ("GET", "HEAD", "DELETE", "OPTIONS")
        cleanup = definition.get("cleanup") if definition["transport"] == "http" else None
        rewrite = mcp_servers.link_rewriter(definition, http_url(request, name))
        sent = None
        content = request.stream() if has_body else None
        declared = request.headers.get("content-length") or ""
        if (cleanup and request.method == "POST" and not rest
                and (not declared or (declared.isdigit() and int(declared) <= mcp_servers.REWRITE_LIMIT))):
            # A JSON-RPC request is small; reading it tells which tool the answer belongs to.
            content = await request.body()
            with contextlib.suppress(ValueError):
                sent = json.loads(content)
        try:
            upstream = await mcp_servers.open_upstream(
                client(), definition, stored, request.method, rest=rest,
                query=request.url.query, incoming=request.headers, content=content,
            )
        except httpx.HTTPError:
            return JSONResponse(status_code=502, content={"error": "mcp_unreachable"})
        headers = mcp_servers.response_headers(upstream)
        media = upstream.headers.get("content-type", "").split(";")[0].strip().lower()
        watching = isinstance(sent, dict) and sent.get("method") == "tools/call"

        async def on_message(message) -> None:
            if watching and upstream.status_code < 300:
                await observed(token, record, name, cleanup, sent, message)

        if definition["transport"] == "sse" and media == "text/event-stream":
            body = mcp_servers.sse_rewritten(upstream.aiter_lines(), definition, name)
        elif media == "text/event-stream" and (rewrite or watching):
            body = sse_events(upstream, rewrite, on_message)
        elif (rewrite or watching) and mcp_servers.rewritable(upstream):
            return await buffered_or_streamed(upstream, headers, rewrite, on_message)
        else:
            body = upstream.aiter_raw()
        return relayed(upstream, body, headers)

    async def sse_events(upstream: httpx.Response, rewrite, on_message) -> AsyncIterator[bytes]:
        """Streamable HTTP's SSE answer, event by event: links rewritten, messages observed."""
        lines: list[str] = []
        async for line in upstream.aiter_lines():
            if line:
                lines.append(line)
                continue
            yield await _event(lines, rewrite, on_message)
            lines = []
        if lines:
            yield await _event(lines, rewrite, on_message)

    async def _event(lines: list[str], rewrite, on_message) -> bytes:
        data = "\n".join(line.split(":", 1)[1].lstrip(" ") if ":" in line else ""
                         for line in lines if line.split(":", 1)[0] == "data")
        if data:
            with contextlib.suppress(ValueError):
                await on_message(json.loads(data))
        text = "\n".join(lines)
        return ((rewrite(text) if rewrite else text) + "\n\n").encode()

    async def http_api(request: Request, name: str, path: str):
        allowed = await allowed_for(request, name)
        if allowed is None:
            return JSONResponse(status_code=403, content=_FORBIDDEN)
        definition, stored, record = allowed
        if not definition.get("http_base"):
            return await forward_mcp(request, name, f"http/{path}", allowed)
        target = mcp_servers.http_api_path(path)
        if target is None:
            return JSONResponse(status_code=404, content={"error": "mcp_not_proxied"})
        has_body = request.method not in ("GET", "HEAD", "DELETE", "OPTIONS")
        try:
            upstream = await mcp_servers.open_http_api(
                client(), definition, stored, request.method, target, request.url.query,
                request.headers, content=request.stream() if has_body else None,
            )
        except httpx.HTTPError:
            return JSONResponse(status_code=502, content={"error": "mcp_unreachable"})
        headers = mcp_servers.response_headers(upstream)
        rewrite = mcp_servers.link_rewriter(definition, http_url(request, name))
        preset = mcp_servers.preset_of(definition)
        uploading = (request.method == "POST" and preset is not None and preset.builds_path
                     and target == preset.builds_path.strip("/"))

        async def on_answer(message) -> None:
            # A build the session uploaded: its cleanup deletes it (internals §18).
            if 200 <= upstream.status_code < 300:
                await recorded_build(_bearer(request), record, name, preset, message)

        if rewrite and mcp_servers.rewritable(upstream):
            return await buffered_or_streamed(upstream, headers, rewrite,
                                              on_answer if uploading else None)
        length = upstream.headers.get("content-length")
        if length is not None and request.method != "HEAD":
            headers["content-length"] = length  # untouched bytes keep their length
        return relayed(upstream, upstream.aiter_raw(), headers)

    api.add_api_route("/v1/mcp/{name}", proxy, methods=_METHODS)
    api.add_api_route("/v1/mcp/{name}/http/{path:path}", http_api, methods=_METHODS)
    api.add_api_route("/v1/mcp/{name}/{rest:path}", proxy, methods=_METHODS)

    @api.post("/v1/traces")
    async def traces(request: Request):
        media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        payload = telemetry.decode_export(await request.body(), media)
        if payload is None:
            return JSONResponse(status_code=400, content={"error": "invalid request body"})

        # Who does this batch claim to be? Resource attributes are the only identification
        # available: OTLP carries no place for ours, and inferring the attempt from the token
        # alone would file a superseded sandbox's spans under the current attempt.
        issue_id, seq = telemetry.attempt_attrs(payload)
        if not issue_id:
            return JSONResponse(status_code=400, content={"error": "unattributed payload"})

        try:
            kv = kv_provider()
            entry = kv.get(state.issue_key(issue_id))
        except Exception:  # noqa: BLE001 — unavailable state never becomes an empty 200
            return JSONResponse(status_code=503, content={"error": "state unavailable"})

        # 1. authorization. The token is cleared when the attempt finishes, so spans
        #    arriving late from a finished run are refused rather than merged into a stale
        #    rollup.
        if not _authorized(entry, _bearer(request)):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

        # 2. fencing — a sandbox from a superseded dispatch still holds a once-valid token
        if seq is None or entry.get("dispatch_seq") != seq:
            return JSONResponse(
                status_code=409, content={"error": "attempt no longer owns this issue"}
            )

        try:
            key = state.telemetry_key(issue_id)
            kv.put(key, telemetry.fold_export(kv.get(key), payload))
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"error": "state unavailable"})
        return Response(content=_ACCEPTED[media], media_type=media)

    return api
