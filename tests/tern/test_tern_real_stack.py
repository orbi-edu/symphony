"""The Symphony Tern plugin against the real stack: Redis, the control API, the `symphony` CLI.

Unlike test_tern_plugin.py (fake API, stub CLI) nothing here is a stand-in for Symphony code:
data is seeded through the real stores, the plugin talks to the real API and writes through the
real CLI, and the chat opens through the real `symphony acp` bridge. Only the Kubernetes Secret
behind providers is in memory (see real_api.py). Skipped when Tern or redis-server is absent.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import tern_binary  # noqa: E402
from real_stack import BOOTSTRAP, LINEAR_MOCK_KEY, NAMESPACE, RealHarness, redis_binary  # noqa: E402

pytestmark = pytest.mark.skipif(
    tern_binary() is None or redis_binary() is None, reason="Tern and redis-server are required"
)


@pytest.fixture(scope="module")
def stack():
    with RealHarness() as h:
        h.run("plugin.symphony.dashboard")
        h.leave_setup()
        yield h


def no_errors(h: RealHarness) -> None:
    assert h.plugin_errors() == []


def intents(h: RealHarness, sid: str) -> list[tuple[str, dict]]:
    from symphony.sessions import SessionStore

    return [(kind, payload) for _, kind, _, payload in SessionStore(h.redis, NAMESPACE).read_intents(sid, "0-0")]


def test_dashboard_reads_the_real_overview_and_answers_through_the_cli(stack):
    h = stack
    h.run("plugin.symphony.dashboard")
    text = h.wait_text("Needs input")
    assert "Should /v1/invoices paginate" in text
    assert "bootstrap (admin)" in text
    h.key("a")
    time.sleep(0.8)
    h.key("Enter")
    h.wait_until(lambda: intents(h, h.ids["waiting"]), what="answer intent")
    kind, payload = intents(h, h.ids["waiting"])[0]
    assert kind == "answer"
    assert payload == {"text": "Cursor, with offset for compatibility", "request_id": "ask-1"}
    h.wait_text("Answer sent to ENG-139", "sf-dock")
    no_errors(h)


def test_interrupt_and_message_become_runner_intents(stack):
    h = stack
    h.run("plugin.symphony.sessions")
    h.wait_text("Needs input")
    h.click("[data-id$='.kiss_142_1']")  # ENG-142's row: list keys come from the session id
    time.sleep(0.3)
    h.key("i")
    h.wait_text("Interrupt ENG-142?", "sf-dock")
    h.key("Enter")
    h.wait_until(lambda: [k for k, _ in intents(h, h.ids["running"])] == ["interrupt"], what="interrupt")
    h.key("m")
    time.sleep(0.6)
    h.type("Use ISO format for dates")
    h.key("Enter")
    h.wait_until(lambda: len(intents(h, h.ids["running"])) == 2, what="message intent")
    assert intents(h, h.ids["running"])[1] == ("steer", {"text": "Use ISO format for dates"})
    no_errors(h)


def test_publish_picks_from_the_real_catalog_and_bumps_the_generation(stack):
    h = stack
    h.run("plugin.symphony.admin")
    h.wait_text("Generation 1 rollout")
    h.key("down")
    h.key("down")  # oracle
    # The provider picker opens once the host half has decoded the real catalog (megabytes):
    # until then Enter only says it is loading, so press it again until the picker is up.
    deadline = time.monotonic() + 30
    while "amazon-bedrock" not in h.picker():
        assert time.monotonic() < deadline, "the provider picker never opened"
        h.key("Enter")
        time.sleep(1.0)
    h.type("amazon-bedrock")
    h.key("Enter")
    h.wait_picker("Sonnet")
    h.type("sonnet-4-5")
    time.sleep(0.4)
    h.key("Enter")
    h.wait_picker("medium")
    h.key("Enter")
    h.wait_text("Publish 1 change")
    h.key("p")
    time.sleep(0.6)
    h.type("Sonnet on the oracle to cut cost")
    h.key("Enter")
    doc = h.wait_until(lambda: (d := h.api_get("/v1/admin/config"))["generation"] == 2 and d, what="generation 2")
    assert doc["models"]["oracle"]["provider"] == "amazon-bedrock"
    assert "sonnet" in doc["models"]["oracle"]["model"]
    assert doc["reason"] == "Sonnet on the oracle to cut cost"
    h.wait_text("Generation 2 rollout")
    no_errors(h)


def test_builtin_provider_key_and_tokens_round_trip(stack):
    h = stack
    h.run("plugin.symphony.admin")
    h.wait_text("Generation")
    h.key("]")
    text = h.wait_text("key set")
    assert "Anthropic" in text and "OpenAI" in text and "Internal vLLM" in text
    revision = h.api_get("/v1/admin/providers")["revision"]
    h.key("down")  # openai: built-in, nothing stored
    h.key("c")
    time.sleep(0.6)
    h.type("sk-real-stack-test")
    h.key("Enter")
    doc = h.wait_until(lambda: (d := h.api_get("/v1/admin/providers"))["revision"] != revision and d, what="provider save")
    assert {"id": "openai", "builtin": True, "credentialConfigured": True} in doc["items"]
    assert all("sk-real-stack-test" not in arg for arg in Path(h.api_log).read_text().split())
    text = h.wait_text("Replace key")
    assert "sk-real-stack-test" not in text
    h.key("]")
    h.wait_text("Team API tokens")
    h.key("n")
    time.sleep(0.6)
    h.type("carol")
    h.key("Enter")
    time.sleep(0.6)
    h.key("Enter")
    time.sleep(0.6)
    h.key("Enter")
    text = h.wait_text("Token created: carol")
    created = next(line for line in text.splitlines() if line.startswith("sym_"))
    tokens = h.api_get("/v1/admin/tokens")["items"]
    carol = next(t for t in tokens if t["name"] == "carol")
    assert carol["role"] == "operator" and carol["revoked_at"] is None
    assert created.startswith(f"sym_{carol['id']}_")
    h.click("[data-id$='.token_dismiss']")
    h.wait_gone("Token created: carol")
    h.key("Up")  # newest live token (carol) is first
    for _ in range(5):
        h.key("Up")
    h.key("x")
    h.wait_text("Revoke the token carol", "sf-dock")
    h.key("Enter")
    h.wait_until(
        lambda: next(t for t in h.api_get("/v1/admin/tokens")["items"] if t["name"] == "carol")["revoked_at"],
        what="revocation",
    )
    no_errors(h)


def test_merger_bypass_reaches_that_repositorys_merger_and_short_reasons_stay_local(stack):
    from symphony.merger.store import MergerStore

    h = stack
    gateway, web = MergerStore(h.redis, NAMESPACE, "acme/gateway"), MergerStore(h.redis, NAMESPACE, "acme/web")
    h.run("plugin.symphony.merger")
    # A table mounts its rows once laid out, a frame after the view switches: wait for a row.
    text = h.wait_text("finishing the merge", timeout=15)
    assert "verifying findings" in text and "finishing the merge" in text and "reviewed, waiting for the train" in text
    assert "acme/gateway" in text and "acme/web" in text
    h.key("b")  # the first item: acme/gateway #790
    time.sleep(0.6)
    h.type("short")
    h.key("Enter")
    time.sleep(1.0)
    assert gateway.pending_actions() == []
    # The field reopened with the short text selected: typing replaces it.
    h.type("Hotfix approved by alice")
    h.key("Enter")
    action = h.wait_until(gateway.pending_actions, what="merger action")[0]
    assert (action["pr"], action["action"]) == (790, "bypass")
    assert action["reason"] == "Hotfix approved by alice"
    assert web.pending_actions() == []
    no_errors(h)


def test_pause_becomes_an_audited_namespace_command(stack):
    h = stack
    h.run("plugin.symphony.dashboard")
    h.wait_text("Needs input")
    h.click("[data-id='main.head.end.pause.b']")
    time.sleep(0.6)
    h.type("Deploying main")
    h.key("Enter")
    audit = h.wait_until(
        lambda: [i for i in h.api_get("/v1/audit")["items"] if "pause" in str(i.get("action"))],
        what="pause audit",
    )
    assert audit[0]["reason"] == "Deploying main"
    h.wait_text("Pause requested from the daemon", "sf-dock")
    no_errors(h)


def test_chat_opens_the_native_acp_chat_with_the_replayed_transcript(stack):
    h = stack
    h.run("plugin.symphony.chat")
    time.sleep(1.5)
    h.type("ENG-142")
    h.key("Enter")

    def chat_text():
        reply = h.ctl("tree", ".tn-pane.on")
        return " ".join(n.get("text", "") for n in reply.get("nodes", []))

    # `tree` caps a node's text; the replayed user turn and the start of the agent's reply fit.
    text = h.wait_until(lambda: "I created" in chat_text() and chat_text(), timeout=30, what="ACP replay")
    assert "Implement the CSV export of the sales report (ENG-142)." in text
    assert "Agent" in text and "Symphony" in text
    assert any("chat" in t["title"] for t in h.ctl("state")["tabs"])
    no_errors(h)


def test_onboarding_logs_in_with_the_real_cli_and_installs():
    with RealHarness(logged_in=False) as h:
        h.run("plugin.symphony.dashboard")
        h.wait_text("You are not signed in to Symphony")
        h.key("Enter")
        h.wait_text("1 · Symphony address")
        h.type(h.url)
        h.key("Tab")
        h.type(BOOTSTRAP)
        assert BOOTSTRAP not in h.text()
        h.key("Enter")
        h.wait_text("Next step: install in Tern")
        token_file = h.home / ".config" / "symphony" / "token"
        assert token_file.exists() and oct(token_file.stat().st_mode & 0o777) == "0o600"
        h.key("Enter")
        h.wait_text("All set in Tern", timeout=60)
        h.key("Enter")  # Open the dashboard: the fresh backend is not set up, so Setup opens for this admin.
        h.leave_setup()
        h.wait_text("Needs input")
        no_errors(h)


def test_session_detail_lists_the_runners_evidence(stack):
    h = stack
    h.run("plugin.symphony.sessions")
    h.wait_text("Needs input")
    h.click("[data-id$='.kiss_142_1']")
    text = h.wait_text("Exported report in the browser")
    assert "Exporter test output" in text
    assert "screenshot" in text and "log" in text
    no_errors(h)


def test_return_to_symphony_releases_a_held_ticket(stack):
    from symphony import state
    from symphony.admin_commands import AdminStore

    h = stack
    h.run("plugin.symphony.tickets")
    text = h.wait_text("Held by a person")
    assert "held by bob" in text
    assert h.api_get("/v1/overview")["counts"]["held"] == 1
    h.click("[data-id$='release_issue=iss-147']")
    h.wait_text("Return ENG-147 to Symphony?", "sf-dock")
    h.key("Enter")
    store = AdminStore(h.redis, NAMESPACE)

    def release_command():
        for command_id in store.pending():
            record = store.get(command_id) or {}
            if record.get("action") == "release" and record.get("target") == {"issueId": "iss-147"}:
                return record
        return None

    record = h.wait_until(release_command, what="release command")
    assert record["target"] == {"issueId": "iss-147"}
    h.wait_text("ENG-147 returned to Symphony", "sf-dock")
    # The daemon applies it by deleting the hold (admin_commands.py); the view follows.
    h.redis.delete(state.hold_key("iss-147"))
    h.wait_gone("Held by a person", timeout=15)
    no_errors(h)


def test_projection_views_render_the_real_routes_as_words(stack):
    h = stack
    h.run("plugin.symphony.repositories")
    text = h.wait_text("acme/billing", timeout=15)
    assert "acme/web" in text and "persisted" not in text
    h.run("plugin.symphony.infrastructure")
    text = h.wait_text("Daemon dispatching", timeout=15)
    assert "Redis" in text and "healthy" in text
    assert "Kubernetes: unavailable" in text
    assert "successfully" not in text
    h.run("plugin.symphony.telemetry")
    text = h.wait_text("ENG-142", timeout=15)
    assert "Agent usage" in text
    h.run("plugin.symphony.learnings")
    h.wait_text("Retrospectives and learning PRs", timeout=15)
    h.run("plugin.symphony.audit")
    text = h.wait_text("created a token", timeout=15)
    assert "published models" in text and "revoked a token" in text
    assert "took over the ticket" in text and "returned the ticket" in text
    assert "saved a provider" in text and "provider." not in text
    for code in ("hold", "release", "teamconfig.publish", "token.create"):
        assert f"\n{code}\n" not in f"\n{text}\n"


def test_dashboard_rate_limits_show_stage_words_not_codes(stack):
    h = stack
    h.run("plugin.symphony.dashboard")
    text = h.wait_text("Rate limits", timeout=15)
    assert "low priority" in text and "wrapping up" in text and "throttled" in text
    assert "18% of quota" in text and "on retry" in text
    for code in ("low_priority", "wrap_up", "usage_limit"):
        assert code not in text
    no_errors(h)


def test_setup_verifies_the_tracker_against_the_linear_mock_and_stores_the_key_write_only(stack):
    h = stack
    h.run("plugin.symphony.setup")
    h.wait_text("Linear API key", timeout=20)
    h.type("lin_api_" + "wrong".ljust(40, "0"))
    h.key("Tab")
    h.type("ENG")
    h.key("Enter")
    # The API asked Linear (the mock) first and refused to store a key Linear rejects.
    h.wait_text("Not saved", timeout=20)
    assert not h.secrets_file.exists() or "LINEAR_API_KEY" not in h.secrets_file.read_text()
    h.key("Tab")  # back to the key field
    h.key("alt+Backspace")
    h.type(LINEAR_MOCK_KEY)
    h.key("Enter")
    text = h.wait_text("Verified and saved", timeout=30)
    assert "Board states" in text and LINEAR_MOCK_KEY not in text
    tracker = next(s for s in h.api_get("/v1/setup")["steps"] if s["id"] == "tracker")
    assert tracker["status"] == "done" and tracker["detail"]["team_key"] == "ENG"
    assert f"LINEAR_API_KEY={LINEAR_MOCK_KEY}" in h.secrets_file.read_text()
    assert oct(h.secrets_file.stat().st_mode & 0o777) == "0o600"
    config = [p for p in (h.home / ".config").rglob("*") if p.is_file()]
    assert all(LINEAR_MOCK_KEY not in p.read_text(errors="replace") for p in config)
    assert LINEAR_MOCK_KEY not in Path(h.api_log).read_text()
    no_errors(h)


def test_admin_mcp_adds_a_server_whose_test_reaches_the_mcp_mock_with_the_stored_header(stack):
    import json
    import urllib.request

    from real_stack import MCP_MOCK_TOKEN

    h = stack
    h.run("plugin.symphony.admin")
    # The panel keeps the Admin section an earlier test left open: step to MCP from there.
    h.wait_text("Team omp")
    deadline = time.monotonic() + 15
    while "No MCP servers yet" not in h.text():
        assert time.monotonic() < deadline, "the MCP section never opened"
        h.key("]")
        time.sleep(0.4)
    h.key("n")
    h.wait_text("New MCP server")
    h.type("mock-tools")
    h.key("Tab")  # transport: Streamable HTTP
    h.key("Tab")
    h.type(h.mcp_url)
    h.key("Tab")
    h.type("researcher")
    h.key("Tab")  # state: enabled
    h.key("Tab")
    h.type("Authorization")
    h.key("Tab")
    h.type(f"Bearer {MCP_MOCK_TOKEN}")
    assert MCP_MOCK_TOKEN not in h.text()
    h.key("Enter")
    doc = h.wait_until(
        lambda: [s for s in h.api_get("/v1/admin/mcp")["servers"] if s["name"] == "mock-tools" and s.get("headers_set")],
        what="stored server with its header",
    )[0]
    assert doc["transport"] == "http" and doc["roles"] == ["researcher"] and doc["enabled"] is True
    assert [n.lower() for n in doc["headers_set"]] == ["authorization"]
    assert MCP_MOCK_TOKEN not in json.dumps(h.api_get("/v1/admin/mcp"))
    h.wait_text("/v1/mcp/mock-tools")
    h.key("Enter")  # Test: initialize, notifications/initialized, tools/list
    text = h.wait_text("✓ Reached", timeout=30)
    assert "echo" in text and "lookup_ticket_context" in text
    with urllib.request.urlopen(f"{h.mcp_mock}/_control/requests", timeout=5) as resp:
        seen = json.load(resp)["requests"]
    assert seen and all(r["has_auth"] for r in seen)
    assert "tools/list" in [r["rpc_method"] for r in seen]
    assert MCP_MOCK_TOKEN not in Path(h.api_log).read_text()
    no_errors(h)
