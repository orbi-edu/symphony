"""Bearer tokens, role gates, rate limits and the uniform error shape."""

from __future__ import annotations

import pytest
from tests.api_support import bearer, client_for, make_services, tokens_for

from symphony.admin_auth import authorize_command


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client)
    return services, client_for(services), tokens_for(services)


def test_me_reports_the_token_identity(api):
    services, client, tokens = api
    response = client.get("/v1/me", headers=bearer(tokens["operator"]))
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "operator-person" and body["role"] == "operator"
    assert tokens["operator"].startswith(f"sym_{body['token_id']}_")


@pytest.mark.parametrize(
    "headers,code",
    [
        ({}, "unauthorized"),
        ({"Authorization": "Basic abc"}, "unauthorized"),
        ({"Authorization": "Bearer sym_deadbeef_" + "x" * 43}, "invalid_token"),
        ({"Authorization": "Bearer not-a-token"}, "invalid_token"),
    ],
)
def test_missing_or_invalid_token_is_401_with_challenge(api, headers, code):
    _, client, _ = api
    response = client.get("/v1/overview", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": code, "message": response.json()["error"]["message"]}}
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_revoked_token_fails_on_its_next_request(api):
    services, client, tokens = api
    assert client.get("/v1/me", headers=bearer(tokens["viewer"])).status_code == 200
    token_id = tokens["viewer"].split("_")[1]
    services.tokens.revoke(token_id, actor="admin-person")
    response = client.get("/v1/me", headers=bearer(tokens["viewer"]))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_expired_token_is_rejected(redis_client):
    now = [1_700_000_000_000]
    services = make_services(redis_client, clock=lambda: now[0])
    client = client_for(services)
    token, _ = services.tokens.create("temp", "viewer", actor="t", expires_in_s=60)
    assert client.get("/v1/me", headers=bearer(token)).status_code == 200
    now[0] += 61_000
    assert client.get("/v1/me", headers=bearer(token)).status_code == 401


def test_bootstrap_token_is_an_admin(redis_client):
    services = make_services(redis_client)
    token = "sym_0badc0de_" + "B" * 43
    services.tokens.ensure_bootstrap(token)
    response = client_for(services).get("/v1/admin/tokens", headers=bearer(token))
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["bootstrap"]


@pytest.mark.parametrize(
    "method,path,body,minimum",
    [
        ("post", "/v1/sessions/s1/messages", {"text": "hi"}, "operator"),
        ("post", "/v1/sessions/s1/interrupt", {"reason": "stop"}, "operator"),
        ("post", "/v1/commands", {"action": "pause", "reason": "maintenance window"}, "operator"),
        ("post", "/v1/merger/prs/3/requeue", {"reason": "flaky"}, "operator"),
        ("get", "/v1/admin/tokens", None, "admin"),
        ("put", "/v1/admin/config", {"reason": "x", "models": {}}, "admin"),
        ("get", "/v1/admin/providers", None, "admin"),
    ],
)
def test_role_gates(api, method, path, body, minimum):
    _, client, tokens = api
    below = {"operator": ["viewer"], "admin": ["viewer", "operator"]}[minimum]
    for role in below:
        kwargs = {"headers": bearer(tokens[role])}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403, (role, path)
        assert response.json()["error"]["code"] == "forbidden"


def test_per_token_rate_limit_returns_429_with_retry_after(redis_client):
    services = make_services(redis_client)
    client = client_for(services, per_minute=3)
    tokens = tokens_for(services)
    statuses = [client.get("/v1/me", headers=bearer(tokens["viewer"])).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    limited = client.get("/v1/me", headers=bearer(tokens["viewer"]))
    assert limited.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    # Another person's token has its own budget.
    assert client.get("/v1/me", headers=bearer(tokens["admin"])).status_code == 200


def test_failed_authentications_are_rate_limited_per_address(api):
    _, client, tokens = api
    bad = {"Authorization": "Bearer sym_deadbeef_" + "x" * 43}
    statuses = {client.get("/v1/me", headers=bad).status_code for _ in range(31)}
    assert statuses == {401, 429}
    assert client.get("/v1/me", headers=bad).status_code == 429


def test_unknown_route_uses_the_uniform_error_shape(api):
    _, client, tokens = api
    response = client.get("/v1/nope", headers=bearer(tokens["viewer"]))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_validation_errors_never_echo_the_input(api):
    _, client, tokens = api
    secret = "sk-ant-very-secret-value-1234567890"
    response = client.post(
        "/v1/admin/tokens", headers=bearer(tokens["admin"]), json={"name": "x", "role": secret}
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_health_is_public(api):
    _, client, _ = api
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_daemon_fence_follows_token_revocation(api):
    services, _, tokens = api
    token_id = tokens["operator"].split("_")[1]
    command = {"action": "pause", "credentialFingerprint": token_id}
    assert authorize_command(command, "test", services.tokens)
    assert not authorize_command({**command, "action": "workflow"}, "test", services.tokens)
    services.tokens.revoke(token_id, actor="admin-person")
    assert not authorize_command(command, "test", services.tokens)
