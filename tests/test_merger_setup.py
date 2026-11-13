"""First-run setup: with no GitHub App credential or no repository the merger idles (no
heartbeat, so setup shows no merger), says `merger_not_configured` at most once a minute,
never crash-loops, and starts as usual once setup is done."""

from __future__ import annotations

import threading

from symphony.config import parse_workflow
from symphony.entry.merger import (
    NOT_CONFIGURED_POLL_S,
    NotConfigured,
    build,
    configure,
    missing_config,
)
from symphony.ghapp import NotAuthorizedForRepo
from symphony.merger.git import GitError
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
    assert missing_config(None, {}) == ["workflow", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
                                        "GITHUB_APP_PRIVATE_KEY", "repository"]
    assert missing_config(cfg, {**APP, "SYMPHONY_MERGER_REPO": "not-a-repo"}) == ["repository"]
    assert missing_config(cfg, {**APP, "SYMPHONY_MERGER_REPO": "acme/app"}) == []
    with_default = parse_workflow(WORKFLOW.replace("---\nbody", "kubernetes:\n  default_repo: acme/app\n---\nbody"))
    assert missing_config(with_default, {**APP, "SYMPHONY_MERGER_REPO": ""}) == []  # WORKFLOW's default repo


def test_an_unconfigured_merger_idles_logs_once_a_minute_and_starts_once_setup_is_done():
    clock, logger, env = Clock(), Log(), {}
    cfg = parse_workflow(WORKFLOW)
    built = []

    def load():
        # setup publishes the WORKFLOW after 2 minutes; the App's Secret arrives with the restart
        # (simulated here by the env filling in after 5 minutes)
        if clock.t >= 300:
            env.update(APP, SYMPHONY_MERGER_REPO="acme/app")
        return cfg if clock.t >= 120 else None

    m = configure(load, env, threading.Event(), build_merger=lambda c: built.append(c) or "merger",
                  pause=clock.pause, now=clock.now, logger=logger)
    assert m == "merger" and built == [cfg]
    assert clock.t == 300  # it checked every 15 s and started on the first pass with everything
    assert [e for e, _ in logger.lines] == ["merger_not_configured"] * 5  # 0, 60, 120, 180, 240 s
    assert logger.lines[0][1]["missing"][0] == "workflow"
    assert logger.lines[-1][1]["missing"] == ["GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
                                              "GITHUB_APP_PRIVATE_KEY", "repository"]


def test_a_credential_that_fails_keeps_it_idle_with_a_reason_and_no_crash():
    clock, logger = Clock(), Log()
    env = {**APP, "SYMPHONY_MERGER_REPO": "acme/app"}
    failures = [NotAuthorizedForRepo("acme/app"), GitError(["clone"], 128, "Repository not found"),
                NotConfigured(["repository"])]

    def build_merger(cfg):
        if failures:
            raise failures.pop(0)
        return "merger"

    m = configure(lambda: parse_workflow(WORKFLOW), env, threading.Event(), build_merger=build_merger,
                  pause=lambda s: clock.pause(s * 4), now=clock.now, logger=logger)
    assert m == "merger"
    assert [f["missing"] for _, f in logger.lines] == [["repository_access"], ["repository_access"],
                                                      ["repository"]]


def test_the_real_build_with_a_key_that_mints_nothing_idles_with_no_heartbeat_and_no_secret_in_logs(
        redis_client, tmp_path):
    """The App's private key is malformed: `build` fails to mint the token before any network
    call. The merger idles as `github_app`, registers nothing (setup shows no merger for the
    repository) and never logs the key."""
    clock, logger, stop = Clock(), Log(), threading.Event()
    env = {**APP, "SYMPHONY_MERGER_REPO": "acme/app", "SYMPHONY_MERGER_WORKDIR": str(tmp_path / "merger")}
    calls = []

    def build_merger(cfg):
        calls.append(1)
        if len(calls) == 3:
            stop.set()  # the operator stops the pod before setup fixes the key
        return build(redis_client, NS, cfg, env, notify_post=lambda *a, **k: None)

    assert configure(lambda: parse_workflow(WORKFLOW), env, stop, build_merger=build_merger,
                     pause=clock.pause, now=clock.now, logger=logger) is None
    assert len(calls) == 3 and clock.t == 3 * NOT_CONFIGURED_POLL_S
    assert logger.lines == [("merger_not_configured", {"missing": ["github_app"]})]  # once in the minute
    assert registered_repos(redis_client, NS) == []
    assert not MergerStore(redis_client, NS, "acme/app").alive()
    assert "not-really-a-key" not in repr(logger.lines)


def test_a_stopped_merger_returns_none_without_building():
    stop = threading.Event()
    stop.set()
    assert configure(lambda: None, {}, stop, build_merger=lambda c: 1 / 0, pause=lambda s: None) is None
