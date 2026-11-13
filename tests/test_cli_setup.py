"""`symphony setup` against the in-process fake API: the checklist, each step's subcommand
(secrets on stdin, never in argv), the errors a person reads, and the interactive wizard."""

from __future__ import annotations

import io
import json

import pytest

from symphony.cli import creds, render, setup
from symphony.cli.client import Api
from test_cli_commands import home, keychain, login, run  # noqa: F401

pytest_plugins = ["test_cli_fake_api"]

ADMIN = "sym_a1_admin"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture
def admin(home, keychain, fake_api, capsys):  # noqa: F811
    login(capsys, fake_api, ADMIN)
    return fake_api


def bodies(api, path: str) -> list:
    return [r[2]["body"] for r in api.requests if r[1] == path and r[0] != "GET"]


def test_status_lists_every_step_and_what_is_left(admin, capsys):
    code, out, _ = run(capsys, "setup", "--status")
    assert code == 0
    assert out.startswith("Symphony setup is not complete: 3 required steps left. (mode: kubernetes)")
    # One row per step: mark, name, status, and a summary of its detail.
    assert f"  ○ {'Tracker':<24}  {'to do':<7}  No tracker is configured.\n" in out
    assert f"  ○ {'GitHub App':<24}  {'to do':<7}  No GitHub App is configured.\n" in out
    assert f"  ✓ {'Models':<24}  {'done':<7}  1 role on amazon-bedrock\n" in out
    assert f"  – {'Notifications (optional)':<24}  skipped  No channel is configured.\n" in out
    assert "Run `symphony setup` on a terminal to finish" in out
    assert "\x1b[" not in out  # not a terminal: no color
    code, out, _ = run(capsys, "setup", "--status", "--json")
    doc = json.loads(out)
    assert doc["complete"] is False
    assert [s["id"] for s in doc["steps"]] == ["tracker", "github_app", "repositories", "models",
                                              "notifications", "assistant", "webhooks"]


def test_step_summaries_follow_the_detail_and_the_error():
    summary = setup.step_summary
    assert summary({"id": "tracker", "detail": {"kind": "linear", "team_key": "ENG",
                                                "credential": False}}) == "Linear, team ENG, no API key"
    assert summary({"id": "tracker", "detail": {"kind": "github", "owner": "acme",
                                                "project_number": 3, "credential": True}}) == \
        "GitHub project acme#3, field Status"
    assert summary({"id": "repositories", "detail": {
        "default_repo": "acme/app", "repositories": ["acme/app"], "mergers": ["acme/app"]}}) == \
        "default acme/app, merger for acme/app"
    assert summary({"id": "webhooks", "detail": {
        "linear": {"secret": True, "in_use": False}, "github": {"secret": True, "in_use": True,
                                                                "last_delivery_at": None}}}) == \
        "GitHub: no signed delivery yet"
    # An error wins over the detail; a known code reads as its sentence, with the details.
    assert summary({"id": "tracker", "detail": {}, "error": {
        "code": "linear_team_not_found", "message": "Linear has no team with key 'X'"}}) == \
        "Linear has no team with this key. Details: Linear has no team with key 'X'"
    assert summary({"id": "repositories", "detail": {}, "error": {
        "code": "merger_missing", "message": "no merger is registered for acme/lib"}}) == \
        "No merger is registered for acme/lib"


def test_wizard_refuses_without_a_terminal(admin, capsys):
    code, out, err = run(capsys, "setup")
    assert code == 1 and err.startswith("error: The setup wizard needs a terminal.")
    code, out, _ = run(capsys, "setup", "--json")
    assert json.loads(out)["error"]["code"] == "not_a_terminal"


