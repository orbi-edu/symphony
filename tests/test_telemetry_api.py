"""Sandbox gateway `POST /v1/traces`: authorization, fencing, and the promise that nothing
here can affect a run."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from tests.conftest import FakeKV
from tests.test_telemetry import MS, payload, tool

from symphony import state
from symphony.gateway_api import build_app

ISSUE = "issue-1"
TOKEN = "tok-abcdef0123456789"
SEQ = 3


@pytest.fixture
def env():
    kv = FakeKV()
    kv.put(state.issue_key(ISSUE), {"status": "running", "dispatch_seq": SEQ, "bridge_token": TOKEN})
    return TestClient(build_app(lambda: kv)), kv


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _spans(n=1):
    return [tool("bash", 0, MS) for _ in range(n)]


def _kv(key, value):
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _protobuf_export(issue_id=ISSUE, seq=SEQ) -> bytes:
    """What OMP's `exporter-trace-otlp-proto` sends: a root agent loop, one chat call, and one
    failed tool call under it."""
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.extend(
        [_kv("symphony.issue_id", issue_id), _kv("symphony.dispatch_seq", seq)]
    )
    root = b"\x01" * 8
    resource_spans.scope_spans.add().spans.extend([
        Span(name="invoke_agent", span_id=root, start_time_unix_nano=0,
             end_time_unix_nano=9_000 * MS,
             attributes=[_kv("gen_ai.operation.name", "invoke_agent"),
                         _kv("omp.gen_ai.agent.step.count", 2)]),
        Span(name="chat claude-opus-5-5", parent_span_id=root, start_time_unix_nano=0,
             end_time_unix_nano=3_000 * MS,
             attributes=[_kv("gen_ai.operation.name", "chat"),
                         _kv("gen_ai.request.model", "claude-opus-5-5"),
                         _kv("gen_ai.usage.input_tokens", 1234),
                         _kv("gen_ai.usage.output_tokens", 56)]),
        Span(name="execute_tool bash", parent_span_id=root, start_time_unix_nano=0,
             end_time_unix_nano=2_000 * MS,
             status=Status(code=Status.STATUS_CODE_ERROR),
             attributes=[_kv("gen_ai.operation.name", "execute_tool"),
                         _kv("gen_ai.tool.name", "bash")]),
    ])
    return request.SerializeToString()


def _post_protobuf(client, body, **headers):
    return client.post(
        "/v1/traces",
        content=body,
        headers={"Content-Type": "application/x-protobuf", **headers},
    )


def rollup(kv):
    return kv.get(state.telemetry_key(ISSUE))


def test_an_authorized_batch_is_folded(env):
    client, kv = env
    resp = client.post("/v1/traces", json=payload(_spans(2), ISSUE, SEQ), headers=_auth())
    assert resp.status_code == 200
    assert "partialSuccess" in resp.json()  # the shape a conforming exporter reads as delivered
    assert rollup(kv)["tool_calls"] == 2


def test_the_engines_protobuf_export_is_folded(env):
    """OMP's native exporter speaks OTLP/HTTP protobuf only; JSON is not an option it has."""
    client, kv = env
    resp = _post_protobuf(client, _protobuf_export(), **_auth())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-protobuf"
    roll = rollup(kv)
    assert (roll["interactions"], roll["turns"], roll["llm_requests"]) == (1, 2, 1)
    assert roll["llm_ms"] == 3_000
    assert roll["by_model"]["claude-opus-5-5"] == {"requests": 1, "input": 1234, "output": 56}
    assert roll["by_tool"]["bash"] == {"calls": 1, "ms": 2_000, "errors": 1, "max_ms": 2_000}


def test_a_protobuf_export_is_fenced_like_any_other(env):
    client, kv = env
    assert _post_protobuf(client, _protobuf_export(seq=SEQ - 1), **_auth()).status_code == 409
    assert _post_protobuf(client, _protobuf_export()).status_code == 401
    assert rollup(kv) is None


def test_a_missing_or_wrong_token_is_refused_and_writes_nothing(env):
    client, kv = env
    body = payload(_spans(), ISSUE, SEQ)
    assert client.post("/v1/traces", json=body).status_code == 401
    assert client.post("/v1/traces", json=body, headers=_auth("wrong")).status_code == 401
    assert rollup(kv) is None


def test_a_superseded_attempt_cannot_contaminate_the_rollup(env):
    """A sandbox from an earlier dispatch still holds a once-valid token. Its spans belong
    to a run that no longer owns the issue."""
    client, kv = env
    resp = client.post("/v1/traces", json=payload(_spans(), ISSUE, SEQ - 1), headers=_auth())
    assert resp.status_code == 409
    assert rollup(kv) is None


def test_spans_arriving_after_the_attempt_finished_are_refused(env):
    """The runner clears `bridge_token` on finish, so late spans land on a cleared entry
    rather than merging into a stale rollup."""
    client, kv = env
    entry = kv.get(state.issue_key(ISSUE))
    entry["bridge_token"] = None
    kv.put(state.issue_key(ISSUE), entry)
    assert client.post("/v1/traces", json=payload(_spans(), ISSUE, SEQ),
                       headers=_auth()).status_code == 401
    assert rollup(kv) is None


def test_an_unknown_issue_is_unauthorized_not_a_new_entry(env):
    client, kv = env
    assert client.post("/v1/traces", json=payload(_spans(), "ghost", 1),
                       headers=_auth()).status_code == 401
    assert kv.get(state.telemetry_key("ghost")) is None


def test_an_unattributed_payload_is_rejected(env):
    """OTLP carries no field of ours; resource attributes are the only identification. A
    payload without them could only be filed by guessing."""
    client, kv = env
    resp = client.post("/v1/traces", json=payload(_spans(), issue_id=None), headers=_auth())
    assert resp.status_code == 400
    assert rollup(kv) is None


def test_a_malformed_body_is_a_client_error_not_a_500(env):
    client, _ = env
    assert client.post("/v1/traces", content=b"not json", headers=_auth()).status_code == 400
    assert client.post("/v1/traces", json=[1, 2, 3], headers=_auth()).status_code == 400


def test_the_endpoint_never_writes_to_the_issue_entry(env):
    """INV-1. The issue entry is the runner's to write; a second writer on it would race
    the token ledger, which is the one thing here that must not be approximate."""
    client, kv = env
    before = dict(kv.get(state.issue_key(ISSUE)))
    client.post("/v1/traces", json=payload(_spans(5), ISSUE, SEQ), headers=_auth())
    assert kv.get(state.issue_key(ISSUE)) == before


def test_unavailable_state_is_503_never_a_silent_success(env):
    client, _ = env

    class Broken:
        def get(self, *a, **k):
            raise RuntimeError("state down")

        def put(self, *a, **k):
            raise RuntimeError("state down")

    broken = TestClient(build_app(lambda: Broken()))
    resp = broken.post("/v1/traces", json=payload(_spans(), ISSUE, SEQ), headers=_auth())
    assert resp.status_code == 503
