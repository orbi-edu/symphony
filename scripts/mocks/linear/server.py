"""Linear mock (internals §14): Linear's GraphQL API on its published schema, file uploads,
signed webhooks and a control API for the certification script.

    uv run python -m scripts.mocks.linear --port 4100 --state DIR \
        [--team ENG:Engineering ...] [--api-key KEY ...] \
        [--webhook-url URL --webhook-secret SECRET] [--public-url URL]

GraphQL (`POST /graphql` JSON, or `GET /graphql?query=…`; mutations only over POST):

- Every request is parsed and validated against `scripts/mocks/specs/linear.graphql`
  (linear/linear `packages/sdk/src/schema.graphql`) and executed by graphql-core, so unknown
  fields, arguments, types and wrong variable types fail like Linear: HTTP 400 and
  `errors[].extensions.{type, code, statusCode, userError, userPresentableMessage}`.
- Auth as documented: `Authorization: <personal API key>`. A missing or unknown key, or an
  `Authorization: Bearer …` OAuth token (the mock issues none), gets HTTP 401
  `AUTHENTICATION_ERROR` "Authentication required, not authenticated".
- Apollo CSRF prevention as Linear runs it: a "simple" request (no JSON content type and no
  `x-apollo-operation-name`/`apollo-require-preflight`) is refused with HTTP 400 BAD_REQUEST.
- Rate limits as documented: `X-RateLimit-Requests-{Limit,Remaining,Reset}` (2,500/hour per
  user), `X-Complexity` and `X-RateLimit-Complexity-{Limit,Remaining,Reset}` (3,000,000/hour);
  a single query above 10,000 points is refused; an exhausted bucket answers HTTP 429 with
  `RATELIMITED` and `Retry-After`. `POST /_mock/ratelimit` drains or refills the buckets.
- Served root fields: see `graph.ROOTS`. Anything the mock does not model (a field, filter,
  argument or input field) answers `MOCK_NOT_IMPLEMENTED` with HTTP 501 — never invented data.

Uploads (`fileUpload`): a signed `uploadUrl` (`PUT /_uploads/<id>?Expires=…&Signature=…`,
valid one minute) that requires `Content-Type` = the requested type, the returned headers and
exactly `size` bytes (403/400 otherwise, as the storage does); the `assetUrl`
(`GET /uploads/<org>/<id>/<key>`) needs the same `Authorization` as the API unless
`makePublic`.

Webhooks: Issue, Comment, Attachment and Document create/update/remove events POST to the
configured URL with Linear's payload (`action, actor, createdAt, data, type, url, updatedFrom,
organizationId, webhookTimestamp, webhookId`) and headers (`Linear-Delivery`, `Linear-Event`,
`Linear-Signature` = HMAC-SHA256 hex of the raw body with the secret, `Linear-Timestamp`,
`User-Agent: Linear-Webhook`). One attempt each (Linear retries after 1 min/1 h/6 h; the mock
records the failure instead).

Control (`/_mock/*`, JSON, unauthenticated): `GET health|state|issues|issues/<ref>|requests|
uploads|webhooks`; `POST reset`; `POST issues` `{title, team?, state?, labels?, description?,
priority?, parent?, assignee?, project?, as?}`; `POST issues/<ref>/state` `{state, as?}`;
`POST issues/<ref>/comments` `{body, as?, parent?}`; `POST issues/<ref>/labels`
`{add?, remove?, as?}`; `POST labels` `{name, team?, color?}`; `POST teams` `{key, name}`;
`POST users` `{name, email, displayName?, apiKey?}`; `POST api-keys` `{key, user?}`;
`PUT webhooks` `{url, secret, resourceTypes?}`; `POST webhooks/flush`; `POST ratelimit`
`{remaining?, limit?, complexityRemaining?}`. `as` names a user by email, display name or id
(default: the human, `ada@example.com`).

State is persisted to `<state>/linear.json` (+ `<state>/linear-uploads/`) after every change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from . import graph
from .store import (
    DEFAULT_API_KEY,
    DEFAULT_TEAMS,
    WEBHOOK_TYPES,
    LinearError,
    Store,
    iso_ms,
)

REQUESTS_PER_HOUR = 2_500
COMPLEXITY_PER_HOUR = 3_000_000
UPLOAD_WINDOW_S = 60  # "The request must start within one minute of generating the URL."
WEBHOOK_TIMEOUT_S = 5  # Linear gives a consumer five seconds
CSRF_MESSAGE = (
    "This operation has been blocked as a potential Cross-Site Request Forgery (CSRF). Please "
    "either specify a 'content-type' header (with a type that is not one of "
    "application/x-www-form-urlencoded, multipart/form-data, text/plain) or provide a non-empty "
    "value for one of the following headers: x-apollo-operation-name, apollo-require-preflight"
)
SIMPLE_TYPES = ("", "text/plain", "application/x-www-form-urlencoded", "multipart/form-data")


def _det_uuid(*parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _signing_key(store: Store) -> bytes:
    return hashlib.sha256(f"upload:{store.seed}:{store.org['id']}".encode()).digest()


def _signature(store: Store, file_id: str, expires: int) -> str:
    return hmac.new(_signing_key(store), f"{file_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def _upload_headers(record: dict) -> list[dict]:
    name = record["originalName"].replace('"', "")
    return [{"key": "Content-Disposition",
             "value": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"},
            {"key": "x-goog-content-length-range", "value": f"{record['size']},{record['size']}"}]


def upload_file(store: Store, record: dict) -> dict:
    """The `UploadFile` value of a `fileUpload` payload."""
    expires = record["createdMs"] // 1000 + UPLOAD_WINDOW_S
    sig = _signature(store, record["fileId"], expires)
    return {
        "uploadUrl": f"{store.public_url}/_uploads/{record['fileId']}?Expires={expires}&Signature={sig}",
        "assetUrl": f"{store.public_url}/uploads/{record['path']}",
        "contentType": record["contentType"], "filename": record["path"],
        "size": record["size"], "headers": _upload_headers(record),
        "metaData": record["metaData"]}


def _error_body(err: LinearError) -> dict:
    return {"errors": [{"message": err.message, "extensions": err.extensions()}]}


AUTH_ERROR = LinearError("Authentication required, not authenticated", type="authentication error",
                         code="AUTHENTICATION_ERROR", status=401,
                         presentable="You need to authenticate to access this operation.")


class Webhooks:
    """One ordered delivery thread; every attempt is recorded."""

    def __init__(self, mock: LinearMock) -> None:
        self.mock = mock
        self.deliveries: list[dict] = []
        self._queue: queue.Queue[dict] = queue.Queue()
        self._idle = threading.Condition()
        self._pending = 0
        threading.Thread(target=self._run, name="linear-webhooks", daemon=True).start()

    @property
    def config(self) -> dict | None:
        return self.mock.store.data.get("webhook")

    def enqueue(self, events: list[dict]) -> None:
        cfg = self.config
        if not cfg or not cfg.get("enabled"):
            return
        for event in events:
            if event["type"] in cfg["resourceTypes"]:
                with self._idle:
                    self._pending += 1
                self._queue.put(event)

    def flush(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._idle.wait(left)
        return True

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                self._deliver(event)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def _deliver(self, event: dict) -> None:
        cfg = self.config
        store = self.mock.store
        if not cfg:
            return
        sent_ms = int(time.time() * 1000)
        payload = {"action": event["action"], "actor": event["actor"],
                   "createdAt": event["createdAt"], "data": event["data"], "type": event["type"],
                   "url": event["url"]}
        if "updatedFrom" in event:
            payload["updatedFrom"] = event["updatedFrom"]
        payload.update(organizationId=store.data["orgId"], webhookTimestamp=sent_ms,
                       webhookId=cfg["id"])
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        delivery = _det_uuid(store.seed, "delivery", str(len(self.deliveries) + 1))
        headers = {
            "Accept-Charset": "utf-8", "Content-Type": "application/json; charset=utf-8",
            "Linear-Delivery": delivery, "Linear-Event": event["type"],
            "Linear-Signature": hmac.new(cfg["secret"].encode(), body, hashlib.sha256).hexdigest(),
            "Linear-Timestamp": str(sent_ms), "User-Agent": "Linear-Webhook"}
        record = {"deliveryId": delivery, "type": event["type"], "action": event["action"],
                  "url": cfg["url"], "sentAt": iso_ms(sent_ms), "status": None, "error": None,
                  "payload": payload}
        try:
            req = urllib.request.Request(cfg["url"], data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_S) as resp:
                record["status"] = resp.status
        except urllib.error.HTTPError as exc:
            record["status"] = exc.code
            record["error"] = exc.read().decode(errors="replace")[:500]
        except OSError as exc:
            record["error"] = str(exc)
        self.deliveries.append(record)


class LinearMock:
    def __init__(self, state_dir: Path | None, public_url: str, *,
                 teams: tuple[tuple[str, str], ...] = DEFAULT_TEAMS,
                 api_keys: tuple[str, ...] = (DEFAULT_API_KEY,),
                 webhook_url: str | None = None, webhook_secret: str | None = None) -> None:
        self.store = Store(state_dir, public_url=public_url, teams=teams, api_keys=api_keys)
        self.requests: list[dict] = []
        self.buckets: dict[str, dict] = {}
        self.webhooks = Webhooks(self)
        if webhook_url:
            self.configure_webhook(webhook_url, webhook_secret or "", list(WEBHOOK_TYPES))

    def configure_webhook(self, url: str, secret: str, types: list[str]) -> dict:
        unknown = sorted(set(types) - set(WEBHOOK_TYPES))
        if unknown:
            raise ValueError(f"unsupported resourceTypes: {', '.join(unknown)}")
        if not secret:
            raise ValueError("a webhook needs a signing secret")
        with self.store.lock:
            cfg = {"id": _det_uuid(self.store.seed, "webhook", url), "url": url, "secret": secret,
                   "resourceTypes": types, "enabled": True}
            self.store.data["webhook"] = cfg
            self.store.save()
        return cfg

    def reset(self) -> None:
        with self.store.lock:
            webhook = self.store.data.get("webhook")
            self.store.reset()
            if webhook:
                self.store.data["webhook"] = webhook
                self.store.save()
            self.requests.clear()
            self.buckets.clear()
            self.webhooks.deliveries.clear()

    def bucket(self, user_id: str) -> dict:
        now = time.time() * 1000
        b = self.buckets.get(user_id)
        if b is None or now >= b["resetMs"]:
            b = self.buckets[user_id] = {
                "limit": b["limit"] if b else REQUESTS_PER_HOUR,
                "remaining": b["limit"] if b else REQUESTS_PER_HOUR,
                "complexityLimit": COMPLEXITY_PER_HOUR, "complexity": COMPLEXITY_PER_HOUR,
                "resetMs": int(now + 3_600_000)}
        return b

    def commit(self, mutated: bool) -> None:
        """Persist and send the webhooks of whatever the last operation changed."""
        with self.store.lock:
            if mutated:
                self.store.save()
            events = self.store.drain_events()
        self.webhooks.enqueue(events)

    # -- control helpers ---------------------------------------------------------------
    def actor(self, ref: str | None) -> dict:
        store = self.store
        user = store.find_user(ref) if ref else store.get("User", store.data["humanId"])
        if user is None:
            raise LookupError(f"no user {ref!r}")
        return user

    def label_ids(self, team: dict, names: list[str], actor: dict) -> list[str]:
        ids = []
        for name in names:
            label = self.store.find_label(name, team["id"]) or self.store.add_label(
                None, name, actor=actor)
            ids.append(label["id"])
        return ids

    def issue_view(self, issue: dict) -> dict:
        s = self.store
        state = s.table("WorkflowState")[issue["stateId"]]
        def ident(issue_id: str) -> str:
            return s.identifier(s.table("Issue")[issue_id])
        def user(user_id: str | None) -> str | None:
            row = s.get("User", user_id)
            return row["email"] if row else None
        return {
            "id": issue["id"], "identifier": s.identifier(issue), "title": issue["title"],
            "description": issue["description"], "url": s.issue_url(issue),
            "state": state["name"], "stateType": state["type"], "priority": issue["priority"],
            "labels": [s.table("IssueLabel")[i]["name"] for i in issue["labelIds"]],
            "assignee": user(issue["assigneeId"]), "creator": user(issue["creatorId"]),
            "parent": ident(issue["parentId"]) if issue["parentId"] else None,
            "children": [s.identifier(c) for c in s.rows("Issue") if c["parentId"] == issue["id"]],
            "relations": [{"type": r["type"], "issue": ident(r["issueId"]),
                           "relatedIssue": ident(r["relatedIssueId"])}
                          for r in s.rows("IssueRelation")
                          if issue["id"] in (r["issueId"], r["relatedIssueId"])],
            "comments": [{"id": c["id"], "body": c["body"], "user": user(c["userId"]),
                          "parentId": c["parentId"], "createdAt": c["createdAt"],
                          "editedAt": c["editedAt"]}
                         for c in s.rows("Comment") if c["issueId"] == issue["id"]],
            "attachments": [{"id": a["id"], "title": a["title"], "subtitle": a["subtitle"],
                             "url": a["url"]}
                            for a in s.rows("Attachment") if a["issueId"] == issue["id"]],
            "documents": [{"id": d["id"], "title": d["title"], "url": s.document_url(d)}
                          for d in s.rows("Document") if d["issueId"] == issue["id"]],
            "archivedAt": issue["archivedAt"], "createdAt": issue["createdAt"],
            "updatedAt": issue["updatedAt"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "LinearMock/1"
    protocol_version = "HTTP/1.1"

    @property
    def mock(self) -> LinearMock:
        return self.server.mock  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    # -- plumbing ----------------------------------------------------------------------
    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query)

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._path()
        if path == "/graphql":
            return self._graphql_get(query)
        if path.startswith("/_mock/"):
            return self._control("GET", path[len("/_mock/"):], {})
        if path.startswith("/uploads/"):
            return self._asset(path[len("/uploads/"):])
        m = re.fullmatch(r"/([^/]+)/issue/([A-Za-z0-9]+-\d+)(?:/.*)?", path)
        if m and m.group(1) == self.mock.store.org["urlKey"]:
            with self.mock.store.lock:
                issue = self.mock.store.find_issue(m.group(2))
                if issue is not None:
                    return self._json(200, self.mock.issue_view(issue))
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._path()
        body = self._body()
        if path == "/graphql":
            return self._graphql_post(body)
        if path.startswith("/_mock/"):
            return self._control("POST", path[len("/_mock/"):], self._control_body(body))
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        path, query = self._path()
        body = self._body()
        if path.startswith("/_uploads/"):
            return self._upload(path[len("/_uploads/"):], query, body)
        if path.startswith("/_mock/"):
            return self._control("PUT", path[len("/_mock/"):], self._control_body(body))
        self._json(404, {"error": "not found"})

    # -- GraphQL -----------------------------------------------------------------------
    def _csrf_blocked(self) -> bool:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        preflight = self.headers.get("x-apollo-operation-name") or self.headers.get(
            "apollo-require-preflight")
        return content_type in SIMPLE_TYPES and not preflight

    def _graphql_get(self, query: dict[str, list[str]]) -> None:
        if self._csrf_blocked():
            return self._json(400, {"errors": [{"message": CSRF_MESSAGE,
                                                "extensions": {"code": "BAD_REQUEST"}}]})
        variables: Any = None
        if query.get("variables"):
            try:
                variables = json.loads(query["variables"][0])
            except ValueError:
                return self._bad_request("`variables` is not valid JSON.")
        self._graphql((query.get("query") or [None])[0], variables,
                      (query.get("operationName") or [None])[0], post=False)

    def _graphql_post(self, raw: bytes) -> None:
        if self._csrf_blocked():
            return self._json(400, {"errors": [{"message": CSRF_MESSAGE,
                                                "extensions": {"code": "BAD_REQUEST"}}]})
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            payload = json.loads(raw) if content_type == "application/json" else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not payload:
            return self._bad_request("POST body missing, invalid Content-Type, or JSON object "
                                     "has no keys.")
        self._graphql(payload.get("query"), payload.get("variables"),
                      payload.get("operationName"), post=True)

    def _bad_request(self, message: str) -> None:
        self._json(400, {"errors": [{"message": message, "extensions": {"code": "BAD_REQUEST"}}]})

    def _graphql(self, query: Any, variables: Any, operation_name: Any, *, post: bool) -> None:
        mock = self.mock
        request_id = str(uuid.uuid4())
        auth = (self.headers.get("Authorization") or "").strip()
        user = None if auth.lower().startswith("bearer ") else mock.store.user_for_key(auth)
        if user is None:
            return self._json(401, _error_body(AUTH_ERROR), {"X-Request-Id": request_id})
        with mock.store.lock:
            bucket = mock.bucket(user["id"])
            rate_headers = {
                "X-Request-Id": request_id,
                "X-RateLimit-Requests-Limit": str(bucket["limit"]),
                "X-RateLimit-Requests-Reset": str(bucket["resetMs"])}
            if bucket["remaining"] <= 0 or bucket["complexity"] <= 0:
                retry = max(1, math.ceil((bucket["resetMs"] - time.time() * 1000) / 1000))
                err = LinearError("Rate limit exceeded", type="ratelimited", code="RATELIMITED",
                                  status=429, presentable="Rate limit exceeded. Try again later.")
                rate_headers.update({"X-RateLimit-Requests-Remaining": "0",
                                     "Retry-After": str(retry)})
                return self._json(429, _error_body(err), rate_headers)
            if not post and isinstance(query, str) and _is_mutation(query, operation_name):
                return self._json(405, {"errors": [{
                    "message": "Can only perform a mutation operation from a POST request.",
                    "extensions": {"code": "BAD_REQUEST"}}]}, {**rate_headers, "Allow": "POST"})
            bucket["remaining"] -= 1
            outcome = graph.run(mock.store, user, query, variables, operation_name)
            if outcome.complexity is not None:
                bucket["complexity"] = max(0, bucket["complexity"] - outcome.complexity)
                rate_headers["X-Complexity"] = str(outcome.complexity)
            rate_headers.update({
                "X-RateLimit-Requests-Remaining": str(bucket["remaining"]),
                "X-RateLimit-Complexity-Limit": str(bucket["complexityLimit"]),
                "X-RateLimit-Complexity-Remaining": str(bucket["complexity"]),
                "X-RateLimit-Complexity-Reset": str(bucket["resetMs"])})
            mock.requests.append({
                "requestId": request_id, "user": user["email"], "operationName":
                outcome.operation or operation_name, "query": query, "variables": variables,
                "status": outcome.status, "complexity": outcome.complexity,
                "errors": [e["message"] for e in outcome.body.get("errors", [])]})
            del mock.requests[:-1000]
            mock.commit(outcome.mutated)
        self._json(outcome.status, outcome.body, rate_headers)

    # -- uploads -----------------------------------------------------------------------
    def _storage_error(self, status: int, code: str, message: str) -> None:
        body = (f"<?xml version='1.0' encoding='UTF-8'?><Error><Code>{code}</Code>"
                f"<Message>{message}</Message></Error>").encode()
        self._send(status, body, "application/xml; charset=UTF-8")

    def _upload(self, file_id: str, query: dict[str, list[str]], body: bytes) -> None:
        store = self.mock.store
        with store.lock:
            record = store.data["uploads"].get(file_id)
            expires = (query.get("Expires") or ["0"])[0]
            signature = (query.get("Signature") or [""])[0]
            if record is None or not expires.isdigit() or not hmac.compare_digest(
                    signature, _signature(store, file_id, int(expires))):
                return self._storage_error(403, "SignatureDoesNotMatch",
                                           "The request signature we calculated does not match "
                                           "the signature you provided.")
            if time.time() > int(expires):
                return self._storage_error(400, "ExpiredToken", "The provided token has expired.")
            content_type = (self.headers.get("Content-Type") or "").strip()
            if content_type != record["contentType"]:
                return self._storage_error(403, "SignatureDoesNotMatch",
                                           "Content-Type does not match the signed contentType.")
            for header in _upload_headers(record):
                if (self.headers.get(header["key"]) or "") != header["value"]:
                    return self._storage_error(
                        403, "SignatureDoesNotMatch",
                        f"The request is missing the signed header {header['key']}.")
            if len(body) != record["size"]:
                code = "EntityTooLarge" if len(body) > record["size"] else "EntityTooSmall"
                return self._storage_error(400, code, "The upload does not match the signed size.")
            store.put_blob(file_id, body)
            record["uploaded"] = True
            store.save()
        self._send(200, b"", "text/plain")

    def _asset(self, path: str) -> None:
        store = self.mock.store
        with store.lock:
            record = next((r for r in store.data["uploads"].values() if r["path"] == path), None)
            if record is None or not record["uploaded"]:
                return self._send(404, b"Not Found", "text/plain")
            auth = (self.headers.get("Authorization") or "").strip()
            if not record["public"] and store.user_for_key(auth) is None:
                return self._send(401, b"Unauthorized", "text/plain")
            data = store.get_blob(record["fileId"]) or b""
        name = record["originalName"].replace('"', "")
        self._send(200, data, record["contentType"], {
            "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "public, max-age=31536000"})

    # -- control -----------------------------------------------------------------------
    @staticmethod
    def _control_body(raw: bytes) -> Any:
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return None

    def _control(self, method: str, route: str, body: Any) -> None:
        if not isinstance(body, dict):
            return self._json(400, {"error": "the body must be a JSON object"})
        try:
            status, payload = self._route(method, route.strip("/"), body)
        except LinearError as exc:
            status, payload = exc.status, {"error": exc.presentable, "code": exc.code}
        except (LookupError, ValueError) as exc:
            status, payload = 400, {"error": str(exc).strip("'\"")}
        self._json(status, payload)

    def _route(self, method: str, route: str, body: dict) -> tuple[int, Any]:
        mock = self.mock
        store = mock.store
        parts = route.split("/")
        if method == "GET" and route == "health":
            return 200, {"ok": True}
        if method == "GET" and route == "state":
            with store.lock:
                return 200, json.loads(json.dumps(store.data))
        if method == "POST" and route == "reset":
            mock.reset()
            return 200, {"ok": True}
        if method == "GET" and route == "requests":
            return 200, {"requests": list(mock.requests)}
        if method == "GET" and route == "uploads":
            with store.lock:
                return 200, {"uploads": [upload_file(store, r) | {"uploaded": r["uploaded"]}
                                         for r in store.data["uploads"].values()]}
        if route == "webhooks":
            if method == "GET":
                cfg = dict(store.data.get("webhook") or {})
                cfg.pop("secret", None)
                return 200, {"config": cfg or None, "deliveries": list(mock.webhooks.deliveries)}
            if method == "PUT":
                cfg = mock.configure_webhook(str(body.get("url") or ""), str(body.get("secret") or ""),
                                             list(body.get("resourceTypes") or WEBHOOK_TYPES))
                return 200, {k: v for k, v in cfg.items() if k != "secret"}
        if method == "POST" and route == "webhooks/flush":
            return 200, {"flushed": mock.webhooks.flush(float(body.get("timeout") or 10))}
        if method == "POST" and route == "ratelimit":
            with store.lock:
                for user in store.rows("User"):
                    bucket = mock.bucket(user["id"])
                    if "limit" in body:
                        bucket["limit"] = int(body["limit"])
                    if "remaining" in body:
                        bucket["remaining"] = int(body["remaining"])
                    if "complexityRemaining" in body:
                        bucket["complexity"] = int(body["complexityRemaining"])
            return 200, {"ok": True}
        with store.lock:
            result = self._route_entities(method, parts, body)
            if result is not None:
                mock.commit(method != "GET")
                return result
        return 404, {"error": f"no control route {method} /_mock/{route}"}

    def _route_entities(self, method: str, parts: list[str], body: dict) -> tuple[int, Any] | None:
        mock = self.mock
        store = mock.store
        if parts[0] == "issues" and len(parts) == 1:
            if method == "GET":
                return 200, {"issues": [mock.issue_view(i) for i in store.rows("Issue")]}
            if method == "POST":
                return 201, mock.issue_view(self._create_issue(body))
        if parts[0] == "issues" and len(parts) >= 2:
            issue = store.require_issue(parts[1])
            actor = mock.actor(body.get("as"))
            if len(parts) == 2 and method == "GET":
                return 200, mock.issue_view(issue)
            if parts[2:] == ["state"] and method == "POST":
                team = store.table("Team")[issue["teamId"]]
                state = store.state_named(team, str(body.get("state") or ""))
                if state is None:
                    raise LookupError(f"team {team['key']} has no state {body.get('state')!r}")
                store.update_issue(actor, issue["id"], {"stateId": state["id"]})
                return 200, mock.issue_view(issue)
            if parts[2:] == ["comments"] and method == "POST":
                comment = store.create_comment(actor, {"issueId": issue["id"],
                                                       "body": body.get("body"),
                                                       "parentId": body.get("parent")})
                return 201, {"id": comment["id"], "url": store.comment_url(comment)}
            if parts[2:] == ["labels"] and method == "POST":
                team = store.table("Team")[issue["teamId"]]
                add = mock.label_ids(team, list(body.get("add") or []), actor)
                remove = [lb["id"] for lb in (store.find_label(n, team["id"])
                                              for n in body.get("remove") or []) if lb]
                store.set_labels(actor, issue["id"], add, remove)
                return 200, mock.issue_view(issue)
        if parts == ["labels"] and method == "POST":
            team = store.find_team(body.get("team")) if body.get("team") else None
            if body.get("team") and team is None:
                raise LookupError(f"no team {body['team']!r}")
            label = store.add_label(team["id"] if team else None, str(body.get("name") or ""),
                                    actor=mock.actor(body.get("as")), color=body.get("color"))
            return 201, {"id": label["id"], "name": label["name"]}
        if parts == ["teams"] and method == "POST":
            team = store.add_team(str(body.get("key") or ""), str(body.get("name") or body.get("key")))
            return 201, {"id": team["id"], "key": team["key"]}
        if parts == ["users"] and method == "POST":
            email = str(body.get("email") or "")
            if not email or store.find_user(email):
                raise ValueError("a new user needs a unique email")
            user = store.add_user(str(body.get("name") or email), email,
                                  str(body.get("displayName") or email.split("@")[0]))
            for team in store.rows("Team"):
                team["memberIds"].append(user["id"])
            if body.get("apiKey"):
                store.data["apiKeys"][str(body["apiKey"])] = user["id"]
            return 201, {"id": user["id"], "email": email}
        if parts == ["api-keys"] and method == "POST":
            user = store.find_user(body.get("user")) if body.get("user") else store.get(
                "User", store.data["botId"])
            if user is None or not body.get("key"):
                raise LookupError("api-keys needs a key and a known user")
            store.data["apiKeys"][str(body["key"])] = user["id"]
            return 201, {"user": user["email"]}
        return None

    def _create_issue(self, body: dict) -> dict:
        mock = self.mock
        store = mock.store
        actor = mock.actor(body.get("as"))
        team = store.find_team(body.get("team")) if body.get("team") else store.rows("Team")[0]
        if team is None:
            raise LookupError(f"no team {body.get('team')!r}")
        inp: dict[str, Any] = {"teamId": team["id"], "title": body.get("title"),
                               "description": body.get("description"),
                               "priority": body.get("priority")}
        if body.get("state"):
            state = store.state_named(team, body["state"])
            if state is None:
                raise LookupError(f"team {team['key']} has no state {body['state']!r}")
            inp["stateId"] = state["id"]
        if body.get("labels"):
            inp["labelIds"] = mock.label_ids(team, list(body["labels"]), actor)
        if body.get("parent"):
            inp["parentId"] = store.require_issue(body["parent"])["id"]
        if body.get("assignee"):
            inp["assigneeId"] = mock.actor(body["assignee"])["id"]
        if body.get("project"):
            project = next((p for p in store.rows("Project")
                            if body["project"] in (p["id"], p["slugId"], p["name"])), None)
            if project is None:
                raise LookupError(f"no project {body['project']!r}")
            inp["projectId"] = project["id"]
        return store.create_issue(actor, inp)


def _is_mutation(query: str, operation_name: Any) -> bool:
    try:
        doc = graph.graphql.parse(query)
    except graph.GraphQLError:
        return False
    ops = [d for d in doc.definitions if isinstance(d, graph.OperationDefinitionNode)]
    op = next((o for o in ops if o.name and o.name.value == operation_name), None) or (
        ops[0] if len(ops) == 1 else None)
    return op is not None and op.operation.value == "mutation"


def serve(port: int, state_dir: Path | None, host: str = "127.0.0.1", *,
          public_url: str | None = None, teams: tuple[tuple[str, str], ...] = DEFAULT_TEAMS,
          api_keys: tuple[str, ...] = (DEFAULT_API_KEY,), webhook_url: str | None = None,
          webhook_secret: str | None = None, verbose: bool = False) -> ThreadingHTTPServer:
    """Bind and return the server (call `serve_forever`); `server.mock` is the `LinearMock`."""
    graph.schema()  # build once, before the first request
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.verbose = verbose  # type: ignore[attr-defined]
    url = public_url or f"http://{host}:{server.server_port}"
    server.mock = LinearMock(state_dir, url, teams=teams, api_keys=api_keys,  # type: ignore[attr-defined]
                             webhook_url=webhook_url, webhook_secret=webhook_secret)
    return server
