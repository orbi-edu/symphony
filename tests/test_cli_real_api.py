"""The CLI and the ACP bridge against the REAL control API (`symphony.api.build_app` on uvicorn,
real stores on a disposable Redis): the fake API in `test_cli_fake_api.py` only proves the CLI
against our reading of the contract; this proves it against what the API actually answers."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from tests.api_support import (
    FakeMerger,
    Server,
    bearer,
    build_app,
    fresh_daemon,
    make_services,
    tokens_for,
)
from tests.test_admin_providers import SecretCore

from symphony import state
from symphony.cli import creds, main
from symphony.cli.client import REASON_RULES, reason_problem
from symphony.providers import ProviderRepository
from test_acp_bridge import AcpClient, text_of

CATALOG = Path(__file__).parent / "fixtures" / "omp-catalog" / "models.json"


class Keychain:
    def __init__(self) -> None:
        self.items: dict = {}

    def set_password(self, service, user, secret):
        self.items[(service, user)] = secret

    def get_password(self, service, user):
        return self.items.get((service, user))

    def delete_password(self, service, user):
        self.items.pop((service, user), None)


@pytest.fixture
def real(redis_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    monkeypatch.setenv("TERN_CONFIG_DIR", str(tmp_path / "tern"))
    for var in ("SYMPHONY_URL", "SYMPHONY_TOKEN", "SYMPHONY_OMP", "TERM_PROGRAM", "PI_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    kc = Keychain()
    monkeypatch.setattr(creds, "_keyring", lambda: kc)
    # Providers in an in-memory Secret (the providers tests' fake core) and the fixture catalog,
    # whose built-in provider ids are `acme` and `zeta`.
    # Two repositories have a merger; the one for `acme/lib` stopped answering.
    services = make_services(redis_client, merger=FakeMerger(["acme/app", "acme/lib"],
                                                             dead={"acme/lib"}),
                             ping_interval_s=0.5,
                             providers=ProviderRepository(SecretCore(), "symphony"),
                             catalog_path=CATALOG)
    tokens = tokens_for(services)
    fresh_daemon(redis_client)
    with Server(build_app(services)) as server:
        yield Real(server.url, services, tokens, monkeypatch)


class Real:
    def __init__(self, url, services, tokens, monkeypatch) -> None:
        self.url, self.services, self.tokens, self._mp = url, services, tokens, monkeypatch

    def run(self, capsys, *argv, stdin: str | None = None):
        if stdin is not None:
            self._mp.setattr(sys, "stdin", io.StringIO(stdin))
        code = main.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    def login(self, capsys, role: str) -> None:
        code, out, err = self.run(capsys, "login", "--url", self.url, "--token", "-", "--json",
                                  stdin=self.tokens[role])
        assert code == 0, out + err

    def seed(self, sid="iss-1:1", **meta) -> str:
        store = self.services.sessions
        store.open(sid, kind="implement", identifier="ENG-1", title="Fix login redirect",
                   issue_id="iss-1", seq=1, repo="acme/web", branch="sym/eng-1", **meta)
        store.update(sid, state="running", turn=2,
                     tokens={"input": 1000, "output": 200, "total": 1200, "cost": 0.12})
        return sid


def test_login_whoami_status_and_sessions(real, capsys):
    real.login(capsys, "viewer")
    code, out, _ = real.run(capsys, "token", "print", "--json")
    assert json.loads(out) == {"url": real.url, "token": real.tokens["viewer"],
                               "name": "viewer-person", "role": "viewer"}
    code, out, _ = real.run(capsys, "whoami")
    assert code == 0 and "viewer-person (viewer)" in out

    sid = real.seed()
    code, out, err = real.run(capsys, "status")
    assert code == 0, err
    assert "daemon up (heartbeat " in out and "1,200 tokens" in out
    # The overview's merger counts are the sum over repositories.
    assert "Merger: 2 in review · 0 in the train · 4 parked" in out
    code, out, _ = real.run(capsys, "status", "--json")
    assert json.loads(out)["counts"]["running"] >= 0

    code, out, _ = real.run(capsys, "sessions")
    row = next(line for line in out.splitlines() if line.startswith("ENG-1"))
    assert "running" in row and sid in row
    code, out, _ = real.run(capsys, "sessions", "--state", "running", "--json")
    assert [m["sid"] for m in json.loads(out)["items"]] == [sid]
    assert real.run(capsys, "issues")[0] == 0
    code, out, _ = real.run(capsys, "merger", "status")
    assert out.index("acme/app (merger running)") < out.index("acme/lib (merger not responding)")
    # FakeMerger's phase `oracle` is no review phase code: it is shown as it comes.
    assert "acme/app (merger running)\n  In review:\n    #7 · oracle · worker w1 · since " in out
    assert "  Queue:\n    #9 Fix · bob · first review · ready " in out
    assert "  Train: no lease, the train is stopped" in out and "{" not in out
    code, out, _ = real.run(capsys, "merger", "status", "--repo", "acme/lib", "--json")
    assert [(r["repo"], r["alive"]) for r in json.loads(out)["repos"]] == [("acme/lib", False)]


def test_merger_actions_name_the_repository(real, capsys):
    real.login(capsys, "operator")
    code, out, err = real.run(capsys, "merger", "requeue", "9", "--reason", "flaky CI")
    assert code == 1 and err == "error: More than one repository has a merger. Name one with --repo.\n"
    code, out, err = real.run(capsys, "merger", "requeue", "9", "--reason", "flaky CI",
                              "--repo", "ACME/App")
    assert code == 0 and out == "PR #9 in acme/app is back in the merger queue.\n"
    code, out, err = real.run(capsys, "merger", "bypass", "9", "--reason", "hotfix",
                              "--repo", "acme/other", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "unknown_repo"
    assert real.services.merger.actions == [("acme/app", 9, "requeue", "operator-person", "flaky CI")]
    code, out, _ = real.run(capsys, "audit")
    assert "merger: requeue" in out and "pr:acme/app#9" in out


def test_operating_commands_reach_the_runner_intents(real, capsys):
    real.login(capsys, "operator")
    sid = real.seed()
    assert real.run(capsys, "say", "ENG-1", "add", "a", "test")[0] == 0
    assert real.run(capsys, "say", sid, "later", "--follow-up")[0] == 0
    assert real.run(capsys, "interrupt", sid, "--reason", "wrong path")[0] == 0
    real.services.sessions.request_input(sid, "q1", "Which DB?", ["pg"])
    assert real.run(capsys, "answer", "ENG-1", "pg")[0] == 0
    intents = [(k, p) for _, k, _, p in real.services.sessions.read_intents(sid)]
    assert [k for k, _ in intents] == ["steer", "follow_up", "interrupt", "answer"]
    assert intents[3][1]["request_id"] == "q1"
    code, out, err = real.run(capsys, "pause", "--reason", "holiday freeze")
    assert code == 0, err
    assert "Pause requested" in out
    code, out, err = real.run(capsys, "resume", "--reason", "holiday is over")
    assert code == 0, err
    code, out, err = real.run(capsys, "reevaluate", "ENG-404", "--reason", "flaky CI fixed")
    assert code == 1 and err == "error: Not found.\n"


def test_status_shows_the_real_rate_limits(real, capsys):
    now = time.time()
    real.services.client.hset(f"sym:{real.services.namespace}:ratelimits", "amazon-bedrock/opus",
                              json.dumps({"stage": "throttled", "remaining": 0, "limit": 50,
                                          "reset_at": int((now + 600) * 1000),
                                          "observed_at": int(now * 1000), "source": "usage_limit"}))
    real.login(capsys, "viewer")
    code, out, err = real.run(capsys, "status")
    assert code == 0, err
    line = out.split("Provider limits (last hour):\n")[1].splitlines()[0]
    assert line.startswith("  amazon-bedrock/opus: throttled by the provider · 0/50 left · resets in ")
    assert "quota, seen " in line


@pytest.mark.parametrize("command", ["repos", "infra", "telemetry", "learning", "audit"])
def test_projection_commands_against_the_real_routes(real, capsys, command):
    real.login(capsys, "admin")
    real.seed()
    real.run(capsys, "token", "create", "bob", "--role", "viewer")  # leaves an audit event
    code, out, err = real.run(capsys, command)
    assert code == 0, err
    code, out, err = real.run(capsys, command, "--json")
    assert code == 0, err
    doc = json.loads(out)
    assert "items" in doc or "services" in doc or "totals" in doc
    if command == "audit":
        assert doc["items"][0]["action"] == "token.create"
        assert "create token" in real.run(capsys, "audit")[1]
    if command == "telemetry":
        assert doc["sessions"][0]["identifier"] == "ENG-1"


REASONS = ["", "r", "r" * 7, "r" * 8, "r" * 9, "r" * 1000, "r" * 1001,
           " " * 7 + "r", "  " + "r" * 8 + "  ", " " * 990 + "r" * 10, " " * 995 + "r" * 6]


@pytest.mark.parametrize("reason", REASONS, ids=lambda r: f"{len(r)}:{len(r.strip())}")
def test_command_reason_rule_matches_the_api(real, capsys, reason):
    """The local check and the API accept and refuse exactly the same reasons."""
    real.login(capsys, "operator")
    api = httpx.post(f"{real.url}/v1/commands", headers=bearer(real.tokens["operator"]),
                     json={"action": "refresh", "reason": reason})
    api_accepts = api.status_code == 202
    assert api_accepts == (reason_problem("command", reason) is None), api.text
    code, out, err = real.run(capsys, "refresh", "--reason", reason, "--json")
    assert (code == 0) == api_accepts
    if not api_accepts:
        assert json.loads(out)["error"]["code"] == "invalid_reason"
    code, out, err = real.run(capsys, "refresh", "--reason", reason)
    if not api_accepts:
        assert "The reason" in err and "characters" in err


@pytest.mark.parametrize("reason", ["", "r", " ", "r" * 500, "r" * 501],
                         ids=lambda r: f"{len(r)}:{len(r.strip())}")
def test_config_reason_rule_matches_the_api(real, capsys, reason):
    generation = real.services.teamconfig.get()["generation"]
    api = httpx.put(f"{real.url}/v1/admin/config", headers={**bearer(real.tokens["admin"]),
                                                             "If-Match": str(generation)},
                    json={"models": {"probe": {"provider": "amazon-bedrock", "model": "m-small"}},
                          "reason": reason})
    assert (api.status_code == 200) == (reason_problem("config", reason) is None), api.text
    assert REASON_RULES["config"] == (1, 500, False)


def test_admin_config_tokens_and_omp_sync(real, capsys, tmp_path, monkeypatch):
    real.login(capsys, "admin")
    code, out, err = real.run(capsys, "admin", "config", "set",
                              "developer=amazon-bedrock/global.anthropic.claude-opus-5:max",
                              "--reason", "faster dev")
    assert code == 0, err
    assert real.services.teamconfig.get()["models"]["developer"]["thinking"] == "max"
    code, out, _ = real.run(capsys, "admin", "config", "show")
    assert "Generation 1 · by admin-person" in out

    omp = tmp_path / "omp"
    omp.write_text("#!/bin/sh\necho omp/18.20.2\n")
    omp.chmod(0o755)
    monkeypatch.setenv("SYMPHONY_OMP", str(omp))
    code, out, err = real.run(capsys, "omp", "sync", "--json")
    assert code == 0, err
    assert real.services.teamconfig.observed()["member:admin-person"]["generation"] == 1
    assert "global.anthropic.claude-opus-5:max" in (
        Path(json.loads(out)["config"]).read_text())
    code, out, _ = real.run(capsys, "admin", "config", "observed")
    assert "member:admin-person" in out

    code, out, err = real.run(capsys, "token", "create", "bob", "--role", "operator", "--json")
    assert code == 0, err
    created = json.loads(out)
    assert created["token"].startswith("sym_") and created["role"] == "operator"
    code, out, _ = real.run(capsys, "token", "list")
    assert "bob" in out
    code, out, err = real.run(capsys, "token", "revoke", created["id"], "--json")
    assert code == 0, err


def test_errors_are_plain_text_and_json_is_untouched(real, capsys):
    real.login(capsys, "viewer")
    sid = real.seed()
    code, out, err = real.run(capsys, "say", sid, "hi")
    assert code == 1 and err == "error: Your token does not allow this. Ask for a token with a higher role.\n"
    code, out, _ = real.run(capsys, "say", sid, "hi", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "forbidden"
    code, out, _ = real.run(capsys, "api", "GET", "/v1/sessions/nope:1")
    assert code == 1 and json.loads(out)["error"]["code"] == "not_found"
    code, out, err = real.run(capsys, "watch", "nope:1")
    assert code == 1 and err == "error: Not found.\n"
    real.services.sessions.close(sid, "done")
    real.login(capsys, "operator")
    code, out, err = real.run(capsys, "say", sid, "too late")
    assert code == 1 and err == "error: This session has ended.\n"


def test_takeover_holds_exports_and_release_round_trips(real, capsys, tmp_path, monkeypatch):
    client = real.services.client
    client.set(state.issue_key("iss-1"), json.dumps({"dispatch_seq": 1, "attempt": 1,
                                                     "identifier": "ENG-1", "status": "running"}))
    real.login(capsys, "operator")
    sid = real.seed()
    real.services.sessions.append_transcript(sid, [{"type": "message", "id": "m1",
                                                    "message": {"role": "user", "content": "go"}}])
    monkeypatch.setenv("SYMPHONY_OMP", "/opt/omp")
    code, out, err = real.run(capsys, "takeover", "ENG-1", "--dir", str(tmp_path), "--json")
    assert code == 0, err
    steps = json.loads(out)
    assert steps["hold"]["action"] == "hold"
    lines = [json.loads(line) for line in Path(steps["transcript"]).read_text().splitlines()]
    # The real export leads with a session header so `omp --resume` can open the file.
    assert lines[0]["type"] == "session" and [e.get("id") for e in lines[1:]] == ["m1"]
    assert [k for _, k, _, _ in real.services.sessions.read_intents(sid)] == ["interrupt"]
    accepted = [e for e in real.services.store.audit()["items"] if e["action"] == "hold"]
    assert accepted and accepted[0]["reason"] == "taken over by operator-person"

    # The daemon applies the durable hold on its next step (internals §12); do what it does.
    client.set(state.hold_key("iss-1"),
               json.dumps(state.hold_record("operator-person", "taken over by operator-person",
                                            int(time.time() * 1000))))
    code, out, _ = real.run(capsys, "issues")
    row = next(line for line in out.splitlines() if line.startswith("ENG-1"))
    assert "held" in row and "by operator-person " in row
    code, out, _ = real.run(capsys, "status")
    assert "ENG-1 by operator-person " in out and ": taken over by operator-person" in out
    code, out, err = real.run(capsys, "takeover", "ENG-1", "--dir", str(tmp_path), "--no-launch")
    assert code == 1 and err == "error: A person already holds this ticket.\n"

    code, out, err = real.run(capsys, "release", "ENG-1")
    assert code == 0, err
    released = [e for e in real.services.store.audit()["items"] if e["action"] == "release"]
    assert released and released[0]["reason"] == "returned by operator-person"
    code, out, err = real.run(capsys, "audit")
    assert code == 0, err
    assert "hold ticket" in out and "release ticket" in out
    client.delete(state.hold_key("iss-1"))
    code, out, err = real.run(capsys, "release", "ENG-1")
    assert code == 1 and err == "error: This ticket is not held.\n"
    code, out, err = real.run(capsys, "release", "ENG-404")
    assert code == 1 and err == "error: Not found.\n"


def test_issues_translate_the_real_block_reason(real, capsys):
    real.services.client.set(state.issue_key("iss-9"), json.dumps({
        "dispatch_seq": 1, "attempt": 1, "identifier": "ENG-9", "status": "blocked",
        "undispatchable_reason": "repo-not-authorized"}))
    real.login(capsys, "viewer")
    code, out, err = real.run(capsys, "issues")
    assert code == 0, err
    row = next(line for line in out.splitlines() if line.startswith("ENG-9"))
    assert "Symphony is not authorized for the repository" in row
    code, out, _ = real.run(capsys, "issues", "--json")
    assert json.loads(out)["items"][0]["reason"] == "repo_not_authorized"


def test_acp_bridge_against_the_real_api(real, capsys):
    sid = real.seed()
    real.services.sessions.append_transcript(sid, [
        {"type": "message", "id": "m1", "message": {"role": "user", "content": [{"type": "text", "text": "Go"}]}},
    ])
    env = {**os.environ, "SYMPHONY_URL": real.url, "SYMPHONY_TOKEN": real.tokens["operator"]}
    client = AcpClient(env)
    try:
        client.request("initialize", {"protocolVersion": 1})
        listed = client.request("session/list", {"cwd": "/tmp"})["result"]["sessions"]
        assert listed[0]["title"] == "ENG-1 · Fix login redirect · running"
        assert client.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})["result"] == {}
        client.updates_until(lambda u: u["sessionUpdate"] == "available_commands_update")
        real.services.sessions.append_event(sid, "message_update", {
            "type": "message_update", "messageId": "a", "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "delta": "LIVE"}})
        client.updates_until(lambda u: text_of(u) == "LIVE")
        client.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "steer me"}]})
        client.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/pause x"}]})
        assert "at least 8" in text_of(client.updates_until(lambda u: "Not sent" in text_of(u))[-1])
        assert [(k, p) for _, k, _, p in real.services.sessions.read_intents(sid)] == [
            ("steer", {"text": "steer me"})]
        real.services.sessions.close(sid, "done")
        client.updates_until(lambda u: "The session ended" in text_of(u))
    finally:
        client.close()


def test_provider_key_against_the_real_api(real, capsys):
    """`admin provider key` stores `{builtin: true, apiKey}` for a catalog provider through
    the real route and Secret store; the key never comes back."""
    key = "sk-test-not-a-real-key-0123456789"
    real.login(capsys, "admin")
    code, out, err = real.run(capsys, "admin", "provider", "list")
    assert code == 0, err
    assert "No providers are configured" in out
    code, out, err = real.run(capsys, "admin", "provider", "key", "acme", stdin=key + "\n")
    assert code == 0, err
    assert "API key for acme saved" in out and key not in out
    stored = real.services.providers._load()[1]["providers"]
    assert stored == {"acme": {"builtin": True, "apiKey": key}}
    code, out, _ = real.run(capsys, "admin", "provider", "list")
    row = next(line for line in out.splitlines() if line.startswith("acme"))
    assert "built-in" in row and row.rstrip().endswith("yes") and key not in out
    code, out, _ = real.run(capsys, "admin", "provider", "list", "--json")
    assert key not in out
    assert {"id": "acme", "builtin": True, "credentialConfigured": True} in json.loads(out)["items"]
    # The audit records the save under its only provider action.
    code, out, _ = real.run(capsys, "audit")
    assert "save provider" in out

    code, out, err = real.run(capsys, "admin", "provider", "key", "claude-pro", stdin=key)
    assert code == 1 and err == "error: This provider is not in the omp catalog.\n"
    code, out, err = real.run(capsys, "admin", "provider", "key", "claude-pro", "--json", stdin=key)
    assert json.loads(out)["error"]["code"] == "unknown_provider"
    real.login(capsys, "operator")
    code, out, err = real.run(capsys, "admin", "provider", "list")
    assert code == 1 and "does not allow this" in err
