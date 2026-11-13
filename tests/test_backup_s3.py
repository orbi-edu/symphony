"""The S3 target (`symphony.s3`, `backup.S3Target`) against the S3 mock, and the mock itself
against the vendored AWS pages (`scripts/mocks/s3`)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest
from scripts.mocks.s3 import server as mock

from symphony import backup, s3
from symphony.config import parse_workflow
from symphony.sessions import SessionStore

SPECS = Path(__file__).resolve().parent.parent / "scripts" / "mocks" / "specs"
NS = "prod"
ACCESS, SECRET = "AKIDBACKUPTEST", "backup-test-secret-key"  # dummy values
BUCKET = "symphony-backups"


@pytest.fixture
def s3_mock():
    server = mock.serve(0, buckets=(BUCKET,), credentials={ACCESS: SECRET})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def endpoint(server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def client_for(server, *, secret: str = SECRET, access: str = ACCESS, clock=time.time,
               region: str = "us-east-1") -> s3.S3Client:
    return s3.S3Client(endpoint=endpoint(server), bucket=BUCKET, region=region,
                       access_key=access, secret_key=secret, clock=clock)


def requests_of(server) -> list[dict]:
    return httpx.get(f"{endpoint(server)}/_mock/requests").json()


def signed(server, method: str, path: str, *, query=(), headers=None, body: bytes = b"",
           payload: str | None = None, sign_extra: bool = True) -> httpx.Response:
    """One hand-signed request, to reach the mock's checks the client never trips."""
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    payload = payload or hashlib.sha256(body).hexdigest()
    host = f"127.0.0.1:{server.server_port}"
    extra = dict(headers or {})
    to_sign = {"host": host, "x-amz-content-sha256": payload, "x-amz-date": amz_date,
               **(extra if sign_extra else {})}
    auth = s3.authorization(method=method, path=path, query=list(query), headers=to_sign,
                            payload_sha256=payload, access_key=ACCESS, secret_key=SECRET,
                            region="us-east-1", amz_date=amz_date)
    url = endpoint(server) + path + (f"?{s3.canonical_query(list(query))}" if query else "")
    return httpx.request(method, httpx.URL(url), content=body, headers={
        **to_sign, **extra, "authorization": auth,
    })


def error_code(response: httpx.Response) -> str:
    return re.search(r"<Code>(\w+)</Code>", response.text).group(1)


# -- the signer against the examples of the AWS SigV4 page --------------------------------

def test_signer_matches_the_aws_examples():
    page = (SPECS / "aws-s3-sigv4-header-auth.md").read_text()
    keys = dict(access_key="AKIAIOSFODNN7EXAMPLE",
                secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                region="us-east-1", amz_date="20130524T000000Z")
    get = s3.authorization(method="GET", path="/test.txt", query=[], headers={
        "host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9",
        "x-amz-content-sha256": s3.EMPTY_SHA256, "x-amz-date": "20130524T000000Z",
    }, payload_sha256=s3.EMPTY_SHA256, **keys)
    body_hash = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
    put = s3.authorization(method="PUT", path="/" + s3.uri_encode("test$file.text", slash=False),
                           query=[], headers={
        "date": "Fri, 24 May 2013 00:00:00 GMT", "host": "examplebucket.s3.amazonaws.com",
        "x-amz-content-sha256": body_hash, "x-amz-date": "20130524T000000Z",
        "x-amz-storage-class": "REDUCED_REDUNDANCY",
    }, payload_sha256=body_hash, **keys)
    for header in (get, put):
        assert header in page  # the exact Authorization header the page prints


# -- the mock follows the vendored pages --------------------------------------------------

def test_mock_reads_its_rules_from_the_pages():
    assert mock.OPERATIONS["ListObjectsV2"].fixed == {"list-type": "2"}
    assert "x-amz-storage-class" in mock.OPERATIONS["PutObject"].headers
    for code, status in {"NoSuchKey": 404, "SignatureDoesNotMatch": 403,
                         "MissingContentLength": 411, "NotImplemented": 501}.items():
        assert mock.ERRORS[code] == status


def test_list_response_uses_only_documented_elements(s3_mock):
    client = client_for(s3_mock)
    client.put("a/one", io.BytesIO(b"1"))
    response = signed(s3_mock, "GET", f"/{BUCKET}", query=[("list-type", "2"), ("prefix", "a/")])
    syntax = (SPECS / "aws-s3-list-objects-v2.md").read_text().split("## Response Syntax")[1]
    documented = set(re.findall(r"<(\w+)>", syntax.split("```")[1]))
    assert set(re.findall(r"<(\w+)[ >]", response.text)) - {"xml"} <= documented


