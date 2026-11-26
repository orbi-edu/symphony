"""Renders the Symphony plugin's documentation screenshots (light and dark) headlessly.

    uv run python tests/tern/scenarios.py [--out docs/tern]

Each scenario drives a fresh `tern serve` (see harness.py) against the fake API and the stub CLI,
then fails if Tern logged a plugin error. The PNGs land in `--out` as `<name>-light.png` and
`<name>-dark.png`; docs/tern.md embeds them.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from harness import TOKEN, Harness  # noqa: E402

POLL = 2.2  # one poll period (2 s) plus margin


def settle(h: Harness, needle: str) -> None:
    h.wait_text(needle)
    time.sleep(0.3)


def charts(out: Path) -> None:
    """The Dashboard and Telemetry, tall enough to show their charts."""
    with Harness(size="1440 1900") as h:
        h.run("plugin.symphony.dashboard")
        settle(h, "Needs input")
        settle(h, "Estimated cost per day")
        h.shots("dashboard", out)
        h.run("plugin.symphony.telemetry")
        settle(h, "Activity by weekday")
        h.shots("telemetry", out)
        check(h)


def views(out: Path) -> None:
    with Harness() as h:
        h.run("plugin.symphony.dashboard")
        settle(h, "Needs input")
        h.key("Tab")
        settle(h, "from the one that needs you")
        h.key("down")
        h.key("down")  # the Slack assistant thread, with its role and place
        settle(h, "Asked by")
        h.shots("sessions", out)
        h.key("Tab")
        settle(h, "On the board")
        h.shots("tickets", out)
        h.key("Tab")
        settle(h, "Queue and parked")
        h.key("down")
        h.shots("merger", out)
        for view, needle in (("repositories", "Repositories Symphony reaches"),
                             ("infrastructure", "Daemon dispatching"),
                             ("learnings", "Retrospectives"), ("audit", "Everything people")):
            h.run(f"plugin.symphony.{view}")
            settle(h, needle)
            h.shots(view, out)
        h.run("plugin.symphony.chat")
        time.sleep(1.2)
        h.shots("chat", out)
        h.key("Escape")
        check(h)


def real(out: Path) -> None:
    """The real stack (real_stack.py): API, Redis stores, CLI and the `symphony acp` chat."""
    from real_stack import RealHarness

    from symphony.sessions import SessionStore

    with RealHarness() as h:
        h.run("plugin.symphony.dashboard")
        h.leave_setup()  # the real backend starts unconfigured, so Setup opens for this admin first
        settle(h, "Needs input")
        h.shots("real-dashboard", out)
        h.run("plugin.symphony.merger")
        settle(h, "Queue and parked")
        h.shots("real-merger", out)
        h.run("plugin.symphony.chat")
        time.sleep(1.5)
        h.type("ENG-142")
        h.key("Enter")
        time.sleep(8)
        # A live omp frame after the replay: the chat shows it without a reload.
        SessionStore(h.redis, "tern-real").append_event(h.ids["running"], "message_update", {
            "type": "message_update", "messageId": "live-1", "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "delta": "Live now: I covered time zones and the tests pass."},
        })
        time.sleep(3)
        h.shots("real-chat", out)
        check(h)


def admin(out: Path) -> None:
    with Harness() as h:
        h.run("plugin.symphony.admin")
        settle(h, "Generation 3 rollout")
        h.key("down")
        h.key("down")
        h.key("Enter")
        time.sleep(1.0)
        h.type("bedrock")
        h.key("Enter")
        time.sleep(1.2)
        h.shots("admin-pick-model", out)
        h.type("sonnet")
        h.key("Enter")
        time.sleep(0.6)
        h.key("Enter")
        settle(h, "Publish 1 change")
        h.shots("admin-models", out)
        h.key("]")
        settle(h, "key set")
        h.shots("admin-providers", out)
        h.key("]")
        settle(h, "created by")
        h.key("n")
        time.sleep(0.5)
        h.type("carol")
        h.key("Enter")
        time.sleep(0.5)
        h.key("Enter")
        time.sleep(0.5)
        h.key("Enter")
        settle(h, "Token created: carol")
        h.shots("admin-tokens", out)
        check(h)
    with Harness() as h:
        # A fresh panel, so the token's flash (it belongs to Tokens) is not on the MCP screen.
        h.run("plugin.symphony.admin")
        settle(h, "Generation 3 rollout")
        for _ in range(3):
            h.key("]")
        settle(h, "MCP servers give agents more tools")
        h.key("Enter")  # Test the selected server
        settle(h, "✓ Reached")
        h.shots("admin-mcp", out)
        h.key("]")
        settle(h, "acme/agent-skills")
        h.shots("admin-skills", out)
        h.key("]")
        settle(h, "omp version")
        h.key("s")
        settle(h, "Applied to your omp")
        text = h.text()
        assert "Default model" in text and "the team uses 18.20.2" in text, text
        h.shots("admin-omp", out)
        check(h)


def assistant(out: Path) -> None:
    """Admin › Assistant: the soul in use, people with memory, one person's items."""
    with Harness() as h:
        h.run("plugin.symphony.admin")
        settle(h, "Generation 3 rollout")
        for _ in range(6):
            h.key("]")
        settle(h, "deploys on Fridays after the release review")
        h.shots("admin-assistant", out)
        h.key("x")
        h.wait_text("Wipe the memory of alice?", "sf-dock")
        time.sleep(0.3)
        h.shots("admin-assistant-wipe", out)
        check(h)