def test_linear_tracker_key_travels_on_stdin_only(admin, capsys, monkeypatch):
    code, out, err = run(capsys, "setup", "tracker", "--kind", "linear", "--team", "ENG",
                         stdin="lin_api_good\n", monkeypatch=monkeypatch)
    assert code == 0, err
    assert out == ("Tracker saved: Linear.\nStates on the board: Todo, In Progress, Done.\n"
                   "Warning: the WORKFLOW state 'Rework' is not on the board.\n"
                   "Restarted to apply it: symphony-daemon, symphony-api.\n")
    assert bodies(admin, "/v1/setup/tracker") == [
        {"kind": "linear", "team_key": "ENG", "api_key": "lin_api_good"}]
    code, out, err = run(capsys, "setup", "tracker", "--kind", "linear", "--project", "web",
                         stdin="lin_api_bad\n", monkeypatch=monkeypatch)
    assert code == 1 and err == f"error: {render.ERROR_TEXT['linear_unauthorized']}\n"
    # A code that names the value: the sentence, then the service's details.
    code, out, err = run(capsys, "setup", "tracker", "--kind", "linear", "--team", "OPS",
                         stdin="lin_api_good\n", monkeypatch=monkeypatch)
    assert err == ("error: Linear has no team with this key. Details: Linear has no team with "
                   "key 'OPS'\n")
    code, out, _ = run(capsys, "setup", "tracker", "--kind", "linear", "--team", "ENG", "--json",
                       stdin="\n", monkeypatch=monkeypatch)
    assert json.loads(out)["error"]["code"] == "missing_secret"


def test_tracker_flags_are_checked_before_any_request(admin, capsys, monkeypatch):
    before = len(admin.requests)
    code, _, err = run(capsys, "setup", "tracker", "--kind", "linear", "--team", "ENG",
                       "--project", "web", stdin="lin_api_good\n", monkeypatch=monkeypatch)
    assert code == 1 and err == "error: Use exactly one of --team KEY or --project SLUG.\n"
    code, _, err = run(capsys, "setup", "tracker", "--kind", "github", "--team", "ENG")
    assert code == 1 and err == "error: --team and --project are for Linear.\n"
    code, _, err = run(capsys, "setup", "tracker", "--kind", "github", "--owner", "acme")
    assert code == 1 and err == "error: GitHub needs --owner and --project-number.\n"
    assert len(admin.requests) == before


def test_github_tracker_needs_the_app_and_no_secret(admin, capsys, monkeypatch):
    argv = ["setup", "tracker", "--kind", "github", "--owner", "acme", "--project-number", "3",
            "--status-field", "Stage"]
    code, _, err = run(capsys, *argv)
    assert code == 1 and err == f"error: {render.ERROR_TEXT['github_app_required']}\n"
    run(capsys, "setup", "github-app", "--app-id", "101", "--installation-id", "7", stdin=PEM,
        monkeypatch=monkeypatch)
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0, err
    assert json.loads(out)["kind"] == "github"
    assert bodies(admin, "/v1/setup/tracker")[-1] == {
        "kind": "github", "owner": "acme", "project_number": 3, "status_field": "Stage"}


def test_github_app_key_from_stdin_or_a_file(admin, capsys, monkeypatch, tmp_path):
    code, out, err = run(capsys, "setup", "github-app", "--app-id", "101", "--installation-id", "7",
                         stdin=PEM, monkeypatch=monkeypatch)
    assert code == 0, err
    assert out == ("GitHub App saved.\nThe installation can reach 1 repository: acme/web.\n"
                   "Warning: the App lacks these permissions: checks. Grant them in the App "
                   "settings, then accept them on the installation.\n"
                   "Restarted to apply it: symphony-daemon, symphony-merger, symphony-api.\n")
    assert bodies(admin, "/v1/setup/github-app") == [
        {"app_id": "101", "installation_id": "7", "private_key": PEM}]
    key = tmp_path / "app.pem"
    key.write_text(PEM)
    code, out, _ = run(capsys, "setup", "github-app", "--app-id", "999", "--installation-id", "7",
                       "--private-key-file", str(key), "--json")
    assert json.loads(out)["error"]["code"] == "github_app_unauthorized"
    # Not a PEM: refused before the request.
    before = len(admin.requests)
    code, _, err = run(capsys, "setup", "github-app", "--app-id", "101", "--installation-id", "7",
                       stdin="hello", monkeypatch=monkeypatch)
    assert err == "error: The private key must be the PEM file GitHub gave you for the App.\n"
    assert len(admin.requests) == before
    code, _, err = run(capsys, "setup", "github-app", "--app-id", "1", "--installation-id", "7",
                       "--private-key-file", str(tmp_path / "missing.pem"))
    assert err == f"error: Cannot read {tmp_path / 'missing.pem'}.\n"