def test_mock_verifies_sigv4(s3_mock):
    good = client_for(s3_mock)
    good.put("k", io.BytesIO(b"data"))
    cases = {
        "SignatureDoesNotMatch": client_for(s3_mock, secret="wrong"),
        "InvalidAccessKeyId": client_for(s3_mock, access="AKIDUNKNOWN"),
        "RequestTimeTooSkewed": client_for(s3_mock, clock=lambda: time.time() - 3600),
        "AuthorizationHeaderMalformed": client_for(s3_mock, region="eu-west-1"),
    }
    for code, client in cases.items():
        with pytest.raises(s3.S3Error) as caught:
            client.get("k", io.BytesIO())
        assert caught.value.code == code and caught.value.status in (400, 403)
    # the body must hash to x-amz-content-sha256
    response = signed(s3_mock, "PUT", f"/{BUCKET}/k", body=b"other",
                      payload=hashlib.sha256(b"data").hexdigest())
    assert (response.status_code, error_code(response)) == (400, "BadDigest")
    # an x-amz-* header must be signed
    response = signed(s3_mock, "PUT", f"/{BUCKET}/k", body=b"x",
                      headers={"x-amz-storage-class": "STANDARD"}, sign_extra=False)
    assert error_code(response) == "AccessDenied"
    assert httpx.get(f"{endpoint(s3_mock)}/{BUCKET}/k").status_code == 403
    out = io.BytesIO()
    good.get("k", out)
    assert out.getvalue() == b"data"


def test_mock_checks_parameters_against_the_request_syntax(s3_mock):
    unknown = signed(s3_mock, "GET", f"/{BUCKET}/k", headers={"x-amz-bogus": "1"})
    assert (unknown.status_code, error_code(unknown)) == (400, "InvalidArgument")
    unmodeled = signed(s3_mock, "GET", f"/{BUCKET}/k", headers={"range": "bytes=0-1"})
    assert (unmodeled.status_code, error_code(unmodeled)) == (501, "NotImplemented")
    v1 = signed(s3_mock, "GET", f"/{BUCKET}")
    assert error_code(v1) == "NotImplemented"
    missing = signed(s3_mock, "GET", "/no-such-bucket/k")
    assert (missing.status_code, error_code(missing)) == (404, "NoSuchBucket")
    with pytest.raises(s3.S3Error) as caught:
        client_for(s3_mock).get("absent", io.BytesIO())
    assert (caught.value.status, caught.value.code) == (404, "NoSuchKey")
    # no secret and no signature in what the mock records
    log = json.dumps(requests_of(s3_mock))
    assert SECRET not in log and "Signature=" not in log


def test_client_pages_through_a_long_listing(s3_mock):
    objects = s3_mock.RequestHandlerClass.state.buckets[BUCKET]
    for i in range(1005):
        objects[f"p/{i:05d}"] = mock.StoredObject(b"x", '"e"', time.time(), "binary/octet-stream")
    listed = client_for(s3_mock).list("p/")
    assert [item["key"] for item in listed] == sorted(objects)
    assert sum(r["operation"] == "ListObjectsV2" for r in requests_of(s3_mock)) == 2
    client_for(s3_mock).delete("p/00000")
    assert "p/00000" not in objects


# -- backups and the archive on S3 --------------------------------------------------------

def s3_workflow(server, keep: int = 2):
    return parse_workflow(
        "---\ntracker: {kind: memory}\n"
        f"backup: {{keep: {keep}, s3: {{endpoint: '{endpoint(server)}', bucket: {BUCKET}, "
        "prefix: team/symphony}}\n---\nWork.\n"
    )


ENV = {backup.ACCESS_KEY_ENV: ACCESS, backup.SECRET_KEY_ENV: SECRET}


def test_nightly_run_to_s3_then_restore(redis_client, s3_mock):
    cfg = s3_workflow(s3_mock)
    redis_client.set(f"sym:{NS}:teamconfig", '{"generation": 4}')
    redis_client.hset(f"sym:{NS}:tokens", "t1", "{}")
    sessions = SessionStore(redis_client, NS)
    sessions.open("iss-1:1", kind="implement", identifier="ENG-1")
    sessions.close("iss-1:1", "succeeded")
    now = time.time_ns() // 1_000_000
    for step in range(3):
        result = backup.run(redis_client, NS, cfg, ENV, now_ms=now + step * 1000)
    assert result["target"] == {"kind": "s3", **cfg.backup_s3}
    keys = [o["key"] for o in httpx.get(f"{endpoint(s3_mock)}/_mock/objects").json()]
    assert all(k.startswith(f"team/symphony/{NS}/") for k in keys)
    target = backup.target(cfg, NS, ENV)
    names = [item["name"] for item in target.list(backup.BACKUPS)]
    assert len(names) == 2 and names[-1] == result["backup"]["name"]
    [archived] = target.list(backup.ARCHIVE)  # later runs found nothing new
    out = io.BytesIO()
    backup.read_archive(target, archived["name"], out)
    assert b'"sid":"iss-1:1"' in gzip.decompress(out.getvalue())

    redis_client.delete(f"sym:{NS}:teamconfig", f"sym:{NS}:tokens")
    report = backup.restore_from(redis_client, NS, target, names[-1], confirm=True)
    assert report["failed"] == 0
    assert redis_client.get(f"sym:{NS}:teamconfig") == '{"generation": 4}'
    assert redis_client.hget(f"sym:{NS}:tokens", "t1") == "{}"


def test_s3_without_access_keys_is_not_configured(redis_client, s3_mock):
    with pytest.raises(backup.BackupError) as caught:
        backup.run(redis_client, NS, s3_workflow(s3_mock), {})
    assert caught.value.code == "not_configured"
    assert backup.SECRET_KEY_ENV in caught.value.message
    doc = backup.status(redis_client, NS, s3_workflow(s3_mock), ENV)
    assert doc["target"]["kind"] == "s3" and SECRET not in json.dumps(doc)
