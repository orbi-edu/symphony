"""Credential isolation, named-Secret CAS, and provider HTTP authority regressions."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from tests.api_support import bearer, client_for, make_services, tokens_for

from symphony.providers import ProviderError, ProviderRepository

KEY = "dummy-provider-credential-not-a-real-key"
OTHER_KEY = "dummy-other-provider-credential-not-a-real-key"
CATALOG = Path(__file__).parent / "fixtures" / "omp-catalog" / "models.json"


def definition(**changes):
    return {
        "name": "Managed provider",
        "api": "anthropic-messages",
        "baseUrl": "https://api.anthropic.com",
        "auth": "apiKey",
        "models": [{"id": "model-one"}],
        "apiKey": KEY,
    } | changes


def document(secret):
    return json.loads(base64.b64decode(secret.data["providers.json"]))


def encoded(providers):
    return base64.b64encode(
        json.dumps({"schemaVersion": 1, "providers": providers}).encode()
    ).decode()


class SecretCore:
    def __init__(self, providers=None):
        self.secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name="symphony-providers",
                namespace="symphony",
                resource_version="1",
                labels={"kept": "label"},
                annotations={"kept": "annotation"},
            ),
            data={"providers.json": encoded(providers or {}), "unrelated": "a2VwdA=="},
        )
        self.read_error = None
        self.on_read = None
        self.on_replace = None
        self.writes = 0

    def read_namespaced_secret(self, **kwargs):
        if self.read_error is not None:
            raise self.read_error
        result = copy.deepcopy(self.secret)
        if self.on_read is not None:
            callback, self.on_read = self.on_read, None
            callback()
        return result

    def replace_namespaced_secret(self, *, body, **kwargs):
        self.writes += 1
        if self.on_replace is not None:
            self.on_replace()
        if body.metadata.resource_version != self.secret.metadata.resource_version:
            raise ApiException(status=409, reason=KEY)
        body.metadata.resource_version = str(int(body.metadata.resource_version) + 1)
        self.secret = copy.deepcopy(body)
        return copy.deepcopy(body)


def test_write_only_projection_and_selected_runtime_preserve_metadata():
    core = SecretCore({"other": definition(apiKey=OTHER_KEY)})
    repository = ProviderRepository(core, "symphony")
    result = repository.save(
        "anthropic",
        definition(
            models=[
                {"id": "model-one"},
                {"id": "model-two", "contextWindow": 32000, "maxTokens": 4000},
            ]
        ),
        "1",
    )
    assert KEY not in json.dumps(result) and OTHER_KEY not in json.dumps(result)
    assert all("apiKey" not in item and item["credentialConfigured"] for item in result["items"])
    runtime = repository.runtime("anthropic", "model-two")
    assert runtime.environment == {"SYMPHONY_PROVIDER_API_KEY": KEY}
    assert runtime.models_config == {
        "providers": {
            "anthropic": {
                "api": "anthropic-messages",
                "baseUrl": "https://api.anthropic.com",
                "auth": "apiKey",
                "apiKey": "SYMPHONY_PROVIDER_API_KEY",
                "models": [{"id": "model-two", "contextWindow": 32000, "maxTokens": 4000}],
            }
        }
    }
    assert KEY not in repr(runtime)
    assert core.secret.data["unrelated"] == "a2VwdA=="
    assert core.secret.metadata.labels == {"kept": "label"}
    assert core.secret.metadata.annotations["kept"] == "annotation"
    assert document(core.secret)["providers"]["other"] == definition(apiKey=OTHER_KEY)
    assert repository.runtime("host-managed", "any-model") is None
    with pytest.raises(ProviderError, match="workflow-selected model"):
        repository.runtime("anthropic", "unconfigured-model")


def test_omitted_key_keeps_replacement_rotates_and_no_auth_clears():
    core = SecretCore({"custom": definition()})
    repository = ProviderRepository(core, "symphony")
    edit = definition(name="Renamed")
    del edit["apiKey"]
    repository.save("custom", edit, "1")
    assert repository.runtime("custom", "model-one").environment == {
        "SYMPHONY_PROVIDER_API_KEY": KEY
    }
    repository.save("custom", edit | {"apiKey": OTHER_KEY}, "2")
    assert repository.runtime("custom", "model-one").environment == {
        "SYMPHONY_PROVIDER_API_KEY": OTHER_KEY
    }
    public = repository.save("custom", edit | {"auth": "none", "api": "openai-completions"}, "3")
    assert public["items"][0]["credentialConfigured"] is False
    assert "apiKey" not in document(core.secret)["providers"]["custom"]
    runtime = repository.runtime("custom", "model-one")
    assert runtime.environment == {}
    assert "apiKey" not in runtime.models_config["providers"]["custom"]
    with pytest.raises(ProviderError, match="API key is required"):
        repository.save("custom", edit, "4")


def test_stale_write_and_racing_rotation_preserve_credentials():
    core = SecretCore({"custom": definition()})
    repository = ProviderRepository(core, "symphony")
    with pytest.raises(ProviderError) as stale:
        repository.save("custom", definition(apiKey=OTHER_KEY), "old")
    assert stale.value.status_code == 412
    assert core.writes == 0

    def concurrent_rotation():
        core.secret.metadata.resource_version = "2"
        core.secret.data["providers.json"] = encoded({"custom": definition(apiKey=OTHER_KEY)})

    core.on_replace = concurrent_rotation
    with pytest.raises(ProviderError) as raced:
        repository.save("custom", definition(name="Stale edit"), "1")
    assert raced.value.status_code == 412
    assert repository.runtime("custom", "model-one").environment == {
        "SYMPHONY_PROVIDER_API_KEY": OTHER_KEY
    }


def test_missing_storage_is_host_managed_but_malformed_and_unavailable_fail_closed():
    core = SecretCore()
    repository = ProviderRepository(core, "symphony")
    core.read_error = ApiException(status=404)
    assert repository.runtime("custom", "model-one") is None
    assert repository.read()["storageReady"] is False
    with pytest.raises(ProviderError) as missing:
        repository.save("custom", definition(), "1")
    assert missing.value.code == "providers_not_bootstrapped"
    core.read_error = RuntimeError(KEY)
    with pytest.raises(ProviderError) as unavailable:
        repository.runtime("host-managed", "anything")
    assert KEY not in str(unavailable.value)
    core.read_error = None
    core.secret.data["providers.json"] = encoded(
        {"custom": definition(headers={"Authorization": KEY})}
    )
    with pytest.raises(ProviderError) as malformed:
        repository.runtime("host-managed", "anything")
    assert malformed.value.code == "providers_invalid"


def test_empty_bootstrapped_secret_can_accept_first_provider():
    core = SecretCore()
    core.secret.data = None
    repository = ProviderRepository(core, "symphony")
    assert repository.read()["storageReady"] is True
    assert repository.save("custom", definition(), "1")["revision"] == "2"


@pytest.mark.parametrize(
    "changes",
    [
        {"headers": {"Authorization": KEY}},
        {"apiKey": None},
        {"apiKey": "bad\nkey"},
        {"auth": "oauth"},
        {"auth": "none"},
        {"baseUrl": "https://user:password@host/v1"},
        {"baseUrl": "https://host/v1?api_key=secret"},
        {"baseUrl": "!command"},
        {"models": [{"id": "model", "headers": {"Authorization": KEY}}]},
        {"models": [{"id": "model", "reasoning": "true"}]},
        {"models": [{"id": "model", "maxTokens": 0}]},
        {"models": [{"id": "model", "contextWindow": None}]},
        {"models": [{"id": "model", "cost": {"input": 0}}]},
        {"models": [{"id": "model"}, {"id": "model"}]},
    ],
)
def test_untrusted_provider_fields_are_rejected_without_key_disclosure(changes):
    core = SecretCore()
    with pytest.raises(ProviderError) as error:
        ProviderRepository(core, "symphony").save("custom", definition(**changes), "1")
    assert error.value.status_code == 422
    assert KEY not in str(error.value)
    assert core.writes == 0


def test_lost_ack_is_confirmed_only_by_readback_of_our_write():
    core = SecretCore()
    original = core.replace_namespaced_secret

    def lost_ack(**kwargs):
        original(**kwargs)
        raise TimeoutError(KEY)

    core.replace_namespaced_secret = lost_ack
    repository = ProviderRepository(core, "symphony")
    assert repository.save("custom", definition(), "1")["revision"] == "2"
    assert core.writes == 1

    def unavailable_ack(**kwargs):
        original(**kwargs)
        core.read_error = TimeoutError(KEY)
        raise TimeoutError(KEY)

    core.replace_namespaced_secret = unavailable_ack
    with pytest.raises(ProviderError) as unknown:
        repository.save("custom", definition(apiKey=OTHER_KEY), "2")
    assert unknown.value.code == "providers_outcome_unknown"
    assert KEY not in str(unknown.value)
    assert core.writes == 2


@pytest.fixture
def provider_api(redis_client):
    core = SecretCore()
    repository = ProviderRepository(core, "symphony")
    services = make_services(redis_client, providers=repository, catalog_path=CATALOG)
    tokens = tokens_for(services)
    return SimpleNamespace(
        client=client_for(services), services=services, store=services.store, core=core,
        providers=repository, tokens=tokens, admin=bearer(tokens["admin"]),
    )


PATH = "/v1/admin/providers"


def test_provider_http_writes_never_enqueue_or_disclose_keys(provider_api):
    api = provider_api
    response = api.client.put(PATH + "/custom", headers=api.admin | {"If-Match": '"1"'},
                              json=definition())
    assert response.status_code == 200
    assert response.headers["ETag"] == '"2"'
    assert KEY not in response.text
    assert response.json()["items"][0]["credentialConfigured"] is True
    listing = api.client.get(PATH, headers=api.admin)
    assert listing.status_code == 200 and KEY not in listing.text
    assert listing.headers["ETag"] == '"2"'
    events = api.client.get("/v1/audit", headers=api.admin).json()["items"]
    assert {item["result"] for item in events} == {"attempted", "succeeded"}
    assert all(item["commandId"] is None and item["actor"] == "admin-person" for item in events)
    private_store = api.store.client.hgetall(api.store.key)
    assert KEY not in json.dumps(private_store)
    assert not any(key.startswith(("command:", "inbox:", "idem:")) for key in private_store)


def test_missing_catalog_does_not_disable_custom_provider_editing(redis_client):
    core = SecretCore()
    services = make_services(redis_client, providers=ProviderRepository(core, "symphony"),
                             catalog_path="/nonexistent/models.json")
    client, admin = client_for(services), bearer(tokens_for(services)["admin"])
    catalog = client.get("/v1/admin/catalog", headers=admin)
    assert catalog.status_code == 503
    assert catalog.json()["error"]["code"] == "catalog_unavailable"
    response = client.put(PATH + "/custom", headers=admin | {"If-Match": '"1"'},
                          json=definition())
    assert response.status_code == 200


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_provider_routes_are_admin_only(provider_api, role):
    api = provider_api
    headers = bearer(api.tokens[role]) | {"If-Match": '"1"', "X-Role": "admin"}
    assert api.client.get(PATH, headers=headers).status_code == 403
    response = api.client.put(PATH + "/custom", headers=headers, json=definition())
    assert response.status_code == 403
    assert api.core.writes == 0


def test_provider_revocation_during_save_prevents_secret_mutation(provider_api):
    api = provider_api
    admin_id = api.tokens["admin"].split("_")[1]
    api.core.on_read = lambda: api.services.tokens.revoke(admin_id, actor="someone")
    response = api.client.put(PATH + "/custom", headers=api.admin | {"If-Match": '"1"'},
                              json=definition())
    assert response.status_code == 401
    assert api.core.writes == 0
    assert {item["result"] for item in api.store.audit()["items"]} == {"attempted", "failed"}


def test_provider_audit_unavailability_prevents_secret_mutation(provider_api):
    api = provider_api
    api.store.client.set(api.store.key, "unavailable")
    response = api.client.put(PATH + "/custom", headers=api.admin | {"If-Match": '"1"'},
                              json=definition())
    assert response.status_code == 503
    assert api.core.writes == 0
    assert KEY not in response.text


def test_provider_preconditions_and_stale_key_preservation(provider_api):
    api = provider_api
    body = definition()
    assert api.client.put(PATH + "/custom", headers=api.admin, json=body).status_code == 428
    assert api.client.put(PATH + "/custom", headers=api.admin | {"If-Match": '"1"'},
                          json=body).status_code == 200
    response = api.client.put(PATH + "/custom", headers=api.admin | {"If-Match": '"1"'},
                              json=body | {"apiKey": OTHER_KEY})
    assert response.status_code == 412
    assert OTHER_KEY not in response.text
    assert api.providers.runtime("custom", "model-one").environment == {
        "SYMPHONY_PROVIDER_API_KEY": KEY
    }


def test_provider_free_text_is_rejected_without_credential_disclosure(provider_api):
    api = provider_api
    response = api.client.put(
        PATH + "/custom",
        headers=api.admin | {"If-Match": '"1"'},
        json=definition() | {"reason": "Accidentally pasted credential " + KEY},
    )
    assert response.status_code == 422
    assert KEY not in response.text
    assert api.core.writes == 0
    assert api.store.audit()["items"] == []


def test_provider_unknown_outcome_is_audited_without_claiming_failure(provider_api):
    api = provider_api
    original = api.core.replace_namespaced_secret

    def lose_response(**kwargs):
        original(**kwargs)
        api.core.read_error = TimeoutError(KEY)
        raise TimeoutError(KEY)

    api.core.replace_namespaced_secret = lose_response
    response = api.client.put(PATH + "/custom", headers=api.admin | {"If-Match": '"1"'},
                              json=definition())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "providers_outcome_unknown"
    assert KEY not in response.text
    assert {item["result"] for item in api.store.audit()["items"]} == {"attempted", "unknown"}
    assert api.core.writes == 1


def test_subscription_protocols_are_gone():
    """No OAuth or subscription provider exists (internals §13): every model uses an API key."""
    core = SecretCore()
    profile = {
        "name": "ChatGPT",
        "api": "openai-codex-responses",
        "baseUrl": "https://chatgpt.com/backend-api",
        "auth": "oauth",
        "models": [{"id": "gpt-5.4"}],
    }
    with pytest.raises(ProviderError):
        ProviderRepository(core, "symphony").save("openai-codex", profile, "1")
    assert core.writes == 0


def put(api, provider_id, body, revision):
    return api.client.put(f"{PATH}/{provider_id}", headers=api.admin | {"If-Match": f'"{revision}"'},
                          json=body)


def test_builtin_key_is_write_only_and_audited_as_a_save(provider_api):
    api = provider_api
    response = put(api, "acme", {"builtin": True, "apiKey": KEY}, 1)
    assert response.status_code == 200
    assert KEY not in response.text
    assert response.json()["items"] == [
        {"id": "acme", "builtin": True, "credentialConfigured": True}
    ]
    listing = api.client.get(PATH, headers=api.admin)
    assert KEY not in listing.text
    assert listing.json()["items"] == response.json()["items"]
    stored = document(api.core.secret)["providers"]["acme"]
    assert stored == {"builtin": True, "apiKey": KEY}
    runtime = api.providers.runtime("acme", "m1")
    assert runtime.models_config == {"providers": {"acme": {"apiKey": "SYMPHONY_PROVIDER_API_KEY"}}}
    assert runtime.environment == {"SYMPHONY_PROVIDER_API_KEY": KEY}
    audit = api.client.get("/v1/audit", headers=api.admin)
    assert KEY not in audit.text
    assert {(e["action"], e["result"]) for e in audit.json()["items"]} == {
        ("provider.save", "attempted"), ("provider.save", "succeeded")}


def test_builtin_key_omitted_keeps_the_stored_one(provider_api):
    api = provider_api
    assert put(api, "acme", {"builtin": True, "apiKey": KEY}, 1).status_code == 200
    kept = put(api, "acme", {"builtin": True}, 2)
    assert kept.status_code == 200
    assert api.providers.runtime("acme", "m1").environment == {"SYMPHONY_PROVIDER_API_KEY": KEY}
    rotated = put(api, "acme", {"builtin": True, "apiKey": OTHER_KEY}, 3)
    assert rotated.status_code == 200 and OTHER_KEY not in rotated.text
    assert api.providers.runtime("acme", "m1").environment == {
        "SYMPHONY_PROVIDER_API_KEY": OTHER_KEY}


def test_builtin_without_any_key_is_provider_key_required(provider_api):
    api = provider_api
    response = put(api, "acme", {"builtin": True}, 1)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_key_required"
    assert api.core.writes == 0
    custom = put(api, "custom", {k: v for k, v in definition().items() if k != "apiKey"}, 1)
    assert custom.status_code == 422
    assert custom.json()["error"]["code"] == "provider_key_required"
    results = {(e["action"], e["result"]) for e in api.store.audit()["items"]}
    assert results == {("provider.save", "attempted"), ("provider.save", "failed")}


def test_builtin_key_needs_a_provider_the_pinned_omp_knows(provider_api):
    api = provider_api
    response = put(api, "not-in-omp", {"builtin": True, "apiKey": KEY}, 1)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_provider"
    assert KEY not in response.text
    assert api.core.writes == 0
    assert api.store.audit()["items"] == []
    # In the catalog but not usable with a key: OAuth-only (an OAuth token, or a hook-renewed
    # session like Copilot), host-credential and rule-less ids.
    for provider_id in ("codexy", "copiloty", "hosty", "unruled"):
        refused = put(api, provider_id, {"builtin": True, "apiKey": KEY}, 1)
        assert refused.json()["error"]["code"] == "unknown_provider", provider_id
    assert api.core.writes == 0
    extra = put(api, "acme", {"builtin": True, "apiKey": KEY, "baseUrl": "https://evil.test"}, 1)
    assert extra.status_code == 422 and KEY not in extra.text


def test_builtin_key_without_a_catalog_is_503(redis_client):
    core = SecretCore()
    services = make_services(redis_client, providers=ProviderRepository(core, "symphony"),
                             catalog_path="/nonexistent/models.json")
    tokens = tokens_for(services)
    response = client_for(services).put(
        f"{PATH}/acme", headers=bearer(tokens["admin"]) | {"If-Match": '"1"'},
        json={"builtin": True, "apiKey": KEY})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "catalog_unavailable"
    assert core.writes == 0
