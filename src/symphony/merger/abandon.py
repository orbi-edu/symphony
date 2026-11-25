"""A canceled ticket's pull requests: the merger closes them (`tracker.abandon_states`).

During each review pass (`check_abandoned`), before a PR is picked: an open PR whose ticket
(`Merger.ticket_of`) is in an abandon state (`config.is_abandon_state`: canceled, a duplicate,
a canceled-type state; never the done state) leaves the merger and GitHub:

1. Its parked review, its handed-over record still waiting for the train, and its skip are
   dropped; a record already in a train batch is left to the train, which drops a closed PR.
2. One comment says why, with the merger's marker, then the PR is closed (not merged).
3. Its live preview devices go back (`Merger.release_previews`).

The PR is closed once per (ticket, state): `abandoned:<pr>` records it, so a PR a person
reopens is reviewed as usual and not closed again. A ticket back in another state clears the
record, so a later cancel closes the PR again. Ticket states are read at most once a minute
per ticket.
"""

from __future__ import annotations

from ..config import is_abandon_state
from ..prompt import PUBLIC_BRANCH_PREFIX
from . import comments as cm
from .context import Merger, log

STATE_CACHE_MS = 60_000
RECORD_TTL_S = 90 * 86400


def _record_key(m: Merger, n: int) -> str:
    return m.store._k(f"abandoned:{n}")  # noqa: SLF001


def ticket_state(m: Merger, identifier: str) -> dict | None:
    """`tracker.state_of`, cached for a minute per ticket."""
    now = m.clock()
    cached = m._ticket_states.get(identifier)  # noqa: SLF001
    if cached is not None and now - cached[0] < STATE_CACHE_MS:
        return cached[1]
    found = m.tracker.state_of(identifier) if m.tracker is not None else None
    m._ticket_states[identifier] = (now, found)  # noqa: SLF001
    return found


def abandon_state(m: Merger, identifier: str | None) -> str | None:
    """The name of the abandon state the ticket is in, or None."""
    if not identifier:
        return None
    found = ticket_state(m, identifier) or {}
    name = found.get("name")
    if is_abandon_state(m.abandon_states, m.states.get("done", "Done"), name,
                        bool(found.get("canceled"))):
        return name
    return None


def closing_comment(pr: dict, identifier: str, state: str) -> str:
    """The comment that closes the PR, starting with the merger's marker (`comments.OURS`).
    A public repository's PR (its branch carries the ticket's opaque id) never names the
    ticket."""
    public = (pr.get("head_ref") or "").startswith(PUBLIC_BRANCH_PREFIX)
    ticket = "its ticket" if public else identifier
    return (f"**{cm.VERSION}**: Closed by symphony-merger: {ticket} was moved to {state}. "
            "Reopen the PR to work on it again.")


def close_abandoned(m: Merger, pr: dict, identifier: str, state: str) -> None:
    """Drops the PR's merger state, comments once, closes it, releases its previews. A
    retry after a failed close does not comment twice (`commented:` record)."""
    n = pr["number"]
    key, done = _record_key(m, n), f"{identifier}:{state}"
    m.store.unpark(n)
    m.store.drop_skip(n)
    rec = m.store.rec(n)
    if rec and rec.get("state") in ("publishing", "reviewed"):
        m.store.drop_rec(n)  # waiting for the train: out of the queue
    if m.store.client.get(key) != f"commented:{done}":
        m.gh.comment(n, closing_comment(pr, identifier, state))
        m.store.client.set(key, f"commented:{done}", ex=RECORD_TTL_S)
    m.gh.close(n)
    m.store.client.set(key, done, ex=RECORD_TTL_S)
    log.info("merger_pr_abandoned", pr=n, ticket=identifier, state=state)
    if m.release_previews is not None:
        try:
            m.release_previews(n)
        except Exception as exc:  # noqa: BLE001 — the preview sweep releases a closed PR's devices
            log.warning("merger_preview_release_failed", pr=n, error=str(exc)[:300])


def close_abandoned_prs(m: Merger, owner: str) -> list[int]:
    """`check_abandoned` by one review worker at a time."""
    with m.store.lease("abandon", owner, 60_000) as lease:
        if not lease.held:
            return []
        return check_abandoned(m)


def check_abandoned(m: Merger, prs: list[dict] | None = None) -> list[int]:
    """Close each open PR whose ticket was canceled; the numbers closed."""
    if m.tracker is None:
        return []
    closed = []
    for pr in prs if prs is not None else m.gh.list_open():
        n = pr["number"]
        try:
            identifier = m.ticket_of(pr)
            state = abandon_state(m, identifier)
            key = _record_key(m, n)
            if state is None:
                m.store.client.delete(key)  # back in a live state: a later cancel closes again
                continue
            if m.store.client.get(key) == f"{identifier}:{state}":
                continue  # closed once for this; a person reopened it
            close_abandoned(m, pr, identifier, state)
            closed.append(n)
        except Exception as exc:  # noqa: BLE001 — tried again on the next pass
            log.warning("merger_abandon_check_failed", pr=n, error=str(exc)[:300])
    return closed
