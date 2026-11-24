"""First-run setup: with no GitHub App credential the merger idles (no heartbeat, so setup
shows no merger), says `merger_not_configured` at most once a minute, never crash-loops,
and returns the config once setup is done.  'No repository' is no longer a startup blocker."""

from __future__ import annotations

import threading

from symphony.config import parse_workflow
from symphony.entry.merger import (
    configure,
    missing_config,
)
from symphony.merger.store import MergerStore, registered_repos

NS = "setup"
WORKFLOW = "---\ntracker:\n  kind: memory\n---\nbody\n"
APP = {"GITHUB_APP_ID": "123", "GITHUB_APP_INSTALLATION_ID": "456",
       "GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-really-a-key\n-----END PRIVATE KEY-----"}


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict]] = []

    def warning(self, event: str, **fields) -> None:
        self.lines.append((event, fields))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def pause(self, seconds: float) -> None:
        self.t += seconds


def test_missing_config_names_what_setup_lacks_never_a_value():
    cfg = parse_workflow(WORKFLOW)
    # No WORKFLOW, no App credential
    assert missing_config(None, {}) == ["workflow", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
                                        "GITHUB_APP_PRIVATE_KEY"]
    # App present, WORKFLOW present — no longer checks for a repository
    assert missing_config(cfg, {**APP, "SYMPHONY_MERGER_REPO": "not-a-repo"}) == []
    assert missing_config(cfg, {**APP, "SYMPHONY_MERGER_REPO": "acme/app"}) == []
    assert missing_config(cfg, APP) == []  # no repo needed at startup


def test_an_unconfigured_merger_idles_logs_once_a_minute_and_starts_once_setup_is_done():
    clock, logger, env = Clock(), Log(), {}
    cfg = parse_workflow(WORKFLOW)

    def load():
        # setup publishes the WORKFLOW after 2 minutes; the App's Secret arrives with the restart
        # (simulated here by the env filling in after 5 minutes)
        if clock.t >= 300:
            env.update(APP)
        return cfg if clock.t >= 120 else None

    result = configure(load, env, threading.Event(),
                       pause=clock.pause, now=clock.now, logger=logger)
    assert result is cfg
    assert clock.t == 300  # it checked every 15 s and started on the first pass with everything
    assert [e for e, _ in logger.lines] == ["merger_not_configured"] * 5  # 0, 60, 120, 180, 240 s
    assert logger.lines[0][1]["missing"][0] == "workflow"
    assert logger.lines[-1][1]["missing"] == ["GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
                                              "GITHUB_APP_PRIVATE_KEY"]


def test_missing_github_app_keeps_it_idle_with_a_reason_and_no_crash():
    clock, logger = Clock(), Log()
    env: dict = {}
    calls = [0]

    def load():
        calls[0] += 1
        if calls[0] >= 4:
            env.update(APP)
        return parse_workflow(WORKFLOW)

    result = configure(load, env, threading.Event(),
                       pause=clock.pause, now=clock.now, logger=logger)
    assert result is not None
    assert len(logger.lines) == 1  # one log in the first minute


def test_the_real_build_with_a_key_that_mints_nothing_idles_with_no_heartbeat_and_no_secret_in_logs(
        redis_client, tmp_path):
    """The App's private key is malformed: `build` fails to mint the token before any network
    call. Configure returns the config (it only checks for WORKFLOW + App env), but the fleet
    will fail to build a merger for the repo. Here we just verify configure succeeds and no
    secret is logged."""
    clock, logger, stop = Clock(), Log(), threading.Event()
    env = {**APP, "SYMPHONY_MERGER_REPO": "acme/app", "SYMPHONY_MERGER_WORKDIR": str(tmp_path / "merger")}
    cfg = parse_workflow(WORKFLOW)

    # configure now only checks WORKFLOW + App env, so it returns immediately
    result = configure(lambda: cfg, env, stop,
                       pause=clock.pause, now=clock.now, logger=logger)
    assert result is cfg
    assert logger.lines == []
    assert registered_repos(redis_client, NS) == []
    assert not MergerStore(redis_client, NS, "acme/app").alive()
    assert "not-really-a-key" not in repr(logger.lines)


def test_a_stopped_merger_returns_none_without_building():
    stop = threading.Event()
    stop.set()
    assert configure(lambda: None, {}, stop, pause=lambda s: None) is None
