"""Headless end-to-end tests of the Symphony Tern plugin (tern-plugin/).

Each test drives a real `tern serve` with the plugin loaded as a fixture, against the fake API
(fake_api.py) and the stub CLI (bin/symphony); see harness.py. Skipped when `tern` isn't
installed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import TOKEN, Harness, tern_binary  # noqa: E402

pytestmark = pytest.mark.skipif(tern_binary() is None, reason="Tern is not installed")


def no_errors(h: Harness) -> None:
    assert h.plugin_errors() == []


def test_dashboard_shows_waiting_input_first_and_answers_through_the_cli():
    with Harness() as h:
        h.run("plugin.symphony.dashboard")
        text = h.wait_text("Needs input")
        assert text.index("ENG-139") < text.index("ENG-142")
        assert "Should /v1/invoices paginate" in text
        h.key("a")  # picker with the agent's options; Enter takes the first
        time.sleep(0.6)
        h.key("Enter")
        w = h.wait_write("POST", "/v1/sessions/iss-139:2/messages")
        assert w["body"] == {"text": "Cursor, with offset for compatibility", "mode": "auto"}
        assert ["answer", "iss-139:2", "Cursor, with offset for compatibility", "--json"] in h.calls()
        h.wait_text("Answer sent to ENG-139", "sf-dock")
        h.wait_gone("Should /v1/invoices paginate")
        no_errors(h)


def test_other_omp_builtin_provider_gets_a_key_from_the_catalog_picker():
    with Harness() as h:
        h.run("plugin.symphony.admin")
        h.wait_text("Generation 3 rollout")
        h.key("]")
        text = h.wait_text("Other omp provider")
        # Featured built-ins first, then the stored custom provider.
        order = ["Anthropic", "OpenAI", "Amazon Bedrock", "Google", "Internal vLLM"]
        assert [text.index(name) for name in order] == sorted(text.index(name) for name in order)
        assert "groq" not in text
        h.click("[data-id$='.builtin_other']")
        time.sleep(0.6)
        # Tern's native picker (`.tn-pick`), outside the plugin surface.
        picker = " ".join(n.get("text", "") for n in h.ctl("tree", ".tn-pick .ck-label").get("nodes", []))
        assert "groq" in picker
        assert "google-vertex" not in picker  # host credentials: no key to store (internals §13)
        h.type("groq")
        h.key("Enter")
        time.sleep(0.6)
        h.type("gsk-other-456")
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/providers/groq")
        assert w["if_match"] == '"4821"'
        assert w["body"] == {"builtin": True, "apiKey": "gsk-other-456"}
        text = h.wait_text("Key for groq saved", "sf-dock")
        text = h.wait_text("groq")
        assert "gsk-other-456" not in text
        no_errors(h)


def test_interrupt_asks_first_and_message_goes_to_say():
    with Harness() as h:
        h.run("plugin.symphony.sessions")
        h.wait_text("Needs input 1")
        h.key("down")  # ENG-142, running
        h.key("i")
        h.wait_text("Interrupt ENG-142?", "sf-dock")
        assert not [w for w in h.write_log() if w["path"].endswith("/interrupt")]
        h.key("Enter")
        assert h.wait_write("POST", "/v1/sessions/iss-142:1/interrupt")["body"] == {"reason": "Interrupted from Tern"}
        h.key("m")
        time.sleep(0.6)
        h.type("Use ISO format for dates")
        h.key("Enter")
        w = h.wait_write("POST", "/v1/sessions/iss-142:1/messages")
        assert w["body"] == {"text": "Use ISO format for dates", "mode": "auto"}
        no_errors(h)


def test_admin_publishes_a_staged_model_with_if_match_and_shows_rollout():
    with Harness() as h:
        h.run("plugin.symphony.admin")
        h.wait_text("Generation 3 rollout")
        assert "4 of 7 consumers" in h.text()
        h.key("down")
        h.key("down")  # oracle
        h.key("Enter")  # provider picker
        time.sleep(0.8)
        h.type("bedrock")
        h.key("Enter")
        time.sleep(1.0)
        h.type("sonnet")
        h.key("Enter")
        time.sleep(0.6)
        h.key("Enter")  # keeps the current thinking level (listed first)
        h.wait_text("Publish 1 change")
        assert not [w for w in h.write_log() if w["path"] == "/v1/admin/config"]
        h.key("p")
        time.sleep(0.6)
        h.type("Sonnet on the oracle to cut cost")
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/config")
        assert w["if_match"] == "3"
        assert w["body"] == {
            "models": {"oracle": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-sonnet-5", "thinking": "medium"}},
            "reason": "Sonnet on the oracle to cut cost",
        }
        h.wait_text("Generation 4 rollout")
        assert "1 of 7 consumers" in h.text()
        no_errors(h)


def test_created_token_is_shown_once_and_never_persisted():
    with Harness() as h:
        h.run("plugin.symphony.admin")
        h.wait_text("Generation 3")
        h.key("]")
        h.key("]")
        h.wait_text("Team API tokens")
        h.key("n")
        time.sleep(0.5)
        h.type("carol")
        h.key("Enter")
        time.sleep(0.5)
        h.key("Enter")  # operator (current default first)
        time.sleep(0.5)
        h.key("Enter")  # 90 days
        w = h.wait_write("POST", "/v1/admin/tokens")
        assert w["body"] == {"name": "carol", "role": "operator", "expires_days": 90}
        text = h.wait_text("Token created: carol")
        created = next(line for line in text.splitlines() if line.startswith("sym_"))
        h.click("[data-id$='.token_dismiss']")
        h.wait_gone("Token created: carol")
        assert created not in h.text()
        on_disk = "".join(
            p.read_text(errors="replace") for p in (h.tmp / "config").rglob("*") if p.is_file()
        )
        assert created not in on_disk and TOKEN not in on_disk
        assert TOKEN not in h.serve_log.read_text(errors="replace")
        no_errors(h)


def test_builtin_provider_key_travels_on_stdin_and_never_renders():
    with Harness() as h:
        h.run("plugin.symphony.admin")
        h.wait_text("Generation 3")
        h.key("]")
        h.wait_text("no key")
        h.key("down")  # openai: built-in of the catalog, nothing stored yet
        h.key("c")
        time.sleep(0.6)
        h.type("sk-test-123")
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/providers/openai")
        assert w["if_match"] == '"4821"'
        assert w["body"] == {"builtin": True, "apiKey": "sk-test-123"}
        assert all("sk-test-123" not in arg for call in h.calls() for arg in call)
        h.wait_text("Key for OpenAI saved", "sf-dock")
        text = h.wait_text("Replace key")
        assert "sk-test-123" not in text
        no_errors(h)


def test_own_provider_key_resends_its_definition():
    with Harness() as h:
        h.run("plugin.symphony.admin")
        h.wait_text("Generation 3")
        h.key("]")
        h.wait_text("Internal vLLM")
        for _ in range(4):
            h.key("down")
        h.key("c")
        time.sleep(0.6)
        h.type("vllm-key-789")
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/providers/vllm-internal")
        assert w["body"] == {"name": "Internal vLLM", "api": "openai-completions", "baseUrl": "https://vllm.acme.internal/v1",
                             "auth": "apiKey", "models": [{"id": "qwen3-coder"}], "apiKey": "vllm-key-789"}
        no_errors(h)


def test_merger_shows_each_repository_and_actions_name_theirs():
    with Harness() as h:
        h.run("plugin.symphony.merger")
        text = h.wait_text("Queue and parked 3")
        assert "acme/gateway" in text and "acme/web" in text and "merger-web-0" in text
        h.key("down")  # acme/gateway #790 first (repositories in order), then acme/web #821
        h.key("b")
        time.sleep(0.6)
        h.type("Approved hotfix")
        h.key("Enter")
        w = h.wait_write("POST", "/v1/merger/prs/821/bypass")
        assert w["body"] == {"reason": "Approved hotfix", "repo": "acme/web"}
        h.wait_text("Bypassed acme/web #821", "sf-dock")
        no_errors(h)


def test_onboarding_logs_in_with_token_on_stdin_then_installs():
    with Harness(logged_in=False) as h:
        h.run("plugin.symphony.dashboard")
        h.wait_text("You are not signed in to Symphony")
        h.key("Enter")
        h.wait_text("1 · Symphony address")
        h.type(h.url)
        h.key("Tab")
        h.type(TOKEN)
        assert TOKEN not in h.text()  # masked
        h.key("Enter")
        h.wait_text("Next step: install in Tern")
        login = next(c for c in h.calls() if c[0] == "login")
        assert login == ["login", "--url", h.url, "--token", "-", "--json"]
        assert json.loads((h.stub_home / "creds.json").read_text())["token"] == TOKEN
        h.key("Enter")
        h.wait_text("All set in Tern")
        # The panel already runs from the installed plugin: setup must not reinstall it.
        assert ["tern", "install", "--no-plugin", "--json"] in h.calls()
        h.key("Enter")  # opens the panel, which re-reads the credential
        h.wait_text("Needs input")
        no_errors(h)


def test_offline_keeps_last_data_and_says_so():
    with Harness() as h:
        h.run("plugin.symphony.dashboard")
        h.wait_text("Needs input")
        assert h.api is not None
        h.api.terminate()
        h.api.wait(5)
        h.wait_text("No connection to Symphony", "sf-dock")
        assert "Offline" in h.text()
        assert "ENG-139" in h.text()
        no_errors(h)


def test_viewer_sees_no_actions_and_no_admin_tab():
    with Harness(role="viewer", state="empty") as h:
        h.run("plugin.symphony.dashboard")
        text = h.wait_text("No active sessions")
        assert "Admin" not in text and "Pause" not in text
        assert "interrupt" not in h.text("sf-dock")
        no_errors(h)


PEM = "-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEAtest -----END RSA PRIVATE KEY-----"


def test_setup_opens_for_an_admin_and_verifies_each_step_through_the_cli():
    with Harness(state="unconfigured") as h:
        h.run("plugin.symphony.dashboard")
        # Opened by itself: the backend is not set up.
        text = h.wait_text("Linear API key", timeout=15)
        assert "Setup" in text and "Tracker" in text and "Verify and save" in text
        h.type("lin_api_good_key")
        h.key("Tab")
        h.type("OPS")
        h.key("Enter")
        h.wait_text("Linear has no team with the key OPS.")  # verified before it is stored
        h.key("alt+Backspace")
        h.type("ENG")
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/setup/tracker")
        assert w["body"] == {"kind": "linear", "api_key": "lin_api_good_key", "team_key": "OPS"}
        text = h.wait_text("Verified and saved")
        assert "Board states" in text and "In Progress" in text
        assert "lin_api_good_key" not in text
        w = [x for x in h.write_log() if x["path"] == "/v1/setup/tracker"][-1]
        assert w["body"]["team_key"] == "ENG"
        h.click("[data-id$='.s_github_app']")
        h.wait_text("Installation ID")
        h.type("123456")
        h.key("Tab")
        h.type("45678901")
        h.key("Tab")
        h.type(PEM)
        h.key("Enter")
        assert h.wait_write("PUT", "/v1/setup/github-app")["body"]["private_key"].startswith("-----BEGIN RSA")
        text = h.wait_text("3 found")
        assert "acme/web, acme/billing, acme/gateway" in text and "MIIEowIBAAKCAQEAtest" not in text
        h.click("[data-id$='.s_repositories']")
        h.wait_text("Tickets without a repo: label go here.")
        h.type("acme/web")
        h.key("Enter")
        assert h.wait_write("PUT", "/v1/setup/repositories")["body"] == {"default_repo": "acme/web"}
        h.wait_text("Symphony is ready")
        # Secrets went to the CLI on stdin: never in an argument list.
        for secret in ("lin_api_good_key", "MIIEowIBAAKCAQEAtest"):
            assert all(secret not in arg for call in h.calls() for arg in call)
        assert ["setup", "tracker", "--kind", "linear", "--team", "ENG", "--json"] in h.calls()
        no_errors(h)


def test_setup_webhook_secrets_are_shown_once_and_notifications_test():
    with Harness(state="unconfigured") as h:
        h.run("plugin.symphony.setup")
        h.wait_text("Linear API key", timeout=15)
        h.click("[data-id$='.s_notifications']")
        h.wait_text("Slack channel")
        h.type("#eng-symphony")
        h.key("Tab")
        h.type("xoxb-test-token")
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/setup/notifications")
        assert w["body"] == {"slack": {"channel": "#eng-symphony", "bot_token": "xoxb-test-token"}}
        h.wait_text("Verified and saved")  # the PUT is back: one CLI call at a time
        h.click("[data-id$='.test.b']")
        h.wait_write("POST", "/v1/setup/notifications/test")
        h.wait_gone("Sending…")
        h.wait_text("test message sent")
        h.click("[data-id$='.s_webhooks']")
        h.wait_text("Create webhook secrets")
        h.key("Enter")
        text = h.wait_text("The secrets are not shown again")
        linear_secret = hashlib.sha256(b"linear").hexdigest()
        assert "/v1/webhooks/linear" in text and linear_secret[:16] in text
        h.click("[data-id$='.webhooks_dismiss.b']")
        h.wait_gone(linear_secret[:16])
        assert all("xoxb-test-token" not in arg for call in h.calls() for arg in call)
        no_errors(h)


def test_viewer_sees_the_setup_banner_and_no_setup_view():
    with Harness(role="viewer", state="unconfigured") as h:
        h.run("plugin.symphony.dashboard")
        text = h.wait_text("Symphony is not set up yet", timeout=15)
        assert "Ask an admin to finish setup." in text and "Open setup" not in text
        time.sleep(1.0)
        assert "Verify and save" not in h.text()
        no_errors(h)


def test_notification_test_shows_which_channel_passed_when_one_fails():
    with Harness(state="unreachable-discord") as h:
        h.run("plugin.symphony.setup")
        h.wait_text("Linear API key", timeout=15)
        h.click("[data-id$='.s_notifications']")
        h.wait_text("Slack channel")
        h.key("ctrl+t")
        h.wait_write("POST", "/v1/setup/notifications/test")
        text = h.wait_text("Discord could not be reached")
        assert "test message sent" in text  # Slack passed
        assert "Not saved" not in text  # the CLI's exit 1 carries the per-channel answer
        no_errors(h)


def test_ctrl_p_pauses_the_daemon_with_a_reason():
    with Harness() as h:
        h.run("plugin.symphony.dashboard")
        h.wait_text("Needs input")
        assert "pause" in h.text("sf-dock")
        h.key("ctrl+p")
        time.sleep(0.6)
        h.type("Deploying main now")
        h.key("Enter")
        w = h.wait_write("POST", "/v1/commands")
        assert w["body"]["action"] == "pause" and w["body"]["reason"] == "Deploying main now"
        no_errors(h)


def open_mcp(h: Harness) -> str:
    h.run("plugin.symphony.admin")
    h.wait_text("Generation 3 rollout")
    for _ in range(3):
        h.key("]")
    return h.wait_text("MCP servers give agents more tools")


def test_admin_models_lists_the_assistant_roles():
    with Harness() as h:
        h.run("plugin.symphony.admin")
        text = h.wait_text("Generation 3 rollout")
        for role in ("Assistant", "Researcher", "Planner"):
            assert role in text
        roles = text[text.index("Probe"):]  # the Admin tab strip above also says "Assistant"
        assert roles.index("Assistant") < roles.index("Researcher") < roles.index("Planner")
        assert "answers questions in Slack and Discord" in text
        no_errors(h)


def test_admin_mcp_tab_lists_servers_and_tests_the_selected_one():
    with Harness() as h:
        text = open_mcp(h)
        for word in ("linear-docs", "local-files", "sentry", "1 header set", "disabled", "Streamable HTTP", "stdio"):
            assert word in text
        assert "lin_mcp_stored" not in text  # header values never come back
        assert "Agents call" in text and "/v1/mcp/linear-docs" in text  # first row, sorted by name
        h.key("Enter")
        text = h.wait_text("✓ Reached")
        assert "5 tools" in text and "search_issues" in text and "2025-11-25" in text
        assert ["admin", "mcp", "test", "linear-docs", "--json"] in h.calls()
        h.key("down")  # local-files: stdio, tested where the agent runs
        h.key("Enter")
        h.wait_text("stdio servers are tested when an agent starts")
        h.key("down")  # sentry: no stored header, the server says 401
        h.key("Enter")
        text = h.wait_text("Test failed")
        assert "The server rejected the stored headers" in text
        no_errors(h)


def test_admin_mcp_adds_a_server_with_its_header_masked_and_on_stdin():
    secret = "Bearer sk-mcp-docs-4f2a9c"
    with Harness() as h:
        open_mcp(h)
        h.key("n")
        h.wait_text("New MCP server")
        h.type("Docs Search")  # not a valid name: refused before any call
        h.key("Enter")
        h.wait_text("Enter a name of lowercase letters")
        assert not [w for w in h.write_log() if w["path"].startswith("/v1/admin/mcp")]
        h.key("alt+Backspace")
        h.type("docs-search")
        h.key("Tab")  # transport: Streamable HTTP
        h.key("Tab")
        h.type("https://mcp.docs.example/mcp")
        h.key("Tab")
        h.type("researcher, planner")
        h.key("Tab")  # state: enabled
        h.key("Tab")
        h.type("Authorization")
        h.key("Tab")
        h.type(secret)
        assert secret not in h.text()  # masked while typed
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/mcp/docs-search")
        assert w["body"] == {"transport": "http", "url": "https://mcp.docs.example/mcp",
                             "roles": ["researcher", "planner"], "enabled": True}
        w = h.wait_write("PUT", "/v1/admin/mcp/docs-search/headers")
        assert w["body"] == {"headers": {"Authorization": secret}}
        h.wait_text("Server docs-search added with the header Authorization", "sf-dock")
        text = h.wait_text("/v1/mcp/docs-search")
        assert secret not in text
        assert ["admin", "mcp", "headers", "docs-search", "--json"] in h.calls()
        assert all(secret not in arg for call in h.calls() for arg in call)
        h.key("Enter")
        h.wait_text("✓ Reached")
        no_errors(h)


def test_admin_mcp_toggle_edit_headers_and_remove_go_through_the_cli():
    with Harness() as h:
        open_mcp(h)
        h.key("down")
        h.key("down")  # sentry
        h.key("d")  # enable: the whole definition is sent again, timeout kept
        w = h.wait_write("PUT", "/v1/admin/mcp/sentry")
        assert w["body"] == {"transport": "sse", "url": "https://mcp.sentry.dev/sse",
                             "roles": ["implementer", "developer"], "enabled": True, "timeout_ms": 20000}
        h.wait_text("Server sentry enabled", "sf-dock")
        h.key("e")
        h.wait_text("Edit sentry")
        h.key("Tab")  # from URL to roles
        h.key("alt+Backspace")
        h.type("reviewer")
        h.key("Enter")
        h.wait_text("Server sentry saved", "sf-dock")
        puts = [x for x in h.write_log() if x["path"] == "/v1/admin/mcp/sentry"]
        assert puts[-1]["body"]["roles"] == ["reviewer"]
        h.key("up")
        h.key("up")  # linear-docs
        h.key("h")
        h.wait_text("Secret headers")
        h.key("Tab")  # the value: left empty, it removes the stored header
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/mcp/linear-docs/headers")
        assert w["body"] == {"headers": {"Authorization": None}}
        h.wait_text("Header Authorization removed from linear-docs", "sf-dock")
        h.key("down")  # local-files
        h.key("x")
        h.wait_text("Remove the MCP server local-files?", "sf-dock")
        assert not [x for x in h.write_log() if x["method"] == "DELETE"]
        h.key("Enter")
        h.wait_write("DELETE", "/v1/admin/mcp/local-files")
        h.wait_text("Server local-files removed", "sf-dock")
        h.wait_gone("local-files")
        assert ["admin", "mcp", "remove", "local-files", "--json"] in h.calls()
        no_errors(h)


def test_admin_mcp_empty_state_offers_add():
    with Harness(state="empty") as h:
        h.run("plugin.symphony.admin")
        h.wait_text("Generation 3 rollout")
        for _ in range(3):
            h.key("]")
        h.wait_text("No MCP servers yet")
        h.key("n")
        h.wait_text("New MCP server")
        h.key("Escape")
        h.wait_text("No MCP servers yet")
        no_errors(h)


def test_sessions_show_assistant_threads_with_their_role_and_place():
    with Harness() as h:
        h.run("plugin.symphony.sessions")
        text = h.wait_text("slack C0123ABCD")
        assert "assistant · research · Slack · C0123ABCD · thread 1731700000.000200" in text
        assert "assistant · planning · Discord · DM" in text
        h.key("down")
        h.key("down")  # ENG-139 waits, ENG-142 runs, then the Slack thread (8 s ago)
        text = h.wait_text("Asked by")
        assert "Researcher" in text and "Slack" in text and "C0123ABCD" in text and "bob" in text
        assert "Take over" not in text
        no_errors(h)


def test_setup_assistant_step_sends_the_app_token_on_stdin():
    token = "xapp-1-A0TEST-1234-abcdef"
    with Harness() as h:
        h.run("plugin.symphony.setup")
        h.wait_text("Slack app token", timeout=15)  # the first optional step not done yet
        h.click("[data-id$='.s_assistant']")
        h.type("C0123ABCD")
        h.key("Tab")
        h.type(token)
        assert token not in h.text()
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/setup/assistant")
        assert w["body"] == {"slack": {"channels": ["C0123ABCD"], "app_token": token}}
        text = h.wait_text("Verified and saved")
        assert "Slackconnected C0123ABCD" in text and token not in text
        assert ["setup", "assistant", "--slack-channel", "C0123ABCD", "--json"] in h.calls()
        assert all(token not in arg for call in h.calls() for arg in call)
        # Discord too: this team has no Discord bot token yet, and the API says so. The focus
        # stayed on the app token field.
        h.key("Tab")
        h.type("123456789012345678")
        h.key("Enter")
        h.wait_text("Set up Discord in Notifications first")
        bodies = [x["body"] for x in h.write_log() if x["path"] == "/v1/setup/assistant"]
        assert bodies[-1] == {"slack": {"channels": ["C0123ABCD"]}, "discord": {"channels": ["123456789012345678"]}}
        no_errors(h)


# ── Seiji: watching a device, the preset and the test's user and quota ────────

ENG_163 = "[data-id$='.kiss_163_1']"  # holds an iPhone 16 on the Seiji server
ENG_164 = "[data-id$='.kiss_164_1']"  # holds an Android emulator: no live screen yet


def kv_rows(h: Harness) -> str:
    """The focused panel's `kv` rows, one per line (`tree` caps the text of a whole `kv`)."""
    nodes = h.ctl("tree", ".tn-pane.on .sf-main .sf-kv-item").get("nodes", [])
    return "\n".join(n.get("text", "") for n in nodes)


