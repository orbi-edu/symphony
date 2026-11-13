"""`/v1/backup` and `symphony backup …` against the real API on uvicorn (real stores on a
disposable Redis), with a directory target from the mounted WORKFLOW.md."""

from __future__ import annotations

import gzip
import io
import json

import httpx
import pytest
from tests.api_support import NS, Server, bearer, build_app, fresh_daemon, make_services, tokens_for
from tests.test_cli_real_api import Keychain

from symphony import backup, edge
from symphony.cli import creds, main
from symphony.sessions import SessionStore


@pytest.fixture
def stack(redis_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    for var in ("SYMPHONY_URL", "SYMPHONY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    keychain = Keychain()
    monkeypatch.setattr(creds, "_keyring", lambda: keychain)
    mount = tmp_path / "workflow"
    mount.mkdir()
    (mount / "WORKFLOW.md").write_text(
        f"---\ntracker: {{kind: memory}}\nbackup: {{dir: {tmp_path / 'store'}, keep: 2}}\n"
        "---\nWork.\n"
    )
    monkeypatch.setattr(edge, "WORKFLOW_MOUNT", str(mount))
    services = make_services(redis_client)
    tokens = tokens_for(services)
    fresh_daemon(redis_client)  # a live, unpaused daemon
    with Server(build_app(services)) as server:
        yield server, services, tokens, tmp_path


def cli(capsys, *argv):
    code = main.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def login(capsys, server, tokens, role="admin"):
    import sys

    stdin = io.StringIO(tokens[role])
    old, sys.stdin = sys.stdin, stdin
    try:
        code, out, err = cli(capsys, "login", "--url", server.url, "--token", "-", "--json")
    finally:
        sys.stdin = old
    assert code == 0, out + err


def audit_actions(services) -> list[str]:
    return [event["action"] for event in services.store.audit(limit=200)["items"]]


def test_backup_routes_are_admin_only(stack):
    server, _services, tokens, _ = stack
    for role in ("viewer", "operator"):
        for method, path in (("GET", "/v1/backup"), ("GET", "/v1/backup/export"),
                             ("POST", "/v1/backup/run"), ("POST", "/v1/backup/restore?confirm=true")):
            response = httpx.request(method, server.url + path, headers=bearer(tokens[role]))
            assert response.status_code == 403, (role, path)
    doc = httpx.get(server.url + "/v1/backup", headers=bearer(tokens["admin"])).json()
    assert doc["target"]["kind"] == "dir" and doc["keep"] == 2
    assert doc["retention"] == {"sessions_days": 7, "audit_days": 30}


def test_export_and_restore_through_the_cli(stack, capsys):
    server, services, tokens, tmp_path = stack
    client = services.client
    client.set(f"sym:{NS}:teamconfig:history", "kept")
    login(capsys, server, tokens)
    file = tmp_path / "prod.ndjson.gz"
    code, out, err = cli(capsys, "backup", "export", "-o", str(file), "--json")
    assert code == 0, out + err
    doc = json.loads(out)
    assert doc["namespace"] == NS and doc["records"] > 0 and file.stat().st_size == doc["bytes"]
    response = httpx.get(server.url + "/v1/backup/export", headers=bearer(tokens["admin"]))
    assert response.headers["cache-control"] == "no-store"

    client.delete(f"sym:{NS}:teamconfig:history")
    code, out, _ = cli(capsys, "backup", "restore", str(file))
    assert code == 1 and "--confirm" in _
    code, out, _ = cli(capsys, "backup", "restore", str(file), "--confirm", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "daemon_running"
    assert client.get(f"sym:{NS}:teamconfig:history") is None

    client.set(f"admin:{NS}:admission", "true")  # what a processed `pause` leaves
    code, out, err = cli(capsys, "backup", "restore", str(file), "--confirm")
    assert code == 0, out + err
    assert "symphony resume" in out
    assert client.get(f"sym:{NS}:teamconfig:history") == "kept"
    assert client.get(f"admin:{NS}:admission") == "true"
    actions = audit_actions(services)
    assert "backup.export" in actions and "backup.restore" in actions


def test_a_tampered_upload_writes_nothing(stack, capsys):
    server, services, tokens, tmp_path = stack
    services.client.set(f"admin:{NS}:admission", "true")
    out = io.BytesIO()
    backup.write_export(services.client, NS, out)
    raw = gzip.decompress(out.getvalue()).replace(b'"format":1', b'"format":1 ', 1)
    response = httpx.post(server.url + "/v1/backup/restore", params={"confirm": "true"},
                          content=gzip.compress(raw), headers=bearer(tokens["admin"]))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "checksum_mismatch")
    response = httpx.post(server.url + "/v1/backup/restore", params={"namespace_from": "Bad!"},
                          content=b"", headers=bearer(tokens["admin"]))
    assert response.status_code == 422


def test_run_list_restore_object_and_archive(stack, capsys):
    server, services, tokens, tmp_path = stack
    sessions = SessionStore(services.client, NS)
    sessions.open("iss-1:1", kind="implement", identifier="ENG-1")
    sessions.append_transcript("iss-1:1", [{"role": "agent", "text": "done"}])
    sessions.close("iss-1:1", "succeeded")
    login(capsys, server, tokens)
    code, out, err = cli(capsys, "backup", "run", "--json")
    assert code == 0, out + err
    result = json.loads(out)
    assert result["archive"]["sessions"] == 1 and result["backup"]["records"] > 0

    code, out, _ = cli(capsys, "backup", "list", "--json")
    [item] = json.loads(out)["items"]
    assert item["name"] == result["backup"]["name"]
    code, out, _ = cli(capsys, "backup", "status")
    assert code == 0 and "directory" in out and "backup" in out

    code, out, _ = cli(capsys, "backup", "archive", "list", "--json")
    [archived] = json.loads(out)["items"]
    code, out, _ = cli(capsys, "backup", "archive", "get", archived["name"])
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["entry"]["text"] for line in lines if line["kind"] == "transcript"] == ["done"]

    services.client.set(f"admin:{NS}:admission", "true")
    code, out, err = cli(capsys, "backup", "restore", "--object", item["name"], "--confirm", "--json")
    assert code == 0, out + err
    assert json.loads(out)["name"] == item["name"]
    code, out, _ = cli(capsys, "backup", "restore", "--object", "../x", "--confirm", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] in ("invalid_name", "not_found")
    actions = audit_actions(services)
    assert {"backup.run", "backup.archive.get", "backup.restore"} <= set(actions)
