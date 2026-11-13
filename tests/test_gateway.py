"""Sandbox gateway as a whole (internals §9): the only service a workspace reaches, with
traces behind the attempt's own bearer and dispatch fencing, and the MCP proxy
(tests/test_gateway_mcp.py). No credential of any kind is served to a workspace
(internals §3, §13)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests.conftest import FakeKV
from tests.test_telemetry import MS, payload, tool

from symphony import state
from symphony.gateway_api import build_app

ISSUE = "issue-1"
TOKEN = "tok-abcdef0123456789"
SEQ = 3


@pytest.fixture
def gateway():
    kv = FakeKV()
    kv.put(
        state.issue_key(ISSUE),
        {"status": "running", "dispatch_seq": SEQ, "bridge_token": TOKEN, "provider": "anthropic"},
    )
    kv.put(state.dispatch_key(ISSUE, SEQ), {"daemon_owner": "d1"})
    return SimpleNamespace(client=TestClient(build_app(lambda: kv)), kv=kv)


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _traces(gw, seq=SEQ, token=TOKEN):
    return gw.client.post("/v1/traces", json=payload([tool("bash", 0, MS)], ISSUE, seq),
                          headers=_auth(token))


def test_the_gateway_serves_exactly_traces_and_the_mcp_proxy():
    """The tracker tool route is gone (`linear_graphql` is a runner host tool) and so is the
    provider-token route (models are reached by API key staged per attempt)."""
    routes = {
        route.path
        for route in build_app(lambda: FakeKV(), namespace="t").routes
        if route.path.startswith(("/v1", "/api"))
    }
    assert routes == {"/v1/traces", "/v1/mcp/{name}", "/v1/mcp/{name}/{rest:path}",
                      "/v1/mcp/{name}/http/{path:path}"}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/tool", {"issue_id": ISSUE, "seq": SEQ, "tool_call_id": "c",
                          "name": "linear_graphql", "arguments": {"query": "mutation X"}}),
        ("/api/v1/provider-token", {"issue_id": ISSUE, "seq": SEQ}),
    ],
)
def test_retired_credential_routes_are_not_found(gateway, path, body):
    assert gateway.client.post(path, json=body, headers=_auth()).status_code == 404


def test_the_attempt_bearer_authorizes_traces(gateway):
    assert _traces(gateway).status_code == 200
    assert gateway.kv.get(state.telemetry_key(ISSUE)) is not None


def test_a_superseded_dispatch_is_fenced(gateway):
    """A sandbox from an older dispatch still holds a once-valid bearer; it may not act."""
    entry = gateway.kv.get(state.issue_key(ISSUE))
    gateway.kv.put(state.issue_key(ISSUE), entry | {"dispatch_seq": SEQ + 1})
    assert _traces(gateway, seq=SEQ).status_code == 409
    assert gateway.kv.get(state.telemetry_key(ISSUE)) is None


def test_a_rotated_bearer_is_refused(gateway):
    entry = gateway.kv.get(state.issue_key(ISSUE))
    gateway.kv.put(state.issue_key(ISSUE), entry | {"bridge_token": "successor"})
    assert _traces(gateway).status_code == 401


def test_workspace_pod_carries_no_host_credential_and_no_api_token():
    """The agent receives only staged credentials, never a Secret binding or API token."""
    from symphony.config import Config
    from symphony.kube import _workspace_manifest

    manifest = _workspace_manifest(Config(tracker_kind="memory"), "ws-x", "img")
    spec = manifest["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert not spec["containers"][0].get("envFrom")
    dumped = str(manifest)
    assert "linear" not in dumped and "github" not in dumped and "provider" not in dumped