def test_repositories_set_and_clear_the_default(admin, capsys):
    code, out, _ = run(capsys, "setup", "repositories", "--default-repo", "ACME/Web")
    assert code == 0
    assert out == ("Default repository: acme/web.\nWarning: no merger is registered for acme/web "
                   "yet. Deploy a merger for each repository.\n")
    code, out, _ = run(capsys, "setup", "repositories", "--clear-default-repo")
    assert out == ("No default repository. Each ticket must name its repository with a "
                   "`repo:` label.\n")
    assert bodies(admin, "/v1/setup/repositories") == [{"default_repo": "acme/web"},
                                                        {"default_repo": None}]
    code, _, err = run(capsys, "setup", "repositories", "--default-repo", "web")
    assert code == 1 and err == "error: Use owner/name, not 'web'.\n"
    admin.setup["github_app"] = {"repositories": ["acme/web"]}
    code, _, err = run(capsys, "setup", "repositories", "--default-repo", "acme/other")
    assert err == ("error: The GitHub App is not installed on this repository. Details: the "
                   "GitHub App is not installed on acme/other\n")


def test_notifications_one_channel_at_a_time(admin, capsys, monkeypatch):
    code, _, err = run(capsys, "setup", "notifications", "test")
    assert code == 1 and err == "error: No Slack or Discord channel is configured.\n"
    code, out, err = run(capsys, "setup", "notifications", "slack", "--channel", "C123",
                         stdin="xoxb-good\n", monkeypatch=monkeypatch)
    assert code == 0, err
    assert out == ("Slack saved. The test message was sent.\n"
                   "Restarted to apply it: symphony-daemon.\n")
    code, out, _ = run(capsys, "setup", "notifications", "discord", "--webhook", "--json",
                       stdin="https://discord.test/api/webhooks/1/abc\n", monkeypatch=monkeypatch)
    doc = json.loads(out)
    assert doc["slack"]["configured"] and doc["discord"]["configured"]
    assert bodies(admin, "/v1/setup/notifications") == [
        {"slack": {"bot_token": "xoxb-good", "channel": "C123"}},
        {"discord": {"webhook_url": "https://discord.test/api/webhooks/1/abc"}}]
    admin.setup_failing_channels.add("discord")
    code, out, _ = run(capsys, "setup", "notifications", "test")
    assert code == 1  # one channel failed; both results are still printed
    assert out == ("  ✓ Slack: test message sent\n"
                   "  ✗ Discord: Discord could not be reached. Try again in a moment.\n")
    code, out, _ = run(capsys, "setup", "notifications", "test", "--json")
    assert code == 1
    assert json.loads(out) == {"results": [
        {"channel": "slack", "ok": True, "at": 2, "error": None},
        {"channel": "discord", "ok": False, "at": 2, "error": {
            "code": "unreachable", "message": "Discord could not be reached. Try again in a moment."}}]}
    # The test message fails, so nothing is stored.
    code, _, err = run(capsys, "setup", "notifications", "slack", "--channel", "C1",
                       stdin="xoxb-broken\n", monkeypatch=monkeypatch)
    assert err == ("error: The test message failed, so nothing was saved. Check the token, the "
                   "channel or the URL. Details: the slack test message was rejected; nothing "
                   "was stored\n")
    assert admin.setup["slack"] == {"bot_token": "xoxb-good", "channel": "C123"}
    code, out, _ = run(capsys, "setup", "notifications", "discord", "--remove")
    assert out.startswith("Discord removed.\n")
    assert bodies(admin, "/v1/setup/notifications")[-1] == {"discord": None}
    assert admin.setup["slack"] and not admin.setup["discord"]


