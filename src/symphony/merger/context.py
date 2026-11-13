"""What the merger needs from the world, in one place: stores, GitHub, git, agents, the
ticket tracker, Slack, the team config, and the wait that wake-ups cut short."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from ..endpoints import github_server_url
from ..teamconfig import TeamConfig
from ..trackers.base import TrackerAdapter
from .agents import AgentRunner
from .git import Mirror
from .github import GitHub
from .policy import PATH, Policy, parse
from .store import MergerStore

log = structlog.get_logger()


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Merger:
    store: MergerStore
    gh: GitHub
    git: Mirror
    agents: AgentRunner
    team: TeamConfig
    repo: str
    states: dict
    tracker: TrackerAdapter | None = None  # WORKFLOW.md's tracker (`edge.make_tracker`)
    notify_post: Callable[..., None] | None = None
    # Waits up to `seconds` for a wake-up (a webhook, a hand-off): never a fixed sleep.
    pause: Callable[[float], None] = time.sleep
    # Wakes the merger's other loops (a hand-off wakes the train at once).
    wake: Callable[[str], None] = field(default=lambda reason: None)
    clock: Callable[[], int] = _now_ms
    # The tracker's required labels (`tracker.required_labels`): a ticket the merger opens
    # carries them, with `repo:<owner/name>`, so the daemon can dispatch it.
    ticket_labels: tuple[str, ...] = ()

    @property
    def main(self) -> str:
        return self.git.main

    def policy(self) -> Policy:
        """The policy on origin/main: a PR can't loosen the rules that review it."""
        return parse(self.git.show(self.git.origin_main, PATH))

    def config(self, stage: str) -> dict:
        """The team config, observed as consumer `merger:<stage>:<repo>` (`review`, `train`)."""
        doc = self.team.get()
        self.team.observe(f"merger:{stage}:{self.store.repo}", doc.get("generation", 0))
        return doc

    def pr_url(self, n: int) -> str:
        return f"{github_server_url()}/{self.repo}/pull/{n}"

    def notify(self, event: str, text: str, *, link: str | None = None, login: str | None = None) -> None:
        if self.notify_post is None:
            return
        try:
            self.notify_post(event, text, link=link, github_login=login)
        except Exception as exc:  # noqa: BLE001 — a channel outage never stops a merge
            log.warning("merger_notify_failed", event=event, error=str(exc)[:300])

    def ticket_of(self, pr: dict) -> str | None:
        """The ticket a PR implements: Symphony's branches and titles carry its identifier."""
        if self.tracker is None:
            return None
        for text in (pr.get("head_ref", ""), pr.get("title", ""), pr.get("body", "")):
            identifier = self.tracker.identifier_in(text or "", repo=self.repo)
            if identifier:
                return identifier
        return None

    def move_ticket(self, identifier: str | None, state_key: str, comment: str = "") -> None:
        """Moves the PR's ticket to `tracker.states[state_key]`, with an optional comment. A
        tracker outage is logged, never fatal: the PR's own state already changed."""
        if not identifier or self.tracker is None:
            return
        try:
            if comment:
                self.tracker.comment(identifier, comment)
            self.tracker.move(identifier, self.states[state_key])
        except Exception as exc:  # noqa: BLE001
            log.warning("merger_ticket_failed", issue=identifier, state=state_key, error=str(exc)[:300])