def onboarding(out: Path) -> None:
    with Harness(logged_in=False) as h:
        h.run("plugin.symphony.signin")
        settle(h, "1 · Symphony address")
        h.type(h.url)
        h.key("Tab")
        h.type(TOKEN)
        time.sleep(0.4)
        h.shots("signin", out)
        h.key("Enter")
        settle(h, "Next step")
        h.key("Enter")
        settle(h, "All set in Tern")
        h.shots("signin-ready", out)
        check(h)


def states(out: Path) -> None:
    with Harness(logged_in=False) as h:
        h.run("plugin.symphony.dashboard")
        settle(h, "You are not signed in")
        h.shots("disconnected", out)
        check(h)
    with Harness() as h:
        h.run("plugin.symphony.dashboard")
        settle(h, "Needs input")
        assert h.api is not None
        h.api.terminate()
        h.api.wait(5)
        h.wait_text("No connection", "sf-dock")
        h.shots("offline", out)
        check(h)


def setup(out: Path) -> None:
    """The first-run setup on a backend that is not set up (setup contract §4)."""
    with Harness(state="unconfigured") as h:
        h.run("plugin.symphony.dashboard")
        settle(h, "Linear API key")
        h.type("lin_api_8f2c0d1e9b7a4c35")
        h.key("Tab")
        h.type("OPS")
        h.key("Enter")
        settle(h, "Linear has no team")
        h.shots("setup-error", out)
        h.key("alt+Backspace")
        h.type("ENG")
        h.key("Enter")
        settle(h, "Verified and saved")
        h.shots("setup-tracker", out)
        h.click("[data-id$='.s_github_app']")
        settle(h, "Installation ID")
        h.type("123456")
        h.key("Tab")
        h.type("45678901")
        h.key("Tab")
        h.type("-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEA0demo -----END RSA PRIVATE KEY-----")
        h.key("Enter")
        time.sleep(0.5)
        h.shots("setup-verifying", out)
        settle(h, "3 found")
        time.sleep(0.6)  # the check mark's animation ends
        h.shots("setup-github-app", out)
        h.click("[data-id$='.s_repositories']")
        settle(h, "Tickets without a repo: label go here.")
        h.type("acme/web")
        h.key("Enter")
        settle(h, "Symphony is ready")
        time.sleep(0.6)
        h.shots("setup-ready", out)
        h.click("[data-id$='.s_webhooks']")
        settle(h, "Create webhook secrets")
        h.key("Enter")
        settle(h, "The secrets are not shown again")
        h.shots("setup-webhooks", out)
        check(h)
    with Harness() as h:
        # A team that is set up: the optional Assistant step is the next one.
        h.run("plugin.symphony.setup")
        settle(h, "Slack app token")
        h.type("C0123ABCD")
        h.key("Tab")
        h.type("xapp-1-A0DEMO-1234-5678abcdef")
        h.key("Enter")
        settle(h, "Verified and saved")
        time.sleep(0.6)  # the check mark's animation ends
        h.shots("setup-assistant", out)
        check(h)
    with Harness(role="viewer", state="unconfigured") as h:
        h.run("plugin.symphony.dashboard")
        settle(h, "Ask an admin to finish setup.")
        h.shots("setup-banner", out)
        check(h)


def seiji(out: Path) -> None:
    """A team with a Seiji server: a session that holds an iPhone, and the server's test."""
    with Harness(state="seiji") as h:
        h.run("plugin.symphony.sessions")
        settle(h, "ENG-163")
        h.click("[data-id$='.kiss_163_1']")
        settle(h, "Watch device")
        h.shots("sessions-seiji", out)
        h.key("shift+w")
        h.wait_text("Control iphone-16 of ENG-163?", "sf-dock")
        time.sleep(0.3)
        h.shots("sessions-seiji-control", out)
        h.key("Escape")
        h.run("plugin.symphony.admin")
        settle(h, "Generation 3 rollout")
        for _ in range(3):
            h.key("]")
        settle(h, "MCP servers give agents more tools")
        h.key("down")
        h.key("down")  # seiji
        h.key("Enter")
        settle(h, "Signed in as")
        h.shots("admin-mcp-seiji", out)
        check(h)


def check(h: Harness) -> None:
    errors = h.plugin_errors()
    if errors:
        raise SystemExit("plugin errors:\n" + "\n".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=HERE.parent.parent / "docs" / "tern")
    parser.add_argument("only", nargs="*", help="scenario names (default: all)")
    args = parser.parse_args()
    scenarios = {"views": views, "charts": charts, "admin": admin, "assistant": assistant, "onboarding": onboarding,
                 "states": states, "setup": setup, "seiji": seiji, "real": real}
    for name in args.only or list(scenarios):
        scenarios[name](args.out)
        print(f"ok {name}")


if __name__ == "__main__":
    main()