def browser_text(h: Harness) -> str:
    """The text of Tern's browser panes (their address bars)."""
    return "\n".join(n.get("text", "") for n in h.ctl("tree", ".tn-pane.web").get("nodes", []))


def wait_browser(h: Harness, needle: str, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = browser_text(h)
        if needle in text:
            return text
        time.sleep(0.2)
    raise AssertionError(f"no browser shows {needle!r}; browsers show:\n{text}")


def viewer_calls(h: Harness) -> list[dict]:
    return [w for w in h.write_log() if w["path"] == "/v1/mcp/seiji/viewer"]


def test_watch_device_opens_a_view_only_screen_beside_the_panel():
    with Harness(state="seiji") as h:
        h.run("plugin.symphony.sessions")
        h.wait_text("ENG-163")
        h.click(ENG_163)
        text = h.wait_text("iphone-16")
        assert "Watch device" in text and "Watch with control" in text
        assert "·Deviceiphone-16 on seiji · l_3f9c2a7b1e04" in kv_rows(h)
        assert "watch device" in h.text("sf-dock")
        h.key("w")
        w = h.wait_write("POST", "/v1/mcp/seiji/viewer")
        assert w["body"] == {"lease_id": "l_3f9c2a7b1e04", "control": False}
        assert ["mcp", "viewer", "seiji", "l_3f9c2a7b1e04", "--json"] in h.calls()
        wait_browser(h, h.url + "/view/open/")
        h.click(".tn-pane.dim .sf-main")
        h.wait_text("Opening the screen of iphone-16 beside this panel, view only", "sf-dock")
        no_errors(h)


def test_watch_with_control_asks_first_and_says_others_may_be_watching():
    with Harness(state="seiji") as h:
        h.run("plugin.symphony.sessions")
        h.wait_text("ENG-163")
        h.click(ENG_163)
        h.wait_text("iphone-16")
        h.key("shift+w")
        dock = h.wait_text("Control iphone-16 of ENG-163?", "sf-dock")
        assert "Others may be watching" in dock and "Seiji plugin" in dock
        assert viewer_calls(h) == []
        h.key("Escape")
        h.wait_gone("Control iphone-16", "sf-dock")
        assert viewer_calls(h) == []
        h.click("[data-id$='.watch_control=iss-163:1']")
        h.wait_text("Control iphone-16 of ENG-163?", "sf-dock")
        h.key("Enter")
        w = h.wait_write("POST", "/v1/mcp/seiji/viewer")
        assert w["body"] == {"lease_id": "l_3f9c2a7b1e04", "control": True}
        assert ["mcp", "viewer", "seiji", "l_3f9c2a7b1e04", "--control", "--json"] in h.calls()
        wait_browser(h, h.url + "/view/open/")
        no_errors(h)


def test_watch_device_explains_android_and_a_released_lease():
    with Harness(state="seiji") as h:
        h.run("plugin.symphony.sessions")
        h.wait_text("ENG-164")
        h.click(ENG_164)
        h.wait_text("pixel-8")
        assert "·Devicepixel-8 on seiji · l_a1b2c3d4e5f6 no live screen yet" in kv_rows(h)
        h.key("w")
        h.wait_text("The live screen supports iOS simulators only for now", "sf-dock")
        # The agent releases the iPhone after the panel's last read: the API refuses the link.
        req = urllib.request.Request(h.url + "/v1/_test/leases/l_3f9c2a7b1e04/release", data=b"{}", method="POST",
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
        h.click(ENG_163)
        h.wait_text("iphone-16")
        h.key("w")
        h.wait_text("No Symphony session holds iphone-16 now", "sf-dock")
        assert browser_text(h) == ""
        no_errors(h)


def test_viewer_role_sees_the_device_but_cannot_watch_it():
    with Harness(state="seiji", role="viewer") as h:
        h.run("plugin.symphony.sessions")
        h.wait_text("ENG-163")
        h.click(ENG_163)
        text = h.wait_text("iphone-16")
        assert "Watch device" not in text and "watch device" not in h.text("sf-dock")
        h.key("w")
        time.sleep(0.5)
        assert viewer_calls(h) == []
        no_errors(h)


def test_admin_mcp_adds_a_seiji_server_with_the_preset():
    secret = "Bearer sj_test_9c2e5b1d4e8f7f3a"
    with Harness() as h:
        open_mcp(h)
        h.key("n")
        h.wait_text("New MCP server")
        h.key("shift+Tab")  # from the name back to the preset
        h.key("right")  # Seiji: the name, the transport and the header name are filled
        text = h.wait_text("Seiji's MCP endpoint")
        assert "\nSSE\n" not in text and "Authorization" in text  # no transport choice: the preset sets it
        h.key("Tab")  # name: seiji
        h.key("Tab")
        h.type("https://seiji.acme.dev/mcp")
        h.key("Tab")
        h.type("implementer")
        h.key("Tab")  # state: enabled
        h.key("Tab")  # header: Authorization
        h.key("Tab")
        h.key("Enter")  # no token yet: refused before any call
        h.wait_text("Enter the header value. It is Bearer and the Seiji token.")
        assert not [w for w in h.write_log() if w["path"].startswith("/v1/admin/mcp")]
        h.type(secret)
        assert secret not in h.text()
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/admin/mcp/seiji")
        assert w["body"] == {"preset": "seiji", "url": "https://seiji.acme.dev/mcp", "roles": ["implementer"],
                             "enabled": True}
        assert ["admin", "mcp", "set", "seiji", "--preset", "seiji", "--url", "https://seiji.acme.dev/mcp",
                "--role", "implementer", "--json"] in h.calls()
        w = h.wait_write("PUT", "/v1/admin/mcp/seiji/headers")
        assert w["body"] == {"headers": {"Authorization": secret}}
        h.wait_text("Agents call")
        rows = kv_rows(h)
        for row in ("·HTTP APIhttps://seiji.acme.dev through /v1/mcp/seiji/http",
                    "·Agent envSEIJI_TOKEN={token} SEIJI_URL={http_url}",
                    "·Cleanuprelease_device for each acquire_device lease when the session ends", "·Timeout3900000 ms"):
            assert row in rows
        assert all(secret not in arg for call in h.calls() for arg in call)
        h.key("Enter")
        text = h.wait_text("✓ Reached")
        assert "23 tools" in text and "Signed in as" in text and "symphony-agents" in text
        assert "6000 min a month" in text and "4 devices at once" in text
        no_errors(h)


def test_admin_mcp_toggle_keeps_the_seiji_fields():
    with Harness(state="seiji") as h:
        open_mcp(h)
        h.key("down")
        h.key("down")  # seiji (sorted: linear-docs, local-files, seiji, sentry)
        h.wait_text("Timeout")
        assert "·Agent envSEIJI_TOKEN={token} SEIJI_URL={http_url}" in kv_rows(h)
        h.key("d")
        w = h.wait_write("PUT", "/v1/admin/mcp/seiji")
        assert w["body"] == {
            "transport": "http", "url": "https://seiji.acme.dev/mcp", "roles": ["implementer", "developer"],
            "enabled": False, "timeout_ms": 3900000, "http_base": "https://seiji.acme.dev",
            "agent_env": {"SEIJI_TOKEN": "{token}", "SEIJI_URL": "{http_url}"},
            "cleanup": {"acquire_tool": "acquire_device", "id_field": "lease_id",
                        "release_tool": "release_device", "release_arg": "lease_id"},
        }
        h.wait_text("Server seiji disabled", "sf-dock")
        no_errors(h)


def open_assistant(h: Harness) -> str:
    h.run("plugin.symphony.admin")
    h.wait_text("Generation 3 rollout")
    for _ in range(5):
        h.key("]")
    return h.wait_text("Every read here is audited")


def memory_reads(h: Harness) -> list[str]:
    return [w["path"] for w in h.write_log() if w["method"] == "GET"]


def test_admin_assistant_shows_the_soul_people_and_items_and_wipes_after_a_confirm():
    with Harness() as h:
        text = open_assistant(h)
        assert "acme/web:.symphony/assistant.md" in text and "4b825dc642cb" in text
        assert "alice" in text and "2 items" in text and "bob" in text and "1 item" in text
        text = h.wait_text("deploys on Fridays after the release review")
        assert "prefers answers in Portuguese" in text and "inferred" in text
        time.sleep(5)  # a few polls: memory reads are audited, so they are not repeated
        assert memory_reads(h) == ["/v1/assistant/memory", "/v1/assistant/memory/alice"]
        h.key("down")
        h.wait_text("owns the billing service")
        h.key("up")
        h.wait_text("deploys on Fridays after the release review")
        h.key("x")
        h.wait_text("Wipe the memory of alice?", "sf-dock")
        assert not [w for w in h.write_log() if w["method"] == "DELETE"]
        h.key("Enter")
        h.wait_write("DELETE", "/v1/assistant/memory/alice")
        h.wait_text("Memory of alice wiped", "sf-dock")
        h.wait_gone("deploys on Fridays")
        assert "2 items" not in h.text() and "bob" in h.text()
        no_errors(h)


def test_repo_detail_shows_detected_values_and_saving_sends_merged_maps():
    with Harness() as h:
        h.run("plugin.symphony.repositories")
        text = h.wait_text("acme/web")
        assert "acme/billing" in text and "acme/gateway" in text
        # acme/web is auto-selected (first row); its detail card shows detected values.
        text = h.wait_text("node-22")
        assert "detected" in text.lower() or "Detected runtime" in text
        assert "frontend" in text  # detected component
        # Open the edit form for acme/web.
        h.key("e")
        h.wait_text("Save")
        h.wait_text("Cancel")
        # Tab to base_branch field and type a value.
        h.key("tab")
        time.sleep(0.3)
        h.type("develop")
        # Tab to components and add a component (field has detected "frontend=web@node-22").
        h.key("tab")
        time.sleep(0.3)
        h.type(" api=src/api@node")
        # Save.
        h.key("Enter")
        w = h.wait_write("PUT", "/v1/setup/repositories")
        body = w["body"]
        # The save sends the full maps: base_branches includes acme/web's new value.
        assert body["base_branches"]["acme/web"] == "develop"
        # Runtime: acme/web had node-22 from detected, so it's in the runtimes map.
        assert body["runtimes"]["acme/web"] == "node-22"
        # Components: acme/web gets both the kept detected one and the new one.
        assert body["components"]["acme/web"]["frontend"]["path"] == "web"
        assert body["components"]["acme/web"]["api"]["path"] == "src/api"
        assert body["components"]["acme/web"]["api"]["runtime"] == "node"
        h.wait_text("Configuration saved for acme/web", "sf-dock")
        no_errors(h)


def test_repo_non_admin_cannot_save():
    with Harness(role="viewer") as h:
        h.run("plugin.symphony.repositories")
        text = h.wait_text("acme/web")
        assert "acme/billing" in text
        # The detail card should not show a Configure button (non-admin).
        time.sleep(1)
        text = h.text()
        assert "Configure" not in text
        # Pressing e should not open a form.
        h.key("e")
        time.sleep(0.5)
        assert "Save" not in h.text()
        assert "Cancel" not in h.text()
        no_errors(h)


def wait_call(h: Harness, argv: list[str], timeout: float = 20) -> None:
    """Waits for the plugin to run `symphony ARGV`, without asking Tern anything meanwhile: a
    headless Tern answering `ctl` requests back to back can hold back the panel's poll timer."""
    deadline = time.monotonic() + timeout
    while argv not in h.calls():
        assert time.monotonic() < deadline, f"no {argv}; calls: {h.calls()}"
        time.sleep(0.2)


def test_the_panel_updates_an_older_cli_then_its_own_plugin():
    # The server runs a newer release than the CLI (a `uv tool` install) and this plugin.
    with Harness(server_version="99.0.0") as h:
        h.run("plugin.symphony.dashboard")
        wait_call(h, ["update", "--json"])
        wait_call(h, ["tern", "install", "--plugin-only", "--json"])
        h.wait_text("Updated the Symphony plugin to 99.0.0; reloading", "sf-dock")
        assert h.calls().index(["version", "--json"]) < h.calls().index(["update", "--json"])
        time.sleep(4.5)  # two more overviews: each target is tried once
        calls = h.calls()
        assert calls.count(["update", "--json"]) == 1
        assert calls.count(["tern", "install", "--plugin-only", "--json"]) == 1
        no_errors(h)


def test_a_plugin_as_new_as_the_server_starts_no_update():
    with Harness() as h:
        h.run("plugin.symphony.dashboard")
        h.wait_text("Needs input")
        time.sleep(4.5)
        calls = h.calls()
        assert not any(c[:1] in (["version"], ["update"]) or c[:2] == ["tern", "install"] for c in calls)
        no_errors(h)
