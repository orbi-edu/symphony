"""`/v1/assistant/memory` (admin, audited) and `symphony assistant memory …` on the real API."""

from __future__ import annotations

import json

import pytest
from tests.api_support import Server, bearer, build_app, client_for, make_services, tokens_for

from symphony.assistant.memory import MemoryStore
from symphony.cli import main
from symphony.entry import assistant as entry


@pytest.fixture
def api(redis_client):
    services = make_services(redis_client)
    memory = MemoryStore(redis_client, services.namespace)
    memory.remember("alice", "deploys on Fridays", "explicit", "t1", 90)
    memory.remember("alice", "likes short answers", "inferred", "t2", 90)
    memory.remember("bob", "owns the billing service", "explicit", "t3", 90)
    entry.beat(redis_client, services.namespace, state="running",
               soul={"repo": "acme/app", "path": ".symphony/assistant.md", "sha": "abc123def456"})
    return services, tokens_for(services), memory


def audits(services, action):
    return [e for e in services.store.audit(limit=200)["items"] if e["action"] == action]


def test_admin_reads_and_removes_memory_and_every_call_is_audited(api):
    services, tokens, memory = api
    client = client_for(services)
    admin = bearer(tokens["admin"])
    for role in ("viewer", "operator"):
        assert client.get("/v1/assistant/memory", headers=bearer(tokens[role])).status_code == 403
        assert client.delete("/v1/assistant/memory/alice", headers=bearer(tokens[role])).status_code == 403
    doc = client.get("/v1/assistant/memory", headers=admin).json()
    assert doc["soul"] == {"repo": "acme/app", "path": ".symphony/assistant.md", "sha": "abc123def456"}
    assert [(p["login"], p["count"]) for p in doc["people"]] == [("alice", 2), ("bob", 1)]
    items = client.get("/v1/assistant/memory/Alice", headers=admin).json()
    assert items["login"] == "alice" and [i["text"] for i in items["items"]] == [
        "deploys on Fridays", "likes short answers"]
    first = items["items"][0]["id"]
    gone = client.delete(f"/v1/assistant/memory/alice/{first}", headers=admin)
    assert gone.status_code == 200 and gone.json()["removed"]["text"] == "deploys on Fridays"
    assert client.delete(f"/v1/assistant/memory/alice/{first}", headers=admin).status_code == 404
    assert client.get("/v1/assistant/memory/not a login", headers=admin).status_code in (404, 422)
    assert client.delete("/v1/assistant/memory/alice", headers=admin).json() == {"login": "alice", "removed": 1}
    assert memory.people() == [{"login": "bob", "count": 1, "updated_at": memory.items("bob")[0]["at"]}]
    assert [e["target"] for e in audits(services, "assistant.memory_read")] == ["memory:alice", "memory"]
    assert [e["target"] for e in audits(services, "assistant.memory_forget")] == [f"memory:alice:{first}"]
    assert [e["target"] for e in audits(services, "assistant.memory_wipe")] == ["memory:alice"]
    assert all(e["actor"] == "admin-person" for e in audits(services, "assistant.memory_read"))
    assert "Fridays" not in json.dumps(services.store.audit(limit=200))  # no memory text in the audit


def test_the_cli_lists_shows_forgets_and_wipes(api, capsys, monkeypatch, tmp_path):
    services, tokens, memory = api
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with Server(build_app(services)) as server:
        monkeypatch.setenv("SYMPHONY_URL", server.url)
        monkeypatch.setenv("SYMPHONY_TOKEN", tokens["admin"])

        def run(*argv):
            code = main.main(list(argv))
            out, err = capsys.readouterr()
            assert code == 0, err
            return out

        listed = run("assistant", "memory", "list")
        assert "Soul: acme/app:.symphony/assistant.md at abc123def456." in listed
        assert "alice" in listed and "bob" in listed
        shown = json.loads(run("assistant", "memory", "show", "alice", "--json"))
        first = shown["items"][0]["id"]
        assert "deploys on Fridays" in run("assistant", "memory", "show", "alice")
        assert run("assistant", "memory", "forget", "alice", first).startswith(
            f"Forgot {first} of alice: deploys on Fridays")
        assert "1 item removed" in run("assistant", "memory", "wipe", "alice")
        assert "remembers nothing about alice" in run("assistant", "memory", "show", "alice")
        code = main.main(["--json", "assistant", "memory", "forget", "bob", "00000000"])
        assert code == 1 and json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