def test_webhook_secrets_are_shown_once_with_a_warning(admin, capsys):
    code, out, _ = run(capsys, "setup", "webhooks")
    assert code == 0
    assert out.startswith("Save these secrets now. Symphony shows them only once.\n")
    assert f"Linear\n  URL:    {admin.url}/v1/webhooks/linear\n  Secret: lin_whsec_1\n" in out
    assert "  Secret: gh_whsec_1\n  Events: pull_request, check_suite, push" in out
    assert out.endswith("Restarted to apply it: symphony-api.\n")
    code, _, err = run(capsys, "setup", "webhooks")
    assert code == 1 and err == ("error: Webhook secrets exist already. Run `symphony setup "
                                 "webhooks --rotate` to replace them.\n")
    code, out, _ = run(capsys, "setup", "webhooks", "--rotate", "--json")
    assert json.loads(out)["github"]["secret"] == "gh_whsec_2"


def test_viewer_reads_the_checklist_but_cannot_write(home, keychain, fake_api, capsys):  # noqa: F811
    login(capsys, fake_api)
    assert run(capsys, "setup", "--status")[0] == 0
    code, _, err = run(capsys, "setup", "repositories", "--default-repo", "acme/web")
    assert code == 1 and err == f"error: {render.ERROR_TEXT['forbidden']}\n"


def test_assistant_step_sends_the_app_token_on_stdin_only(admin, capsys, monkeypatch):
    code, _, err = run(capsys, "setup", "assistant")
    assert code == 1 and err == "error: Give at least one --slack-channel or --discord-channel.\n"
    code, _, err = run(capsys, "setup", "assistant", "--slack-channel", "C1",
                       stdin="xapp-bad\n", monkeypatch=monkeypatch)
    assert code == 1 and err == f"error: {render.ERROR_TEXT['slack_app_token_rejected']}\n"
    code, out, _ = run(capsys, "setup", "assistant", "--slack-channel", "C1", "--slack-channel",
                       "C2", "--discord-channel", "123", "--repo", "Acme/App", "--json",
                       stdin="xapp-good\n", monkeypatch=monkeypatch)
    assert code == 0
    assert json.loads(out) == {
        "slack": {"connected": True, "channels": ["C1", "C2"]},
        "discord": {"connected": True, "channels": ["123"], "intent": "full"},
        "repositories": ["acme/app"], "restarted": ["symphony-assistant"]}
    # An empty stdin keeps the stored app token; no --repo keeps the repositories.
    code, out, _ = run(capsys, "setup", "assistant", "--slack-channel", "C1", stdin="",
                       monkeypatch=monkeypatch)
    assert code == 0 and out.startswith("Assistant saved. It answers in:\n  Slack: C1\n")
    assert "Repositories: acme/app." in out and "Restarted to apply it: symphony-assistant." in out
    sent = bodies(admin, "/v1/setup/assistant")
    assert sent[1] == {"slack": {"channels": ["C1", "C2"], "app_token": "xapp-good"},
                       "discord": {"channels": ["123"]}, "repositories": ["acme/app"]}
    assert sent[2] == {"slack": {"channels": ["C1"]}}
    # Discord alone asks for no token.
    code, _, _ = run(capsys, "setup", "assistant", "--discord-channel", "9", stdin="xapp-x\n",
                     monkeypatch=monkeypatch)
    assert code == 0 and bodies(admin, "/v1/setup/assistant")[-1] == {
        "discord": {"channels": ["9"]}}


def test_assistant_status_reads_the_setup_step(admin, capsys):
    code, out, _ = run(capsys, "assistant", "status")
    assert code == 0 and out == ("The assistant is off. Turn it on with `symphony setup "
                                 "assistant`.\n")
    admin.setup.update(assistant={"slack": {"channels": ["C1"]}, "discord": None,
                                  "repositories": None}, app_token="xapp-good")
    code, out, _ = run(capsys, "assistant", "status")
    assert out.startswith("The assistant is not running.\n  Slack: - · channels C1\n")
    admin.assistant_beat = {"at": 1, "state": "running", "slack": {"connected": True},
                            "discord": None, "threads": 2}
    code, out, _ = run(capsys, "assistant", "status", "--json")
    doc = json.loads(out)
    assert doc["running"] is True and doc["threads"] == 2 and doc["status"] == "done"
    assert doc["slack"] == {"configured": True, "connected": True, "channels": ["C1"]}
    code, out, _ = run(capsys, "assistant", "status")
    assert "The assistant is running.\n" in out and "  Slack: connected · channels C1\n" in out
    assert "  active threads: 2\n  repositories: all" in out
    code, out, _ = run(capsys, "setup", "--status")
    assert "Assistant (optional)" in out and "Slack: 1 channel, connected; running, 2 threads" in out


