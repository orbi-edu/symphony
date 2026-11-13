"""S3 mock: PutObject, GetObject, ListObjectsV2 and DeleteObject, signed with SigV4, checked
against the vendored AWS pages in `scripts/mocks/specs/`:

- `aws-s3-put-object.md`, `aws-s3-get-object.md`, `aws-s3-list-objects-v2.md`,
  `aws-s3-delete-object.md`: the request syntax of each operation is parsed at start. A
  query parameter or an `x-amz-*` header that the page does not list → 400 `InvalidArgument`.
  One the page lists but this mock does not model (ranges, conditions, checksums, encryption,
  `delimiter`, …) → 501 `NotImplemented`, the page's code for "functionality that is not
  implemented". Another method or path → 501 `NotImplemented` or 405 `MethodNotAllowed`.
- `aws-s3-error-responses.md`: the `<Error>` body and the HTTP status of each code, parsed
  from the page.
- `aws-s3-sigv4-header-auth.md`, `aws-sigv4-signing-elements.md`,
  `aws-sigv4-troubleshooting.md`: header SigV4. Each request needs `Authorization:
  AWS4-HMAC-SHA256 Credential=…, SignedHeaders=…, Signature=…`, `x-amz-date` and
  `x-amz-content-sha256`. The mock rebuilds the canonical request and the signature and
  checks the scope date, Region and service, that `host` and every `x-amz-*` header but
  `x-amz-content-sha256` are signed, and that the body hashes to `x-amz-content-sha256`
  (`UNSIGNED-PAYLOAD` skips that). The pages give no clock window; the mock allows 15
  minutes, the AWS default.

Path-style URLs only (`/<bucket>/<key>`). PutObject needs `Content-Length` (else 411
`MissingContentLength`), stores the body, `Content-Type` and the MD5 ETag, and checks
`Content-MD5`. ListObjectsV2 serves `prefix`, `max-keys`, `start-after` and
`continuation-token`.

Control (`/_mock/*`, JSON, no auth):
- `POST /_mock/buckets` `{"name"}`; `POST /_mock/credentials` `{"access_key_id",
  "secret_access_key"}`
- `GET /_mock/objects` — `[{bucket, key, size, etag}]`
- `GET /_mock/requests` — `[{operation, status, code}]` per S3 request (never a header value)
- `POST /_mock/reset` — drop objects and the request log; buckets and credentials stay

State is in memory only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from xml.sax.saxutils import escape

SPECS = Path(__file__).resolve().parents[1] / "specs"
PAGES = {
    "PutObject": "aws-s3-put-object.md",
    "GetObject": "aws-s3-get-object.md",
    "ListObjectsV2": "aws-s3-list-objects-v2.md",
    "DeleteObject": "aws-s3-delete-object.md",
}
# What this mock does with each operation; any other listed parameter is NotImplemented.
MODELED = {
    "PutObject": {"content-length", "content-type", "content-md5"},
    "GetObject": set(),
    "ListObjectsV2": {"list-type", "prefix", "max-keys", "start-after", "continuation-token"},
    "DeleteObject": set(),
}
# SigV4 headers (aws-s3-sigv4-header-auth.md); `x-amz-security-token` is for temporary
# credentials, which this mock does not issue.
SIGNING = {"x-amz-date", "x-amz-content-sha256"}
ALGORITHM = "AWS4-HMAC-SHA256"
SKEW_S = 15 * 60
XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"
DEFAULT_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"  # the example keys of the SigV4 page
DEFAULT_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    has_key: bool
    fixed: dict[str, str]
    query: frozenset[str]
    headers: frozenset[str]


def _request_syntax(text: str) -> list[str]:
    section = text.split("## Request Syntax", 1)[1]
    block = section.split("```", 2)[1]
    return [line.strip() for line in block.strip().splitlines() if line.strip()]


def load_operation(name: str) -> Operation:
    lines = _request_syntax((SPECS / PAGES[name]).read_text(encoding="utf-8"))
    method, target, _version = lines[0].split(" ")
    path, _, query_text = target.partition("?")
    fixed, query = {}, set()
    for part in filter(None, query_text.split("&")):
        key, _, value = part.partition("=")
        query.add(key)
        if not value.startswith("{{"):
            fixed[key] = value
    headers = set()
    for line in lines[1:]:
        header, sep, _value = line.partition(":")
        if sep and header.lower() != "host" and not header.startswith("{{"):
            headers.add(header.lower())
    return Operation(name, method, "{{Key+}}" in path, fixed, frozenset(query), frozenset(headers))


def load_errors() -> dict[str, int]:
    """Error code → HTTP status, from the list of `aws-s3-error-responses.md`."""
    text = (SPECS / "aws-s3-error-responses.md").read_text(encoding="utf-8")
    out: dict[str, int] = {}
    pattern = re.compile(
        r"^- \*\* ?`(\w+)` ?\*\*\n(?:  - .*\n)*?  - \*\*HTTP status code:\*\* `?(\d{3})", re.M
    )
    for code, status in pattern.findall(text):
        out.setdefault(code, int(status))
    return out


OPERATIONS = {name: load_operation(name) for name in PAGES}
ERRORS = load_errors()


class S3Failure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message
        self.status = ERRORS[code]


@dataclass
class StoredObject:
    body: bytes
    etag: str
    modified: float
    content_type: str


@dataclass
class State:
    region: str
    credentials: dict[str, str]
    buckets: dict[str, dict[str, StoredObject]] = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def uri_encode(value: str, *, slash: bool) -> str:
    return quote(value, safe="-._~" + ("" if slash else "/"))


_AUTH = re.compile(
    r"AWS4-HMAC-SHA256 Credential=([^/,\s]+)/(\d{8})/([^/,\s]+)/([^/,\s]+)/aws4_request,\s*"
    r"SignedHeaders=([a-z0-9;-]+),\s*Signature=([0-9a-f]{64})\Z"
)


def verify_signature(state: State, method: str, raw_path: str, raw_query: str,
                     headers: dict[str, str], body: bytes, now: float) -> None:
    auth = headers.get("authorization")
    if not auth:
        raise S3Failure("AccessDenied", "Access Denied")
    if not auth.startswith(ALGORITHM + " "):
        raise S3Failure("InvalidRequest", "Please use AWS4-HMAC-SHA256.")
    match = _AUTH.fullmatch(auth.strip())
    if not match:
        raise S3Failure("AuthorizationHeaderMalformed", "The authorization header is malformed.")
    access_key, scope_date, region, service, signed, signature = match.groups()
    amz_date, payload_hash = headers.get("x-amz-date"), headers.get("x-amz-content-sha256")
    if not amz_date or not payload_hash:
        raise S3Failure("MissingSecurityHeader", "Your request is missing a required header.")
    secret = state.credentials.get(access_key)
    if secret is None:
        raise S3Failure(
            "InvalidAccessKeyId", "The AWS access key ID that you provided does not exist."
        )
    try:
        moment = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        raise S3Failure("AccessDenied", "x-amz-date is not YYYYMMDDTHHMMSSZ.") from None
    if scope_date != amz_date[:8]:
        raise S3Failure(
            "AuthorizationHeaderMalformed",
            "Date in Credential scope does not match YYYYMMDD from ISO-8601 version of date "
            "from HTTP",
        )
    if region != state.region:
        raise S3Failure(
            "AuthorizationHeaderMalformed",
            f"Credential should be scoped to a valid Region, not {region}",
        )
    if service != "s3":
        raise S3Failure(
            "AuthorizationHeaderMalformed", "Credential should be scoped to correct service: 's3'"
        )
    if abs(now - moment) > SKEW_S:
        raise S3Failure(
            "RequestTimeTooSkewed",
            "The difference between the request time and the server's time is too large.",
        )
    names = signed.split(";")
    if names != sorted(names):
        raise S3Failure("AuthorizationHeaderMalformed", "SignedHeaders must be sorted.")
    must = {"host"} | {h for h in headers if h.startswith("x-amz-")} - {"x-amz-content-sha256"}
    if not must <= set(names) or not set(names) <= set(headers):
        raise S3Failure("AccessDenied", "There were headers present in the request which were not signed.")
    pairs = sorted(
        (uri_encode(k, slash=True), uri_encode(v, slash=True))
        for k, v in parse_qsl(raw_query, keep_blank_values=True)
    )
    canonical = "\n".join([
        method,
        uri_encode(unquote(raw_path), slash=False),
        "&".join(f"{k}={v}" for k, v in pairs),
        "".join(f"{n}:{' '.join(headers[n].split())}\n" for n in names),
        signed,
        payload_hash,
    ])
    scope = f"{scope_date}/{region}/s3/aws4_request"
    to_sign = f"{ALGORITHM}\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = ("AWS4" + secret).encode()
    for part in (scope_date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise S3Failure(
            "SignatureDoesNotMatch",
            "The request signature we calculated does not match the signature you provided.",
        )
    if payload_hash != "UNSIGNED-PAYLOAD" and payload_hash != hashlib.sha256(body).hexdigest():
        raise S3Failure("BadDigest", "The x-amz-content-sha256 value did not match the body.")


def _operation(method: str, key: str, query: dict[str, str]) -> Operation:
    if key:
        by_method = {"PUT": "PutObject", "GET": "GetObject", "DELETE": "DeleteObject"}
        if method not in by_method:
            raise S3Failure("MethodNotAllowed", "The specified method is not allowed.")
        return OPERATIONS[by_method[method]]
    if method == "GET" and query.get("list-type") == "2":
        return OPERATIONS["ListObjectsV2"]
    raise S3Failure("NotImplemented", "This mock serves only PutObject, GetObject, "
                                      "ListObjectsV2 and DeleteObject.")


def _check_parameters(op: Operation, query: dict[str, str], headers: dict[str, str]) -> None:
    for name in query:
        if name not in op.query:
            raise S3Failure("InvalidArgument", f"{op.name} has no query parameter {name}.")
        if name not in MODELED[op.name]:
            raise S3Failure("NotImplemented", f"The mock does not model {name}.")
    for name, value in op.fixed.items():
        if query.get(name) != value:
            raise S3Failure("InvalidArgument", f"{op.name} needs {name}={value}.")
    for name in headers:
        if name.startswith("x-amz-") and name not in SIGNING and name not in op.headers:
            raise S3Failure("InvalidArgument", f"{op.name} has no header {name}.")
        if name in op.headers and name not in MODELED[op.name]:
            raise S3Failure("NotImplemented", f"The mock does not model the header {name}.")


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Handler(BaseHTTPRequestHandler):
    server_version = "AmazonS3"
    state: State

    def log_message(self, *_args) -> None:
        pass

    # -- plumbing -----------------------------------------------------------------------
    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("x-amz-request-id", uuid.uuid4().hex[:16].upper())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, doc) -> None:
        self._send(status, json.dumps(doc).encode(), {"Content-Type": "application/json"})

    def _error(self, failure: S3Failure, resource: str) -> None:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Error>'
            f"<Code>{failure.code}</Code><Message>{escape(failure.message)}</Message>"
            f"<Resource>{escape(resource)}</Resource>"
            f"<RequestId>{uuid.uuid4().hex[:16].upper()}</RequestId></Error>"
        ).encode()
        self._send(failure.status, body, {"Content-Type": "application/xml"})

    def _body(self, required: bool) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise S3Failure("MissingContentLength", "You must provide the Content-Length HTTP header.")
        length = self.headers.get("Content-Length")
        if length is None:
            if required:
                raise S3Failure("MissingContentLength", "You must provide the Content-Length HTTP header.")
            return b""
        size = int(length)
        body = self.rfile.read(size)
        if len(body) != size:
            raise S3Failure("IncompleteBody", "The body is shorter than Content-Length.")
        return body

    # -- dispatch -----------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        parts = urlsplit(self.path)
        if parts.path.startswith("/_mock/"):
            self._control(parts.path, parts.query)
            return
        state = self.state
        operation = "unknown"
        try:
            bucket, _, raw_key = parts.path.lstrip("/").partition("/")
            key = unquote(raw_key)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            headers = {name.lower(): value for name, value in self.headers.items()}
            op = _operation(self.command, key, query)
            operation = op.name
            # S3 authenticates first: the body is read, the signature checked, then the
            # parameters.
            body = self._body(required=op.name == "PutObject")
            verify_signature(state, self.command, parts.path, parts.query, headers, body,
                             time.time())
            _check_parameters(op, query, headers)
            with state.lock:
                if bucket not in state.buckets:
                    raise S3Failure("NoSuchBucket", "The specified bucket does not exist.")
                getattr(self, f"_op_{op.name}")(state.buckets[bucket], bucket, key, query,
                                                headers, body)
                state.requests.append({"operation": operation, "status": 200, "code": None})
        except S3Failure as failure:
            with state.lock:
                state.requests.append(
                    {"operation": operation, "status": failure.status, "code": failure.code}
                )
            self._error(failure, parts.path)

    # -- operations ---------------------------------------------------------------------
    def _op_PutObject(self, objects, bucket, key, query, headers, body) -> None:
        if "content-md5" in headers:
            try:
                digest = base64.b64decode(headers["content-md5"], validate=True)
            except ValueError:
                raise S3Failure("InvalidDigest", "The Content-MD5 you specified is not valid.") from None
            if digest != hashlib.md5(body).digest():
                raise S3Failure("BadDigest", "The Content-MD5 you specified did not match.")
        etag = f'"{hashlib.md5(body).hexdigest()}"'
        objects[key] = StoredObject(
            body, etag, time.time(), headers.get("content-type", "binary/octet-stream")
        )
        self._send(200, b"", {"ETag": etag})

    def _op_GetObject(self, objects, bucket, key, query, headers, body) -> None:
        item = objects.get(key)
        if item is None:
            raise S3Failure("NoSuchKey", "The specified key does not exist.")
        self._send(200, item.body, {
            "ETag": item.etag,
            "Last-Modified": formatdate(item.modified, usegmt=True),
            "Content-Type": item.content_type,
        })

    def _op_DeleteObject(self, objects, bucket, key, query, headers, body) -> None:
        objects.pop(key, None)
        self._send(204)

    def _op_ListObjectsV2(self, objects, bucket, key, query, headers, body) -> None:
        prefix = query.get("prefix", "")
        try:
            max_keys = int(query.get("max-keys", "1000"))
        except ValueError:
            raise S3Failure("InvalidArgument", "max-keys must be an integer.") from None
        if max_keys < 0:
            raise S3Failure("InvalidArgument", "max-keys must not be negative.")
        max_keys = min(max_keys, 1000)
        after = query.get("start-after", "")
        token = query.get("continuation-token")
        if token is not None:
            try:
                after = max(after, base64.urlsafe_b64decode(token.encode()).decode())
            except ValueError:
                raise S3Failure("InvalidArgument", "The continuation token is not valid.") from None
        names = sorted(k for k in objects if k.startswith(prefix) and k > after)
        page, truncated = names[:max_keys], len(names) > max_keys
        parts = [
            f'<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="{XMLNS}">',
            f"<Name>{escape(bucket)}</Name><Prefix>{escape(prefix)}</Prefix>",
            f"<KeyCount>{len(page)}</KeyCount><MaxKeys>{max_keys}</MaxKeys>",
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
        ]
        if token is not None:
            parts.append(f"<ContinuationToken>{escape(token)}</ContinuationToken>")
        if "start-after" in query:
            parts.append(f"<StartAfter>{escape(query['start-after'])}</StartAfter>")
        if truncated:
            next_token = base64.urlsafe_b64encode(page[-1].encode()).decode()
            parts.append(f"<NextContinuationToken>{next_token}</NextContinuationToken>")
        for name in page:
            item = objects[name]
            parts.append(
                f"<Contents><Key>{escape(name)}</Key><LastModified>{_iso(item.modified)}"
                f"</LastModified><ETag>{escape(item.etag)}</ETag><Size>{len(item.body)}</Size>"
                "<StorageClass>STANDARD</StorageClass></Contents>"
            )
        parts.append("</ListBucketResult>")
        self._send(200, "".join(parts).encode(), {"Content-Type": "application/xml"})

    # -- control ------------------------------------------------------------------------
    def _control(self, path: str, query: str) -> None:
        state = self.state
        length = int(self.headers.get("Content-Length") or 0)
        doc = json.loads(self.rfile.read(length) or b"{}") if length else {}
        with state.lock:
            if self.command == "POST" and path == "/_mock/buckets":
                state.buckets.setdefault(doc["name"], {})
                self._json(200, {"buckets": sorted(state.buckets)})
            elif self.command == "POST" and path == "/_mock/credentials":
                state.credentials[doc["access_key_id"]] = doc["secret_access_key"]
                self._json(200, {"access_key_ids": sorted(state.credentials)})
            elif self.command == "GET" and path == "/_mock/objects":
                self._json(200, [
                    {"bucket": b, "key": k, "size": len(o.body), "etag": o.etag}
                    for b, objects in sorted(state.buckets.items())
                    for k, o in sorted(objects.items())
                ])
            elif self.command == "GET" and path == "/_mock/requests":
                self._json(200, list(state.requests))
            elif self.command == "POST" and path == "/_mock/reset":
                for objects in state.buckets.values():
                    objects.clear()
                state.requests.clear()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "unknown control route"})


def serve(
    port: int = 0,
    host: str = "127.0.0.1",
    *,
    region: str = "us-east-1",
    buckets: tuple[str, ...] = (),
    credentials: dict[str, str] | None = None,
) -> ThreadingHTTPServer:
    """Bind the mock (port 0 picks a free one); the caller runs `serve_forever`."""
    state = State(region=region, credentials=dict(credentials or {
        DEFAULT_ACCESS_KEY: DEFAULT_SECRET_KEY,
    }))
    for name in buckets:
        state.buckets[name] = {}
    handler = type("S3Handler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
