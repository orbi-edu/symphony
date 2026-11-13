"""Team configuration, the omp catalog and API tokens."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.api_support import bearer, client_for, make_services, tokens_for

from symphony.teamconfig import ConflictError

CATALOG = Path(__file__).parent / "fixtures" / "omp-catalog" / "models.json"
SWAP = {"models": {"implementer": {"provider": "anthropic", "model": "claude-x", "thinking": "high"}}}


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client, catalog_path=CATALOG)
    return services, client_for(services), tokens_for(services)


def test_config_publish_needs_if_match_reason_and_maps_conflicts(api, monkeypatch):
    services, client, tokens = api
    admin = bearer(tokens["admin"])
    read = client.get("/v1/admin/config", headers=bearer(tokens["viewer"]))
    assert read.status_code == 200 and read.headers["ETag"] == '"0"'
    assert read.json()["generation"] == 0
    body = SWAP | {"reason": "try the new model"}
    assert client.put("/v1/admin/config", headers=admin, json=body).status_code == 428
    assert client.put("/v1/admin/config", headers=admin | {"If-Match": "abc"},
                      json=body).status_code == 422
    no_reason = client.put("/v1/admin/config", headers=admin | {"If-Match": "0"}, json=SWAP)
    assert no_reason.status_code == 422
    published = client.put("/v1/admin/config", headers=admin | {"If-Match": '"0"'}, json=body)
    assert published.status_code == 200
    assert published.headers["ETag"] == '"1"'
    doc = published.json()
    assert doc["generation"] == 1 and doc["updated_by"] == "admin-person"
    assert doc["models"]["implementer"]["model"] == "claude-x"
    assert doc["models"]["reviewer"] == services.teamconfig.get()["models"]["reviewer"]
    stale = client.put("/v1/admin/config", headers=admin | {"If-Match": "0"}, json=body)
    assert stale.status_code == 412
    invalid = client.put("/v1/admin/config", headers=admin | {"If-Match": "1"}, json={
        "models": {"oracle": dict(doc["models"]["reviewer"])}, "reason": "same as reviewer"})
    assert invalid.status_code == 422 and "differ" in invalid.json()["error"]["message"]

    def racing(*args, **kwargs):
        raise ConflictError("configuration changed concurrently; reload and retry")

    monkeypatch.setattr(services.teamconfig, "publish", racing)
    raced = client.put("/v1/admin/config", headers=admin | {"If-Match": "1"}, json=body)
    assert raced.status_code == 409 and raced.json()["error"]["code"] == "conflict"
    audit = client.get("/v1/audit", headers=admin).json()["items"]
    assert any(e["action"] == "teamconfig.publish" and e["target"] == "generation:1" for e in audit)


def test_observed_generations_include_members(api):
    services, client, tokens = api
    services.teamconfig.observe("daemon", 0)
    response = client.post("/v1/admin/config/observed", headers=bearer(tokens["viewer"]),
                           json={"generation": 0})
    assert response.status_code == 204
    observed = client.get("/v1/admin/config/observed", headers=bearer(tokens["viewer"])).json()
    assert set(observed) == {"daemon", "member:viewer-person"}
    assert observed["member:viewer-person"]["generation"] == 0


def test_tokens_are_shown_once_and_revocation_is_immediate(api):
    services, client, tokens = api
    admin = bearer(tokens["admin"])
    created = client.post("/v1/admin/tokens", headers=admin,
                          json={"name": "alice", "role": "operator", "expires_days": 30})
    assert created.status_code == 201
    body = created.json()
    token = body["token"]
    assert token.startswith(f"sym_{body['id']}_") and body["role"] == "operator"
    assert body["expires_at"] - body["created_at"] == 30 * 86400 * 1000
    assert created.headers["Cache-Control"] == "no-store"
    listing = client.get("/v1/admin/tokens", headers=admin)
    secret = token.split("_", 2)[2]
    assert secret not in listing.text and "hash" not in listing.text
    assert "alice" in [item["name"] for item in listing.json()["items"]]
    assert client.get("/v1/me", headers=bearer(token)).json()["name"] == "alice"
    revoked = client.delete(f"/v1/admin/tokens/{body['id']}", headers=admin)
    assert revoked.status_code == 200 and revoked.json()["revoked_by"] == "admin-person"
    assert client.get("/v1/me", headers=bearer(token)).status_code == 401
    assert client.delete("/v1/admin/tokens/ffffffff", headers=admin).status_code == 404
    bad = client.post("/v1/admin/tokens", headers=admin, json={"name": "x", "role": "root"})
    assert bad.status_code == 422
    actions = [e["action"] for e in client.get("/v1/audit", headers=admin).json()["items"]]
    assert {"token.create", "token.revoke"} <= set(actions)


def test_catalog_is_decoded_from_the_pinned_omp_database(api):
    _, client, tokens = api
    headers = bearer(tokens["viewer"])
    response = client.get("/v1/admin/catalog", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "18.20.2"
    # OAuth-only (`codexy`: an OAuth token; `copiloty`: a hook-renewed session) and rule-less
    # (`unruled`) providers are left out; `zeta` has an optional OAuth login beside its API key
    # and stays (internals §13).
    assert [(p["id"], p["credential"]) for p in body["providers"]] == [
        ("acme", "apiKey"), ("hosty", "host"), ("zeta", "apiKey")]
    acme = body["providers"][0]["models"]
    assert [m["id"] for m in acme] == ["m1", "m2"]
    assert acme[0] == {
        "id": "m1", "name": "Model One", "api": "anthropic-messages",
        "baseUrl": "https://api.acme.test", "contextWindow": 200000, "maxTokens": None,
        "reasoning": True, "input": ["text", "image"],
        "cost": {"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 1.25}, "thinking": None,
    }
    assert acme[1]["reasoning"] is False
    assert body["providers"][2]["models"][0]["name"] == "Zed"
    cached = client.get("/v1/admin/catalog", headers=headers | {"If-None-Match": response.headers["ETag"]})
    assert cached.status_code == 304
    one = client.get("/v1/admin/catalog?provider=zeta", headers=headers).json()
    assert [p["id"] for p in one["providers"]] == ["zeta"]
    assert client.get("/v1/admin/catalog?provider=nope", headers=headers).status_code == 404
    for hidden in ("codexy", "copiloty"):
        assert client.get(f"/v1/admin/catalog?provider={hidden}", headers=headers).status_code == 404


def test_missing_catalog_is_503(redis_client):
    services = make_services(redis_client)
    tokens = tokens_for(services)
    response = client_for(services).get("/v1/admin/catalog", headers=bearer(tokens["viewer"]))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "catalog_unavailable"


@pytest.mark.parametrize("method,path", [
    ("get", "/v1/admin/providers/anthropic/subscription"),
    ("put", "/v1/admin/providers/anthropic/subscription"),
    ("get", "/v1/admin/providers/openai-codex/oauth"),
    ("post", "/v1/admin/providers/openai-codex/oauth/login"),
])
def test_there_is_no_oauth_or_subscription_route(api, method, path):
    """Every provider is configured by API key (internals §13)."""
    _, client, tokens = api
    response = getattr(client, method)(path, headers=bearer(tokens["admin"]))
    assert response.status_code in (404, 405)