def test_admin_mcp_commands(admin, capsys, monkeypatch):
    code, out, _ = run(capsys, "admin", "mcp", "list")
    assert out == "No MCP servers. Add one with `symphony admin mcp set`.\n"
    code, out, _ = run(capsys, "admin", "mcp", "set", "docs", "--transport", "http", "--url",
                       "https://mcp.example.com/mcp", "--role", "assistant", "--role",
                       "researcher", "--timeout-ms", "5000", "--json")
    assert code == 0 and json.loads(out) == {
        "name": "docs", "transport": "http", "url": "https://mcp.example.com/mcp",
        "roles": ["assistant", "researcher"], "enabled": True, "timeout_ms": 5000,
        "headers_set": []}
    code, _, err = run(capsys, "admin", "mcp", "set", "bad", "--transport", "http", "--url",
                       "http://x", "--role", "assistant")
    assert err == (f"error: {render.ERROR_TEXT['invalid_mcp_server']} Details: url: use "
                   "https://\n")
    code, _, err = run(capsys, "admin", "mcp", "set", "x", "--transport", "stdio", "--role", "planner")
    assert code == 1 and "needs --command" in err
    code, out, _ = run(capsys, "admin", "mcp", "set", "local", "--transport", "stdio",
                       "--command", "npx", "--arg=-y", "--arg", "pkg", "--env", "MODE=ro",
                       "--role", "planner", "--disabled", "--json")
    assert json.loads(out)["env"] == {"MODE": "ro"} and json.loads(out)["enabled"] is False
    # A failing test exits 1; headers travel on stdin, never in argv or the output.
    code, out, _ = run(capsys, "admin", "mcp", "test", "docs")
    assert code == 1 and out == "docs failed (rejected): the server answered 401\n"
    code, out, _ = run(capsys, "admin", "mcp", "headers", "docs", "--json",
                       stdin='{"headers": {"Authorization": "Bearer s3cret"}}',
                       monkeypatch=monkeypatch)
    assert code == 0 and json.loads(out) == {"headers_set": ["Authorization"]}
    code, out, _ = run(capsys, "admin", "mcp", "headers", "docs", stdin='{"X-Key": "k"}',
                       monkeypatch=monkeypatch)
    assert out == "Headers of docs saved. Stored: Authorization, X-Key.\n" and "k\"" not in out
    code, _, err = run(capsys, "admin", "mcp", "headers", "docs", stdin="[1]",
                       monkeypatch=monkeypatch)
    assert code == 1 and "JSON object on stdin" in err
    code, out, _ = run(capsys, "admin", "mcp", "test", "docs", "--json")
    assert code == 0 and json.loads(out)["tools"] == ["echo", "lookup_ticket_context"]
    code, out, _ = run(capsys, "admin", "mcp", "test", "local")
    assert code == 0 and out == "local: stdio servers are tested when an agent starts.\n"
    code, out, _ = run(capsys, "admin", "mcp", "list")
    assert "docs   http       on       assistant,researcher  Authorization,X-Key" in out
    assert "npx -y pkg" in out and "s3cret" not in out
    code, out, _ = run(capsys, "admin", "mcp", "remove", "local", "--json")
    assert json.loads(out) == {"removed": "local"}
    code, out, _ = run(capsys, "admin", "mcp", "remove", "local", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "not_found"
    assert all("s3cret" not in " ".join(map(str, r[:2])) for r in admin.requests)


# -- the wizard ---------------------------------------------------------------------

class Script:
    """Answers for the wizard's prompts, in order; records each prompt it was asked."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        assert self.answers, f"no answer left for {prompt!r}"
        return self.answers.pop(0)


def wizard(api, token: str, asks: list[str], secrets: list[str]):
    out = io.StringIO()
    ask, secret = Script(asks), Script(secrets)
    w = setup.Wizard(Api(creds.Credential(url=api.url, token=token)), out, ask=ask,
                     secret=secret, style=setup.Style(out))
    return w, out, ask, secret


def test_wizard_walks_every_step_until_ready(fake_api, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text(PEM)
    w, out, ask, secret = wizard(fake_api, ADMIN, [
        "101", "7", str(key),        # GitHub App first: the GitHub tracker needs it
        "linear", "ENG",            # tracker, first try: the key is refused
        "",                          # try again? yes (default)
        "linear", "ENG",            # tracker, second try
        "",                          # default repository: the App's only repository
        "",                          # models are done: change them? no (default)
        "slack", "bot", "C123",      # notifications
        "slack", "C0SYMPHONY", "",   # assistant: Slack, one channel, every repository
        "",                          # create the webhook secrets? yes (default)
    ], ["lin_api_bad", "lin_api_good", "xoxb-good", "xapp-good"])
    assert w.run() == 0
    text = out.getvalue()
    assert text.startswith(setup.BANNER.rstrip("\n"))
    assert "■□□□□□□ Step 1 of 7: GitHub App" in text
    assert "■■■■■■□ Step 6 of 7: Assistant (optional)" in text
    assert "■■■■■■■ Step 7 of 7: Webhooks (optional)" in text
    assert "Assistant saved. It answers in:\n  Slack: C0SYMPHONY\n" in text
    assert f"✗ {render.ERROR_TEXT['linear_unauthorized']}" in text
    assert "Tracker saved: Linear." in text
    assert "Default repository: acme/web." in text
    assert "  ✓ done · 1 role on amazon-bedrock" in text
    assert "Slack saved. The test message was sent." in text
    assert "Secret: lin_whsec_1" in text
    assert "Symphony setup is complete." in text and "Symphony is ready." in text
    assert text.rstrip().endswith("Then follow it with `symphony status` or in Tern.")
    # Secrets come from the hidden prompt only, and never reach the screen.
    assert secret.prompts == ["  Linear API key (hidden): "] * 2 + [
        "  Slack bot token (hidden): ",
        "  Slack app-level token xapp-… (hidden, empty keeps the stored one): "]
    assert "lin_api_good" not in text and "xoxb-good" not in text and "xapp-good" not in text
    assert bodies(fake_api, "/v1/setup/assistant") == [
        {"slack": {"channels": ["C0SYMPHONY"], "app_token": "xapp-good"}, "repositories": []}]
    assert "\x1b[" not in text
    assert bodies(fake_api, "/v1/setup/repositories") == [{"default_repo": "acme/web"}]
    assert not ask.answers and not secret.answers


def test_wizard_replaces_webhook_secrets_only_when_asked(fake_api):
    fake_api.setup.update(tracker={"kind": "linear", "team_key": "ENG"},
                          github_app={"repositories": ["acme/web"]}, default_repo="acme/web",
                          webhooks=1)
    w, out, ask, _ = wizard(fake_api, ADMIN, ["", "", "", "", "skip", "skip", ""], [])
    assert w.run() == 0
    assert "  The secrets exist and cannot be shown again. Replace them? The old ones stop " \
           "working. [y/N] " in ask.prompts
    assert bodies(fake_api, "/v1/setup/webhooks") == []


def test_wizard_for_a_non_admin_shows_the_checklist(fake_api):
    w, out, ask, _ = wizard(fake_api, "sym_o1_operator", [], [])
    assert w.run() == 1
    text = out.getvalue()
    assert "Symphony setup is not complete" in text
    assert text.rstrip().endswith("Only an admin can change the setup. Ask an admin to run "
                                  "`symphony setup`.")
    assert ask.prompts == []


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_color_only_on_a_terminal_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert setup.Style(Tty())("ok", "green") == "\x1b[32mok\x1b[0m"
    assert setup.Style(io.StringIO())("ok", "green") == "ok"
    monkeypatch.setenv("NO_COLOR", "1")
    assert setup.Style(Tty())("ok", "green") == "ok"


def test_spinner_clears_its_line_on_a_terminal():
    out = Tty()
    with setup.spinner(setup.Style(out), out, "Verifying"):
        pass
    assert "Verifying…" in out.getvalue() and out.getvalue().endswith("\r\x1b[2K")
    plain = io.StringIO()
    with setup.spinner(setup.Style(plain), plain, "Verifying"):
        pass
    assert plain.getvalue() == "Verifying…\n"
