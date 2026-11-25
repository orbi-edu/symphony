"""symphony-assistant's entrypoint: idle until configured, heartbeat, graceful stop."""

from __future__ import annotations

import json
import threading

from symphony.config import ConfigHolder, parse_workflow
from symphony.entry import assistant as entry

ENABLED = "---\ntracker: {kind: memory}\nassistant: {enabled: true}\n---\nx"
DISABLED = "---\ntracker: {kind: memory}\n---\nx"


class Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event, **kw):
        self.events.append((event, kw))

    info = warning


def heartbeat(redis_client, ns="default"):
    raw = redis_client.get(entry.heartbeat_key(ns))
    return json.loads(raw) if raw else None


def test_missing_config_names_what_is_absent():
    cfg = parse_workflow(DISABLED)
    assert entry.missing_config(None, {}) == [
        "workflow", "slack_app_token", "slack_bot_token", "discord_bot_token"]
    assert entry.missing_config(cfg, {"SLACK_APP_TOKEN": "xapp-dummy"}) == [
        "slack_bot_token", "discord_bot_token"]
    assert entry.missing_config(cfg, {"DISCORD_BOT_TOKEN": "dummy"}) == []
    assert entry.missing_config(cfg, {"SLACK_APP_TOKEN": "a", "SLACK_BOT_TOKEN": "b"}) == []


def test_it_idles_without_setup(redis_client, monkeypatch):
    monkeypatch.setenv("POD_NAMESPACE", "default")
    stop = threading.Event()
    logger = Logger()
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            stop.set()

    got = entry.wait_until_ready(lambda: None, {}, stop, client=redis_client, pause=pause,
                                 logger=logger)
    assert got is None
    # logged once (not every pass), naming the missing pieces and never a value
    assert logger.events == [("assistant_not_configured", {"missing": [
        "workflow", "slack_app_token", "slack_bot_token", "discord_bot_token"]})]
    beat = heartbeat(redis_client)
    assert beat["state"] == "idle" and beat["slack"] == {"configured": False, "connected": False}
    assert redis_client.ttl(entry.heartbeat_key("default")) <= entry.HEARTBEAT_TTL_S


def test_it_idles_while_disabled_and_starts_once_enabled(redis_client, monkeypatch):
    monkeypatch.setenv("POD_NAMESPACE", "default")
    stop = threading.Event()
    logger = Logger()
    docs = [parse_workflow(DISABLED), parse_workflow(ENABLED)]
    got = entry.wait_until_ready(lambda: docs.pop(0), {"DISCORD_BOT_TOKEN": "dummy"}, stop,
                                 client=redis_client, pause=lambda s: None, logger=logger)
    assert got is not None and got.assistant_enabled
    assert logger.events == [("assistant_disabled", {})]
    assert heartbeat(redis_client)["state"] == "idle"


class FakeTransport:
    def __init__(self, name) -> None:
        self.name = name
        self.connected = False
        self.started = self.stopped = False

    def start(self, on_message, on_action):
        self.started, self.connected = True, True

    def stop(self):
        self.stopped, self.connected = True, False


class FakeEngine:
    def __init__(self) -> None:
        self.swept = 0
        self.stopped = False
        self.souls = None  # no soul reader: the heartbeat's soul is null
        self.agents = None

    def on_message(self, msg):
        pass

    def on_action(self, action):
        pass

    def sweep(self):
        self.swept += 1
        return 0

    def stats(self):
        return {"threads": 2, "active": 1, "queued": 0}

    def stop(self):
        self.stopped = True


def test_running_beats_and_stops_gracefully(redis_client):
    stop = threading.Event()
    engine, slack = FakeEngine(), FakeTransport("slack")
    cfg = parse_workflow(ENABLED)
    seen = []

    def reload():
        if seen:
            beat = heartbeat(redis_client, "ns")
            seen.append(beat)
            stop.set()
        else:
            seen.append(None)
        return cfg

    health: dict = {}
    entry.run(engine, {"slack": slack}, redis_client, "ns", stop, reload=reload, beat_s=0.01,
              health=health)
    beat = seen[1]
    assert beat["state"] == "running" and beat["threads"] == 2
    assert beat["slack"] == {"configured": True, "connected": True}
    assert beat["discord"] == {"configured": False, "connected": False}
    assert slack.started and slack.stopped and engine.stopped and engine.swept >= 1
    assert "at" in health
    assert heartbeat(redis_client, "ns") is None  # removed on a graceful stop


def test_build_wires_the_engine_with_role_tools(redis_client, tmp_path):
    holder = ConfigHolder()
    assert holder.update(ENABLED, {})
    slack = FakeTransport("slack")
    engine, transports, _preview_mgr = entry.build(
        holder, redis_client, "t", {}, transports={"slack": slack},
        workdir=str(tmp_path))
    assert transports == {"slack": slack} and engine.outbounds == {"slack": slack}
    names = {role: [t.name for t in engine.agents.tools_for(role)]
             for role in ("assistant", "researcher", "planner")}
    assert names["assistant"] == names["researcher"] == names["planner"] == [
        "symphony_status", "tracker_search", "open_repository"]
    # The engine's own tools per role: the assistant and the planner propose tickets, the
    # assistant can offer the planner.
    from symphony.assistant.engine import _Thread

    th = _Thread("slack", "C1", "t1", False)
    cfg = holder.config
    engine_tools = {role: {t.name for t in engine._tools(th, cfg, role)}
                    for role in ("assistant", "researcher", "planner")}
    assert {"propose_tickets", "offer_plan"} <= engine_tools["assistant"]
    assert "propose_tickets" in engine_tools["planner"] and "offer_plan" not in engine_tools["planner"]
    assert not {"propose_tickets", "offer_plan"} & engine_tools["researcher"]
    status = engine.agents.tools_for("assistant")[0].answer({}, 0)
    assert json.loads(status)["counts"]["running"] == 0
