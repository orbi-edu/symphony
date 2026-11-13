"""A small S3 client: PutObject, GetObject, ListObjectsV2 and DeleteObject, signed with AWS
Signature Version 4 (header auth, signed payload) over httpx.

It uses path-style URLs (`<endpoint>/<bucket>/<key>`), which AWS, MinIO, Ceph and R2 accept.
Bodies stream from and to files, so an object never has to fit in memory. Error messages
carry the S3 error code, never a credential or a signature.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from typing import BinaryIO
from urllib.parse import quote, urlsplit

import httpx

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 1024 * 1024


class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


def uri_encode(value: str, *, slash: bool) -> str:
    """UriEncode() of the S3 SigV4 page: every byte but `A-Za-z0-9-._~`, `%XX` upper case;
    `/` stays as is in an object key name (`slash=False`)."""
    return quote(value, safe="-._~" + ("" if slash else "/"))


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str = "s3") -> bytes:
    key = _hmac(("AWS4" + secret_key).encode(), date)
    return _hmac(_hmac(_hmac(key, region), service), "aws4_request")


def canonical_query(query: list[tuple[str, str]]) -> str:
    pairs = sorted((uri_encode(k, slash=True), uri_encode(v, slash=True)) for k, v in query)
    return "&".join(f"{k}={v}" for k, v in pairs)


def authorization(
    *,
    method: str,
    path: str,
    query: list[tuple[str, str]],
    headers: dict[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
) -> str:
    """The `Authorization` header value. `path` is the canonical URI (already encoded);
    every header in `headers` is signed."""
    canonical_headers = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(canonical_headers)
    canonical = "\n".join([
        method,
        path,
        canonical_query(query),
        "".join(f"{name}:{canonical_headers[name]}\n" for name in names),
        ";".join(names),
        payload_sha256,
    ])
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    signature = hmac.new(
        signing_key(secret_key, amz_date[:8], region), to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return (
        f"{ALGORITHM} Credential={access_key}/{scope},SignedHeaders={';'.join(names)},"
        f"Signature={signature}"
    )


def _text(element, name: str) -> str | None:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            return child.text
    return None


def _children(element, name: str) -> list:
    return [child for child in element if child.tag.rsplit("}", 1)[-1] == name]


class S3Client:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        timeout_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        parts = urlsplit(endpoint)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._host = parts.netloc
        self.bucket, self.region = bucket, region
        self._access_key, self._secret_key = access_key, secret_key
        self._timeout_s = timeout_s
        self._clock = clock

    def _http(self) -> httpx.Client:
        """One connection pool per operation: nothing stays open between calls."""
        return httpx.Client(timeout=self._timeout_s)

    def _request(
        self,
        http: httpx.Client,
        method: str,
        key: str | None,
        *,
        query: list[tuple[str, str]] | None = None,
        body: Iterator[bytes] | None = None,
        size: int = 0,
        payload_sha256: str = EMPTY_SHA256,
        stream: bool = False,
    ) -> httpx.Response:
        query = query or []
        path = f"/{uri_encode(self.bucket, slash=True)}"
        if key is not None:
            path += f"/{uri_encode(key, slash=False)}"
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._clock()))
        headers = {"host": self._host, "x-amz-content-sha256": payload_sha256,
                   "x-amz-date": amz_date}
        headers["authorization"] = authorization(
            method=method, path=path, query=query, headers=headers,
            payload_sha256=payload_sha256, access_key=self._access_key,
            secret_key=self._secret_key, region=self.region, amz_date=amz_date,
        )
        if body is not None:
            headers["content-length"] = str(size)
        url = self._origin + path + (f"?{canonical_query(query)}" if query else "")
        request = http.build_request(method, httpx.URL(url), headers=headers, content=body)
        try:
            response = http.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise S3Error(0, "unreachable", f"S3 endpoint {self._origin} is unreachable") from exc
        if response.status_code >= 300:
            content = response.read()
            response.close()
            code, message = str(response.status_code), response.reason_phrase
            try:
                root = ET.fromstring(content)
                code = _text(root, "Code") or code
                message = _text(root, "Message") or message
            except ET.ParseError:
                pass
            raise S3Error(response.status_code, code, message)
        return response

    def put(self, key: str, source: BinaryIO) -> None:
        """Upload a seekable file: one pass hashes it, the second streams it."""
        digest = hashlib.sha256()
        source.seek(0)
        size = 0
        while chunk := source.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
        source.seek(0)

        def chunks() -> Iterator[bytes]:
            while chunk := source.read(CHUNK):
                yield chunk

        with self._http() as http:
            self._request(
                http, "PUT", key, body=chunks(), size=size, payload_sha256=digest.hexdigest()
            ).close()

    def get(self, key: str, dest: BinaryIO) -> None:
        with self._http() as http:
            response = self._request(http, "GET", key, stream=True)
            try:
                for chunk in response.iter_bytes(CHUNK):
                    dest.write(chunk)
            finally:
                response.close()

    def list(self, prefix: str) -> list[dict]:
        """Every object under `prefix`: `{key, size, last_modified}`, in key order."""
        out: list[dict] = []
        token: str | None = None
        with self._http() as http:
            while True:
                query = [("list-type", "2"), ("prefix", prefix)]
                if token:
                    query.append(("continuation-token", token))
                root = ET.fromstring(self._request(http, "GET", None, query=query).content)
                for item in _children(root, "Contents"):
                    out.append({
                        "key": _text(item, "Key") or "",
                        "size": int(_text(item, "Size") or 0),
                        "last_modified": _text(item, "LastModified"),
                    })
                token = _text(root, "NextContinuationToken")
                if (_text(root, "IsTruncated") or "false") != "true" or not token:
                    return out

    def delete(self, key: str) -> None:
        with self._http() as http:
            self._request(http, "DELETE", key).close()
