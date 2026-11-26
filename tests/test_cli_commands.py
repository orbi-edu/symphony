"""`symphony` CLI against the in-process fake API: credentials, outputs, exit codes and the
commands that change the local machine (`tern install`, `omp sync`, `takeover`)."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from symphony.cli import creds, main

pytest_plugins = ["test_cli_fake_api"]


class MemoryKeyring:
    def __init__(self, *, drops: bool = False, fails: bool = False) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.drops = drops
        self.fails = fails

    def set_password(self, service, user, secret):
        if self.fails:
            raise RuntimeError("no keychain")
        if not self.drops:
            self.items[(service, user)] = secret

    def get_password(self, service, user):
        if self.fails:
            raise RuntimeError("no keychain")
        return self.items.get((service, user))

    def delete_password(self, service, user):
        self.items.pop((service, user), None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    monkeypatch.setenv("TERN_CONFIG_DIR", str(tmp_path / "tern"))
    for var in ("SYMPHONY_URL", "SYMPHONY_TOKEN", "PI_CONFIG_DIR", "SYMPHONY_OMP", "TERM_PROGRAM"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def keychain(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.setattr(creds, "_keyring", lambda: kr)
    return kr


def run(capsys, *argv, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def login(capsys, api, token="sym_v1_viewer"):
    code, out, _ = run(capsys, "login", "--url", api.url, "--token", token, "--json")
    assert code == 0, out
    return json.loads(out)


# -- credentials -----------------------------------------------------------------

def test_login_keeps_token_in_keychain_and_identity_in_config(home, keychain, fake_api, capsys):
    doc = login(capsys, fake_api)
    assert doc == {"url": fake_api.url, "name": "carol", "role": "viewer", "token_id": "v1",
                   "storage": "keychain"}
    assert keychain.items[("symphony", fake_api.url)] == "sym_v1_viewer"
    config = (home / ".config/symphony/config.toml").read_text()
    assert "sym_v1_viewer" not in config and 'token_id = "v1"' in config
    assert not (home / ".config/symphony/token").exists()

    code, out, _ = run(capsys, "token", "print", "--json")
    assert code == 0
    assert json.loads(out) == {"url": fake_api.url, "token": "sym_v1_viewer", "name": "carol",
                               "role": "viewer"}
    code, out, _ = run(capsys, "whoami")
    assert code == 0 and "carol (viewer)" in out


@pytest.mark.parametrize("backend", [MemoryKeyring(fails=True), MemoryKeyring(drops=True)])
def test_login_falls_back_to_private_file_without_a_working_keychain(home, monkeypatch, fake_api,
                                                                     capsys, backend):
    monkeypatch.setattr(creds, "_keyring", lambda: backend)
    assert login(capsys, fake_api)["storage"] == "file"
    token_file = home / ".config/symphony/token"
    assert token_file.read_text().strip() == "sym_v1_viewer"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".config/symphony/config.toml").stat().st_mode) == 0o600
    code, out, _ = run(capsys, "token", "print", "--json")
    assert code == 0 and json.loads(out)["token"] == "sym_v1_viewer"


def test_login_reads_token_from_stdin_and_rejects_a_bad_one(home, keychain, fake_api, capsys,
                                                           monkeypatch):
    code, out, _ = run(capsys, "login", "--url", fake_api.url, "--token", "-", "--json",
                       stdin="sym_bogus\n", monkeypatch=monkeypatch)
    assert code == 1
    assert json.loads(out)["error"]["code"] == "invalid_token"
    assert not (home / ".config/symphony/config.toml").exists()
    code, out, _ = run(capsys, "login", "--url", fake_api.url + "/", "--token", "-", "--json",
                       stdin="sym_o1_operator\n", monkeypatch=monkeypatch)
    assert code == 0 and json.loads(out)["role"] == "operator"


def test_env_credentials_take_precedence(home, keychain, fake_api, capsys, monkeypatch):
    login(capsys, fake_api)
    monkeypatch.setenv("SYMPHONY_TOKEN", "sym_a1_admin")
    code, out, _ = run(capsys, "token", "print", "--json")
    assert json.loads(out) == {"url": fake_api.url, "token": "sym_a1_admin", "name": "alice",
                               "role": "admin"}
    monkeypatch.setenv("SYMPHONY_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("SYMPHONY_TOKEN")
    code, out, _ = run(capsys, "token", "print", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "not_logged_in"


def test_logout_and_not_logged_in_errors(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    assert run(capsys, "logout")[0] == 0
    assert keychain.items == {}
    code, out, _ = run(capsys, "token", "print", "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "not_logged_in"
    code, out, err = run(capsys, "status")
    assert code == 1 and out == "" and "symphony login" in err


# -- raw API ---------------------------------------------------------------------

def test_api_prints_raw_json_and_exits_1_on_errors(home, keychain, fake_api, capsys, monkeypatch):
    login(capsys, fake_api)
    sid = fake_api.seed_session()
    code, out, _ = run(capsys, "api", "get", "/v1/me")
    assert code == 0 and json.loads(out) == {"name": "carol", "role": "viewer", "token_id": "v1"}

    code, out, _ = run(capsys, "api", "POST", f"/v1/sessions/{sid}/messages",
                       "--data", '{"text": "hi", "mode": "auto"}')
    assert code == 1
    assert json.loads(out)["error"]["code"] == "forbidden"

    code, out, _ = run(capsys, "api", "GET", "/v1/sessions/nope:1")
    assert code == 1 and json.loads(out)["error"]["code"] == "not_found"

    code, out, _ = run(capsys, "api", "GET", "/v1/me", "--data", "{nope")
    assert code == 1 and json.loads(out)["error"]["code"] == "invalid_json"


def test_api_body_from_stdin_and_headers(home, keychain, fake_api, capsys, monkeypatch):
    login(capsys, fake_api, "sym_a1_admin")
    body = json.dumps({"models": {"probe": {"provider": "amazon-bedrock", "model": "m-small"}},
                       "reason": "cheaper probe"})
    code, out, _ = run(capsys, "api", "PUT", "/v1/admin/config", "--data", "-",
                       "--header", "If-Match: 0", stdin=body, monkeypatch=monkeypatch)
    assert code == 0, out
    assert json.loads(out)["generation"] == 1
    assert fake_api.requests[-1][2]["headers"]["if-match"] == "0"
    # A stale generation is refused (412, like the real API), surfaced as exit 1 + its JSON.
    code, out, _ = run(capsys, "api", "PUT", "/v1/admin/config", "--data", "-",
                       "--header", "If-Match: 0", stdin=body, monkeypatch=monkeypatch)
    assert code == 1 and json.loads(out)["error"]["code"] == "precondition_failed"


def test_api_unreachable_server_is_a_json_error(home, keychain, capsys, monkeypatch):
    monkeypatch.setenv("SYMPHONY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SYMPHONY_TOKEN", "sym_x")
    code, out, _ = run(capsys, "api", "GET", "/v1/me")
    assert code == 1 and json.loads(out)["error"]["code"] == "unreachable"


# -- reading commands -------------------------------------------------------------

def test_sessions_table_and_json(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    sid = fake_api.seed_session()
    fake_api.store.open("merger:12:review:1", kind="review", title="Review PR 12")
    code, out, _ = run(capsys, "sessions")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["TICKET", "KIND", "STATE", "TURN", "TOKENS", "COST",
                                "UPDATED", "SESSION"]
    row = next(line for line in lines if line.startswith("ENG-1"))
    assert "implementation" in row and "running" in row and "1,200" in row and sid in row
    assert any(line.startswith("PR #12 · review") and "starting" in line for line in lines)

    code, out, _ = run(capsys, "sessions", "--state", "running", "--json")
    items = json.loads(out)["items"]
    assert [m["sid"] for m in items] == [sid]


def test_json_output_never_renders_text(home, keychain, fake_api, capsys, monkeypatch):
    from symphony.cli import render

    login(capsys, fake_api)
    monkeypatch.setattr(render, "overview", lambda doc: 1 / 0)
    code, out, _ = run(capsys, "status", "--json")
    assert code == 0 and json.loads(out)["daemon"]["alive"] is True


def test_timestamps_accept_epoch_ms_and_iso():
    from symphony.cli import render

    assert render.to_ms(1_700_000_000_000) == 1_700_000_000_000
    assert render.to_ms("2023-11-14T22:13:20Z") == 1_700_000_000_000
    assert render.to_ms("2023-11-14T22:13:20.000000+00:00") == 1_700_000_000_000
    assert render.ago("2023-11-14T22:13:20Z", now_ms=1_700_000_090_000) == "1m ago"
    assert render.ago(None) == "—" and render.ago("garbage") == "—"


def test_status_issues_and_merger_text(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "daemon up" in out and "123,456 tokens" in out and "$4.50" in out
    assert "(heartbeat " in out and " ago)" in out
    assert "symphony sessions --state waiting_input" in out
    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["counts"]["queued"] == 2
    code, out, _ = run(capsys, "issues")
    assert "ENG-1" in out and "In Progress" in out
    code, out, _ = run(capsys, "merger", "status")
    # One section per repository, sorted, with the merger's liveness.
    assert out.index("acme/api (merger not responding)") < out.index("acme/web (merger running)")
    assert "acme/web (merger running)\n  In review:\n    #12 Fix login · reviewing · " in out
    assert "  Queue:\n    #14" in out
    assert "{" not in out  # no JSON dumps in text output


def test_merger_text_uses_labels():
    from symphony.cli import render

    out = render.merger_repo({
        "reviewing": [{"pr": 7, "phase": "verify", "worker": "w1", "started_at": 1,
                       "head_sha": "abc", "sessions": []},
                      {"pr": 8, "phase": "brand_new", "worker": "", "started_at": 1}],
        "parked": [{"pr": 5, "kind": "clean", "parked_at": 1, "comment": "https://github.com/acme/app/pull/5#issuecomment-1"},
                   {"pr": 6, "kind": "report", "parked_at": 1, "comment": "", "title": "Drop the cache"},
                   {"pr": 10, "kind": "clean", "waiting": "approval", "parked_at": 1, "title": "Add the export"},
                   {"pr": 11, "kind": "clean", "waiting": "ci", "parked_at": 1, "title": "Fix the login"}],
        "train": {"lease": {"owner": "merger-0", "expires_in_ms": 9500}, "records": [
            {"pr": 3, "state": "finalizing", "branch": "sym/eng-3", "author": "alice", "reviewed_at": 1},
            {"pr": 4, "state": "train", "conflict": True, "branch": "sym/eng-4"}]},
        "batch": {"members": [{"pr": 3, "merged": True}, {"pr": 4}]},
        "queue": [{"pr": 9, "title": "Fix", "author": "bob", "ready_at": 2, "kind": "rereview"}],
    })
    assert "  #7 · verifying findings · worker w1 · since " in out
    assert "  #8 · brand_new · since " in out  # unknown phase: the raw code
    # An older record (no PR title, no `waiting`): `PR #n`, never the report comment's URL.
    assert "  PR #5 · clean review, waiting for CI · since " in out and "issuecomment" not in out
    assert "  #6 Drop the cache · will not merge, report published" in out
    assert "  #10 Add the export · clean review, waiting for approval · since " in out
    assert "  #11 Fix the login · clean review, waiting for CI · since " in out
    assert "Train: lease held by merger-0 · renews in 9s" in out
    assert "  #3 · finishing the merge · sym/eng-3 · alice · reviewed " in out
    assert "  #4 · conflict · sym/eng-4" in out
    assert "Current batch: #3 ✓  #4" in out
    assert "  #9 Fix · bob · new review · ready " in out
    empty = render.merger_repo({"reviewing": [], "parked": [], "train": {"lease": None, "records": []},
                           "batch": {}, "queue": []})
    assert "Train: no lease, the train is stopped\n  empty" in empty and "Batch: empty" in empty


def test_merger_status_filters_by_repo_and_actions_send_repo(home, keychain, fake_api, capsys):
    login(capsys, fake_api, "sym_o1_operator")
    code, out, _ = run(capsys, "merger", "status", "--repo", "ACME/Web")
    assert code == 0 and "acme/web" in out and "acme/api" not in out
    code, out, _ = run(capsys, "merger", "status", "--repo", "acme/web", "--json")
    assert [r["repo"] for r in json.loads(out)["repos"]] == ["acme/web"]
    code, out, err = run(capsys, "merger", "status", "--repo", "acme/none")
    assert code == 1 and err == "error: No merger serves this repository.\n"

    # Two repositories have a merger: the API needs `repo`, and says so.
    code, out, err = run(capsys, "merger", "requeue", "12", "--reason", "flaky CI")
    assert code == 1 and err == "error: More than one repository has a merger. Name one with --repo.\n"
    code, out, _ = run(capsys, "merger", "requeue", "12", "--reason", "flaky CI", "--json")
    assert json.loads(out)["error"]["code"] == "repo_required"
    code, out, err = run(capsys, "merger", "requeue", "12", "--reason", "flaky CI", "--repo", "Acme/Web")
    assert code == 0 and out == "PR #12 in acme/web is back in the merger queue.\n"
    code, out, err = run(capsys, "merger", "bypass", "3", "--reason", "hotfix", "--repo", "acme/zzz")
    assert code == 1 and err == "error: No merger serves this repository.\n"
    assert [(a["pr"], a["action"], a["repo"], a["reason"]) for a in fake_api.merger_actions] == [
        (12, "requeue", "acme/web", "flaky CI")]

    # One repository: `repo` may be left out.
    fake_api.merger_repos = fake_api.merger_repos[:1]
    code, out, _ = run(capsys, "merger", "release", "14", "--reason", "manual merge", "--json")
    assert code == 0 and json.loads(out)["repo"] == "acme/web"
    sent = len(fake_api.requests)
    code, out, err = run(capsys, "merger", "bypass", "14", "--reason", "   ")
    assert code == 1 and "at least 1 characters" in err and len(fake_api.requests) == sent
    code, out, err = run(capsys, "merger", "bypass", "14", "--reason", "x", "--repo", "web")
    assert code == 1 and "owner/name" in err


def test_merger_with_no_repository_yet(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    fake_api.merger_repos = []
    code, out, _ = run(capsys, "merger", "status")
    assert code == 0 and out == "No merger has registered a repository yet.\n"


# -- operating commands ------------------------------------------------------------

def test_say_interrupt_and_answer_post_intents(home, keychain, fake_api, capsys):
    login(capsys, fake_api, "sym_o1_operator")
    sid = fake_api.seed_session()
    assert run(capsys, "say", "ENG-1", "also", "add", "a", "test")[0] == 0
    assert run(capsys, "say", sid, "later", "--follow-up", "--json")[0] == 0
    code, out, _ = run(capsys, "interrupt", sid, "--reason", "wrong path", "--json")
    assert code == 0 and json.loads(out)["intent_id"]
    code, out, err = run(capsys, "answer", "ENG-1", "yes")
    assert code == 1 and "has no open question" in err

    fake_api.store.request_input(sid, "q1", "Which DB?", ["postgres", "sqlite"])
    code, out, _ = run(capsys, "answer", "ENG-1", "postgres", "--json")
    assert code == 0 and json.loads(out)["request_id"] == "q1"

    intents = [(k, p) for _, k, _, p in fake_api.store.read_intents(sid)]
    assert intents == [
        ("steer", {"text": "also add a test"}),
        ("follow_up", {"text": "later"}),
        ("interrupt", {"reason": "wrong path"}),
        ("answer", {"request_id": "q1", "text": "postgres"}),
    ]


def test_viewer_cannot_steer(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    sid = fake_api.seed_session()
    code, out, err = run(capsys, "say", sid, "hi")
    assert code == 1
    assert err == "error: Your token does not allow this. Ask for a token with a higher role.\n"
    code, out, _ = run(capsys, "say", sid, "hi", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "forbidden"


def test_pause_resume_refresh_are_namespace_wide_and_reevaluate_names_a_ticket(home, keychain,
                                                                              fake_api, capsys):
    login(capsys, fake_api, "sym_o1_operator")
    code, out, _ = run(capsys, "pause", "--reason", "holiday freeze")
    assert code == 0 and "takes no new tickets" in out
    assert run(capsys, "resume")[0] == 0
    assert run(capsys, "refresh")[0] == 0
    assert run(capsys, "reevaluate", "ENG-1", "--reason", "flaky CI fixed")[0] == 0
    assert fake_api.commands == [
        {"action": "pause", "reason": "holiday freeze"},
        {"action": "resume", "reason": "resume from the CLI"},
        {"action": "refresh", "reason": "refresh from the CLI"},
        {"action": "reevaluate", "reason": "flaky CI fixed", "target": "ENG-1"},
    ]


def test_short_reason_is_refused_locally(home, keychain, fake_api, capsys):
    login(capsys, fake_api, "sym_o1_operator")
    sent = len(fake_api.requests)
    code, out, err = run(capsys, "pause", "--reason", "  short ")
    assert code == 1 and err == "error: The reason needs at least 8 characters (it has 5).\n"
    code, out, _ = run(capsys, "pause", "--reason", "x" * 1001, "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "invalid_reason"
    code, _, err = run(capsys, "admin", "config", "set", "probe=a/b", "--reason", "")
    assert code == 1 and "at least 1 characters" in err
    assert len(fake_api.requests) == sent  # nothing reached the API


def test_status_shows_provider_rate_limits(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    code, out, _ = run(capsys, "status")
    assert "Provider limits" not in out
    fake_api.rate_limits = [
        {"model": "amazon-bedrock/global.anthropic.claude-opus-5-5", "stage": "wrap_up",
         "remaining": 0, "limit": 100, "reset_at": "2999-01-01T00:00:00Z", "retry_after_ms": None,
         "observed_at": "2026-11-16T00:46:41Z", "source": "usage_limit"},
        {"model": "openai/gpt-x", "stage": "throttled", "remaining": None, "limit": None,
         "reset_at": None, "retry_after_ms": 30000, "observed_at": "2026-11-16T00:46:41Z",
         "source": "retry"},
        {"model": "x/y", "stage": "brand_new_stage", "remaining": None, "limit": None,
         "reset_at": None, "retry_after_ms": None, "observed_at": "2026-11-16T00:46:41Z",
         "source": "usage_limit"},
    ]
    code, out, _ = run(capsys, "status")
    lines = out.split("Provider limits (last hour):\n")[1].splitlines()
    assert lines[0].startswith("  amazon-bedrock/global.anthropic.claude-opus-5-5: wrapping up "
                               "(quota nearly used) · 0/100 left · resets in ")
    assert "· quota, seen " in lines[0] and lines[0].endswith(" ago")
    assert lines[1].startswith("  openai/gpt-x: throttled by the provider · next retry in 30s · "
                               "throttling, seen ")
    assert lines[2].startswith("  x/y: brand_new_stage · quota, seen ")  # unknown stage: raw
    code, out, _ = run(capsys, "status", "--json")
    assert [i["stage"] for i in json.loads(out)["rate_limits"]] == ["wrap_up", "throttled",
                                                                   "brand_new_stage"]


def test_stage_and_audit_action_labels():
    from symphony.cli import render

    stages = {s: render.rate_limit({"model": "m", "stage": s, "observed_at": None}).split(" · ")[0]
              for s in ("low_priority", "wrap_up", "throttled", "odd")}
    assert stages == {"low_priority": "m: low priority (quota almost used)",
                      "wrap_up": "m: wrapping up (quota nearly used)",
                      "throttled": "m: throttled by the provider", "odd": "m: odd"}
    out = render.audit({"items": [
        {"time": None, "actor": "dave", "action": action, "target": "ENG-1", "result": "accepted",
         "reason": "taken over by dave"} for action in ("hold", "release", "merger.release", "x.y")]})
    rows = out.splitlines()[1:]
    assert ["hold ticket" in rows[0], "release ticket" in rows[1],
            "merger: release PR" in rows[2], "x.y" in rows[3]] == [True] * 4


def test_every_audited_action_has_a_label():
    from symphony import admin_commands
    from symphony.cli import render

    audited = admin_commands.ACTIONS | admin_commands.STANDALONE_ACTIONS | admin_commands.PROVIDER_ACTIONS
    assert sorted(a for a in audited if a not in render._ACTION_LABEL) == []


def test_error_codes_map_to_text():
    from symphony.cli import render

    assert render.error_text("session_ended", 409, "this session has ended") == "This session has ended."
    assert render.error_text("not_found", 404, "session not found") == "Not found."
    assert render.error_text("invalid_request", 422, "invalid request: text") == "The request is not valid."
    # Unknown codes fall back by status alone; unknown statuses get a generic line.
    assert render.error_text("brand_new", 503, "x") == "Symphony is not available right now."
    assert render.error_text("brand_new", 418, "teapot") == "The server refused the request (HTTP 418)."
    assert render.error_text("brand_new", 502, "bad gateway") == "The Symphony server failed (HTTP 502)."
    # The CLI's own codes carry their own message, kept as it is.
    assert render.error_text("unreachable", 0, "Cannot connect to http://h.") == "Cannot connect to http://h."
    assert render.error_text("no_session", 404, "No session for ENG-9.") == "No session for ENG-9."


def test_text_errors_never_show_the_server_message_but_json_does(home, keychain, fake_api, capsys):
    login(capsys, fake_api, "sym_o1_operator")
    code, out, err = run(capsys, "release", "ENG-404")
    assert code == 1 and err == "error: Not found.\n"
    code, out, err = run(capsys, "release", "ENG-404", "--json")
    assert code == 1 and json.loads(out)["error"] == {"code": "not_found",
                                                      "message": "tracked issue not found"}
    code, out, err = run(capsys, "say", "ENG-9", "hi")
    assert code == 1 and err == "error: No session for ENG-9.\n"
    code, out, err = run(capsys, "api", "GET", "/v1/me", "--data", "{nope")
    assert code == 1 and json.loads(out)["error"]["message"] == "--data is not JSON (line 1, column 2)."


def test_projection_commands_render_tables_and_json(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    code, out, _ = run(capsys, "repos")
    row = next(line for line in out.splitlines() if line.startswith("acme/web"))
    assert "kubernetes" in row and "ticket, workflow default" in row
    assert "Warning: the learning source is stale." in out
    assert "reflector heartbeat" not in out  # `note` is for logs, never shown
    code, out, _ = run(capsys, "infra")
    redis_row = next(line for line in out.splitlines() if line.startswith("redis "))
    assert "Redis" in redis_row and "healthy" in redis_row and redis_row.rstrip().endswith("ok")
    api_row = next(line for line in out.splitlines() if line.startswith("symphony-api-7d9"))
    assert "API" in api_row and "degraded" in api_row and "not ready" in api_row
    pvc_row = next(line for line in out.splitlines() if line.startswith("sym-ws-eng-1"))
    assert "PersistentVolumeClaim" in pvc_row and "bound" in pvc_row and "workspace" in pvc_row
    code, out, _ = run(capsys, "telemetry", "--limit", "5")
    assert "Total: 3,000 tokens" in out and "2026-11" in out and "ENG-1" in out
    assert fake_api.requests[-1][1] == "/v1/telemetry"
    code, out, _ = run(capsys, "learning")
    rows = {line.split()[0]: line for line in out.splitlines()[1:]}
    assert "retrospective" in rows["ENG-7"] and "PR opened" in rows["ENG-7"] and "pull/9" in rows["ENG-7"]
    assert "retained" in rows["ENG-8"] and "nothing to learn" in rows["ENG-8"]
    code, out, _ = run(capsys, "audit", "--limit", "1")
    assert "alice" in out and "create token" in out and "symphony audit --cursor c1" in out
    code, out, _ = run(capsys, "audit", "--cursor", "c1", "--json")
    assert json.loads(out)["nextCursor"] is None
    # `--json` is the API's document as is: codes, `note`, everything.
    code, out, _ = run(capsys, "repos", "--json")
    doc = json.loads(out)
    assert doc["items"][0]["routes"] == ["issue", "default"]
    assert doc["sources"]["learning"]["note"] == "reflector heartbeat older than 1h"
    code, out, _ = run(capsys, "learning", "--json")
    assert json.loads(out)["items"][1]["outcome"] == "nothing_to_learn"
    for name in ("infra", "telemetry"):
        code, out, _ = run(capsys, name, "--json")
        assert code == 0 and "observedAt" in json.loads(out)


def test_issues_show_phase_and_block_reason(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    fake_api.issues.append({"issue_id": "iss-2", "identifier": "ENG-2", "title": "No repo",
                            "status": "blocked", "phase": None, "tracker_state": "Todo",
                            "pr_url": None, "held": None, "reason": "mixed_owner_repo_designation",
                            "updated_at": "2026-11-16T00:46:41Z"})
    code, out, _ = run(capsys, "issues")
    rows = {line.split()[0]: line for line in out.splitlines()[1:]}
    assert "BLOCKED" in out.splitlines()[0]
    assert "repositories have more than one owner" in rows["ENG-2"] and "blocked" in rows["ENG-2"]
    assert "running" in rows["ENG-1"] and rows["ENG-1"].count("—") >= 2  # no hold, no block
    code, out, _ = run(capsys, "issues", "--json")
    assert json.loads(out)["items"][1]["reason"] == "mixed_owner_repo_designation"


KEY = "sk-ant-test-not-a-real-key-123456"


def test_admin_provider_list_and_key(home, keychain, fake_api, capsys, monkeypatch):
    login(capsys, fake_api, "sym_a1_admin")
    code, out, _ = run(capsys, "admin", "provider", "list")
    assert code == 0
    assert out.splitlines()[0].split() == ["ID", "TYPE", "NAME", "PROTOCOL", "KEY", "SET"]
    row = next(line for line in out.splitlines() if line.startswith("local-llm"))
    assert "custom" in row and "openai-completions" in row and "not needed" in row

    code, out, err = run(capsys, "admin", "provider", "key", "anthropic", stdin=KEY + "\n",
                         monkeypatch=monkeypatch)
    assert code == 0, err
    assert "API key for anthropic saved" in out and KEY not in out
    put = next(r for r in reversed(fake_api.requests) if r[0] == "PUT")
    assert put[1] == "/v1/admin/providers/anthropic"
    assert put[2]["body"] == {"builtin": True, "apiKey": KEY}
    assert put[2]["headers"]["if-match"] == '"7"'
    assert fake_api.providers["anthropic"] == {"builtin": True, "apiKey": KEY}

    code, out, _ = run(capsys, "admin", "provider", "list")
    row = next(line for line in out.splitlines() if line.startswith("anthropic"))
    assert "built-in" in row and row.rstrip().endswith("yes") and KEY not in out
    code, out, _ = run(capsys, "admin", "provider", "list", "--json")
    doc = json.loads(out)
    assert KEY not in out and doc["revision"] == "8"
    assert {"id": "anthropic", "builtin": True, "credentialConfigured": True} in doc["items"]


def test_admin_provider_key_errors(home, keychain, fake_api, capsys, monkeypatch):
    login(capsys, fake_api, "sym_a1_admin")
    code, out, err = run(capsys, "admin", "provider", "key", "claude-pro", stdin=KEY,
                         monkeypatch=monkeypatch)
    assert code == 1 and err == "error: This provider is not in the omp catalog.\n"
    code, out, _ = run(capsys, "admin", "provider", "key", "claude-pro", "--json", stdin=KEY,
                       monkeypatch=monkeypatch)
    assert json.loads(out)["error"]["code"] == "unknown_provider"
    sent = len(fake_api.requests)
    code, out, err = run(capsys, "admin", "provider", "key", "openai", stdin="\n",
                         monkeypatch=monkeypatch)
    assert code == 1 and "Give the API key" in err and len(fake_api.requests) == sent
    login(capsys, fake_api, "sym_o1_operator")
    code, out, err = run(capsys, "admin", "provider", "list")
    assert code == 1 and "does not allow this" in err


def test_tokens_admin(home, keychain, fake_api, capsys):
    login(capsys, fake_api, "sym_a1_admin")
    code, out, _ = run(capsys, "token", "create", "bob", "--role", "operator", "--expires", "30d")
    assert code == 0 and "sym_t2_secret" in out and "only time it is shown" in out
    assert fake_api.requests[-1][2]["body"] == {"name": "bob", "role": "operator", "expires_days": 30}
    code, out, _ = run(capsys, "token", "list")
    assert "bob" in out and "operator" in out
    code, out, _ = run(capsys, "token", "revoke", "t2", "--json")
    assert code == 0 and json.loads(out) == {"revoked": "t2"}
    code, out, _ = run(capsys, "token", "revoke", "t2", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "not_found"


def test_admin_config_set_show_observed(home, keychain, fake_api, capsys):
    login(capsys, fake_api, "sym_a1_admin")
    code, out, err = run(capsys, "admin", "config", "set",
                         "developer=amazon-bedrock/global.anthropic.claude-opus-5:max",
                         "probe=openai/o-mini:2024", "--reason", "faster dev")
    assert code == 0, err
    doc = fake_api.config.get()
    assert doc["generation"] == 1
    assert doc["models"]["developer"] == {"provider": "amazon-bedrock",
                                          "model": "global.anthropic.claude-opus-5",
                                          "thinking": "max"}
    # A suffix that is no thinking level stays part of the model id.
    assert doc["models"]["probe"]["model"] == "o-mini:2024"
    code, out, _ = run(capsys, "admin", "config", "show")
    assert "Generation 1 · by alice" in out and "amazon-bedrock/global.anthropic.claude-opus-5" in out
    code, out, err = run(capsys, "admin", "config", "set", "developer", "--reason", "x")
    assert code == 1 and "role=provider/model" in err
    fake_api.config.observe("daemon", 0)
    code, out, _ = run(capsys, "admin", "config", "observed")
    assert "daemon" in out and "out of date" in out


# -- local machine -------------------------------------------------------------------

def _fake_tern(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "tern"
    script.write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/tern.calls\"\necho '{\"installed\":\"symphony\"}'\n")
    script.chmod(0o755)
    return bindir


def test_tern_install_merges_idempotently_and_keeps_0600(home, keychain, fake_api, capsys,
                                                        monkeypatch, tmp_path):
    login(capsys, fake_api, "sym_o1_operator")
    settings = home / "tern" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "theme": "dark",
        "agents": {
            "custom": [{"id": "omp-acp", "name": "omp", "command": "omp", "args": ["--mode", "acp"]},
                       {"id": "symphony", "name": "Old", "command": "/old/symphony", "args": ["acp"],
                        "env": {"X": "1"}}],
            "mcp": [{"name": "other", "transport": "http", "url": "https://x/mcp"}],
        },
    }))
    settings.chmod(0o644)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.toml").write_text('schema = 1\nid = "symphony"\n')
    bindir = _fake_tern(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    code, out, err = run(capsys, "tern", "install", "--plugin", str(plugin), "--json")
    assert code == 0, err
    result = json.loads(out)
    assert (result["agent"], result["mcp"]) == ("updated", "added")
    doc = json.loads(settings.read_text())
    assert doc["theme"] == "dark"
    custom = {a["id"]: a for a in doc["agents"]["custom"]}
    assert custom["omp-acp"]["command"] == "omp"
    assert custom["symphony"]["name"] == "Symphony" and custom["symphony"]["args"][-1] == "acp"
    assert custom["symphony"]["env"] == {"X": "1"}
    mcp = {m["name"]: m for m in doc["agents"]["mcp"]}
    assert mcp["other"]["url"] == "https://x/mcp"
    assert mcp["symphony"] == {"name": "symphony", "transport": "http", "url": f"{fake_api.url}/mcp",
                               "headers": {"Authorization": "Bearer sym_o1_operator"}}
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    calls = (bindir / "tern.calls").read_text().splitlines()
    assert calls == [f"plugin install {plugin} --force --json"]

    before = settings.read_text()
    code, out, _ = run(capsys, "tern", "install", "--no-plugin", "--json")
    assert json.loads(out)["agent"] == "unchanged" and json.loads(out)["mcp"] == "unchanged"
    assert settings.read_text() == before
    assert len(doc["agents"]["custom"]) == len(json.loads(before)["agents"]["custom"]) == 2


def test_tern_install_refuses_to_clobber_invalid_settings(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    settings = home / "tern" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{ // comment\n}")
    code, out, err = run(capsys, "tern", "install", "--no-plugin")
    assert code == 1 and "Nothing was changed." in err
    assert settings.read_text() == "{ // comment\n}"


def test_tern_install_creates_settings_from_scratch(home, keychain, fake_api, capsys):
    login(capsys, fake_api)
    code, out, _ = run(capsys, "tern", "install", "--no-plugin", "--json")
    assert code == 0
    doc = json.loads((home / "tern" / "settings.json").read_text())
    assert [a["id"] for a in doc["agents"]["custom"]] == ["symphony"]


def test_tern_install_without_a_plugin_names_the_options(home, keychain, fake_api, capsys,
                                                         tmp_path, monkeypatch):
    # No checkout next to the package, and a package without its plugin.
    from symphony.cli import local
    monkeypatch.setattr(local, "REPO_ROOT", tmp_path / "site-packages")
    monkeypatch.setattr(local, "PACKAGED_PLUGIN", tmp_path / "site-packages" / "symphony" / "tern_plugin")
    login(capsys, fake_api)
    code, out, _ = run(capsys, "tern", "install", "--json")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "local_error"
    assert "has no Tern plugin" in error["message"]
    assert "--plugin DIR" in error["message"] and "--no-plugin" in error["message"]
    assert "site-packages" not in error["message"]


def test_omp_sync_writes_only_the_symphony_profile(home, keychain, fake_api, capsys, tmp_path,
                                                  monkeypatch):
    login(capsys, fake_api)
    fake_api.config.publish({"omp": {"settings": {"compaction": {"enabled": True}}}},
                            expected_generation=0, actor="alice", reason="team defaults")
    default_cfg = home / ".omp/agent/config.yml"
    default_cfg.parent.mkdir(parents=True)
    default_cfg.write_text("modelRoles:\n  default: openai/gpt-x\ntheme: mine\n")
    profile_cfg = home / ".omp/profiles/symphony/agent/config.yml"
    profile_cfg.parent.mkdir(parents=True)
    profile_cfg.write_text("# my tweak\nsymbolPreset: unicode\nmodelRoles:\n  smol: x/y\n")
    omp = tmp_path / "omp"
    omp.write_text("#!/bin/sh\necho 'omp/18.20.2'\n")
    omp.chmod(0o755)
    monkeypatch.setenv("SYMPHONY_OMP", str(omp))

    code, out, err = run(capsys, "omp", "sync", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["generation"] == 1 and result["profile"] == "symphony"
    assert result["model"] == "amazon-bedrock/global.anthropic.claude-opus-5-5:high"
    assert result["omp"] == {"path": str(omp), "version": "18.20.2", "team_version": "18.20.2",
                             "matches": True}
    assert default_cfg.read_text() == "modelRoles:\n  default: openai/gpt-x\ntheme: mine\n"
    written = YAML().load(profile_cfg.read_text())
    assert written["modelRoles"] == {"smol": "x/y",
                                     "default": "amazon-bedrock/global.anthropic.claude-opus-5-5:high"}
    assert written["defaultThinkingLevel"] == "high"
    assert written["compaction"] == {"enabled": True}
    assert written["symbolPreset"] == "unicode" and "# my tweak" in profile_cfg.read_text()
    assert fake_api.config.observed()["member:carol"]["generation"] == 1
    assert sorted(p.name for p in (home / ".omp").rglob("*") if p.is_file()) == ["config.yml", "config.yml"]


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_takeover_holds_interrupts_exports_and_fetches_the_branch(home, keychain, fake_api, capsys, tmp_path,
                                                       monkeypatch):
    login(capsys, fake_api, "sym_o1_operator")
    sid = fake_api.seed_session(repo="acme/web")
    fake_api.store.append_transcript(sid, [{"type": "session", "id": "h"},
                                           {"type": "message", "id": "m1",
                                            "message": {"role": "user", "content": "go"}}])
    origin = tmp_path / "acme" / "web.git"
    origin.mkdir(parents=True)
    _git("init", "--bare", "-b", "main", cwd=origin)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("push", str(origin), "main", cwd=seed)
    _git("switch", "-c", "sym/eng-1", cwd=seed)
    (seed / "b.txt").write_text("wip")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "wip", cwd=seed)
    _git("push", str(origin), "sym/eng-1", cwd=seed)
    clone = tmp_path / "clone"
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    monkeypatch.setenv("SYMPHONY_OMP", "/opt/omp")

    code, out, err = run(capsys, "takeover", "ENG-1", "--dir", str(clone), "--json")
    assert code == 0, err
    steps = json.loads(out)
    assert steps["branch"]["status"] == "switched"
    assert (clone / "b.txt").read_text() == "wip"
    transcript = Path(steps["transcript"])
    assert [json.loads(line)["type"] for line in transcript.read_text().splitlines()] == ["session", "message"]
    assert steps["command"] == ["/opt/omp", "--resume", str(transcript)]
    # Hold first (internals §12), then the interrupt.
    assert fake_api.commands == [{"action": "hold", "target": {"issueId": "iss-1"},
                                  "reason": "taken over by dave"}]
    assert [k for _, k, _, _ in fake_api.store.read_intents(sid)] == ["interrupt"]

    code, out, err = run(capsys, "takeover", "ENG-1", "--dir", str(tmp_path), "--no-hold",
                         "--no-launch")
    assert code == 0 and "is not a git repository" in out and "/opt/omp --resume" in out
    assert "ticket not held (--no-hold)" in out and "nothing was interrupted" in out
    assert len(fake_api.commands) == 1
    assert [k for _, k, _, _ in fake_api.store.read_intents(sid)] == ["interrupt"]


def test_takeover_text_and_release_show_the_hold(home, keychain, fake_api, capsys, tmp_path,
                                                 monkeypatch):
    login(capsys, fake_api, "sym_o1_operator")
    fake_api.seed_session()
    monkeypatch.setenv("SYMPHONY_OMP", "/opt/omp")
    code, out, err = run(capsys, "takeover", "ENG-1", "--dir", str(tmp_path), "--no-launch")
    assert code == 0, err
    assert "ticket held: Symphony does not work on ENG-1 until `symphony release ENG-1`" in out
    code, out, _ = run(capsys, "issues")
    header, row = out.splitlines()[:2]
    assert "HELD" in header and "by dave " in row and " ago" in row
    code, out, _ = run(capsys, "status")
    assert "Held by a person" in out and "ENG-1 by dave " in out and ": taken over by dave" in out

    code, out, err = run(capsys, "release", "ENG-1")
    assert code == 0 and "ENG-1 returned to Symphony" in out
    assert fake_api.commands[-1] == {"action": "release", "target": "ENG-1",
                                     "reason": "returned by dave"}
    code, out, err = run(capsys, "release", "ENG-1")
    assert code == 1 and err == "error: This ticket is not held.\n"
    code, out, _ = run(capsys, "status")
    assert "Held by a person" not in out
    code, out, err = run(capsys, "release", "ENG-404")
    assert code == 1 and err == "error: Not found.\n"
    code, out, err = run(capsys, "release", "ENG-1", "--reason", "short")
    assert code == 1 and "at least 8" in err
