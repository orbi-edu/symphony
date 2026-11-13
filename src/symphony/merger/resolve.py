"""T6: a conflict with main the train handed back (`resolve`), resolved by a developer agent
inside the rebase, checked by a reviewer agent that only the conflicts changed, and pushed with
lease. At most `fix_slots` resolutions run at once; two errors, an unresolved conflict or a
rejected resolution send the PR back to its author (finalized by the train)."""

from __future__ import annotations

from . import agents as ag
from . import comments as cm
from . import prompts
from .context import Merger, log
from .policy import Policy
from .review import ProbeFailed, probe, run_agent

TRIES = 2


def wt_name(n: int) -> str:
    return f"pr-{n}-resolve"


def _back(m: Merger, rec: dict, **over) -> dict:
    r = {**rec, **over, "state": "reviewed"}
    r.pop("conflict", None)
    r.pop("pushing", None)
    m.store.put_rec(r)
    m.wake(f"resolved:{rec['pr']}")
    return r


def _draft(m: Merger, rec: dict, why: str) -> str:
    reason = " ".join(why.split())[:500]
    files = ", ".join(rec["conflict"]["files"])
    text = (f"not merged: it conflicts with main in {files}, and the pipeline couldn't resolve it "
            f"({reason}); rebase it onto main.")
    r = {**rec, "state": "finalizing", "outcome": {"end": "draft", "text": text, "done": []}}
    r.pop("conflict", None)
    r.pop("pushing", None)
    m.store.put_rec(r)
    m.wake(f"unresolved:{rec['pr']}")
    log.info("merger_unresolved", pr=rec["pr"], reason=reason)
    return f"#{rec['pr']} unresolved: {reason}"


def _post(m: Merger, rec: dict, old: str, files: list[str], note: str) -> None:
    try:
        cm.resolved_comment(m, rec, old, files, note)
    except Exception as exc:  # noqa: BLE001 — the push went through; only the explanation is lost
        log.warning("merger_resolved_comment_failed", pr=rec["pr"], error=str(exc)[:300])


def settle_push(m: Merger, rec: dict) -> str | None:
    """A push an earlier try saved as `pushing`, settled by the remote: ours (back to the
    train on it), never landed (intent dropped), or the author's (back, the train drops it)."""
    pushing = rec.get("pushing")
    if not pushing:
        return None
    remote = m.git.ls_remote(rec["branch"])
    if remote == pushing:
        _post(m, _back(m, rec, head_sha=pushing), rec["head_sha"], rec["conflict"]["files"], "")
        return f"#{rec['pr']} resolved: the push of {pushing[:9]} went through"
    if remote != rec["head_sha"]:
        _back(m, rec)
        return f"#{rec['pr']} gone: its branch is at {remote[:9] or 'nothing'}"
    rec.pop("pushing")
    m.store.put_rec(rec)
    return None


def resolve(m: Merger, policy: Policy, team: dict, rec: dict, owner: str) -> str:
    n = rec["pr"]
    conflict = rec["conflict"]
    settled = settle_push(m, rec)
    if settled:
        return settled
    pr = m.gh.pr(n)
    if pr["state"] != "open" or pr["draft"] or pr["base"] != m.main or pr["head_sha"] != rec["head_sha"]:
        _back(m, rec)
        return f"#{n} gone: the merge train drops it"
    if conflict["tries"] >= TRIES:
        return _draft(m, rec, f"its {conflict['tries']} tries failed with an error")
    try:
        m.git.fetch()
        wt = m.git.worktree(wt_name(n), rec["head_sha"])
        m.git.run("switch", "-q", "-C", wt_name(n), cwd=wt)
        main = m.git.main_sha()
        files = m.git.rebase(wt, main, keep=True)
        if not files:
            _back(m, rec)
            return f"#{n} clean: it rebases onto main {main[:9]} without a conflict now"
        probe(m, policy, team, ["developer", "reviewer"])
        base = m.git.merge_base(main, rec["head_sha"])
        m.store.set_reviewing(n, head_sha=rec["head_sha"], worker=owner, phase="resolve", started_at=m.clock())
        run = _Meta(rec)
        dev = run_agent(m, policy, team, "developer", run, n,
                        prompts.resolve_prompt(n, rec["branch"], wt, base, main, files, policy.rules),
                        wt, ag.developer_tools(), ag.EDIT_TOOLS)
        out = dev.last("submit_resolution")
        if dev.status != "completed" or out is None:
            raise ag.AgentFailed(f"developer failed: {dev.error}")
        if not out["resolved"]:
            return _draft(m, rec, out["note"] or "its developer couldn't keep both sides")
        on_branch = m.git.out("branch", "--show-current", cwd=wt) == wt_name(n)
        if not (m.git.rebase_finished(wt) and on_branch
                and m.git.ok("merge-base", "--is-ancestor", main, "HEAD", cwd=wt)):
            return _draft(m, rec, "the rebase isn't complete after its developer")
        head = m.git.rev(wt_name(n), cwd=wt)
        compare = {
            "range_diff": m.git.run("range-diff", f"{base}..{rec['head_sha']}", f"{main}..{head}", check=False).stdout,
            "files": {f: {"pr_change": m.git.run("diff", rec["head_sha"], head, "--", f, check=False).stdout,
                          "main_change": m.git.run("diff", base, main, "--", f, check=False).stdout}
                      for f in files},
        }
        rev = run_agent(m, policy, team, "reviewer", run, n,
                        prompts.resolve_review_prompt(n, wt, files, base, main, rec["head_sha"], head,
                                                      {**out, "comparison": compare}, policy.rules),
                        wt, ag.resolve_review_tools())
        verdict = rev.last("submit_review")
        if rev.status != "completed" or verdict is None:
            raise ag.AgentFailed(f"resolution reviewer failed: {rev.error}")
        if not verdict["approved"]:
            return _draft(m, rec, f"its reviewer didn't approve the resolution: {verdict['issues']}")
        m.store.put_rec({**rec, "pushing": head})
        try:
            m.gh.add_labels(n, [cm.TRAIN_CI])
        except Exception:  # noqa: BLE001 — only the runner choice
            pass
        m.git.push_lease(wt, wt_name(n), rec["branch"], rec["head_sha"])
        _post(m, _back(m, rec, head_sha=head), rec["head_sha"], files, out["note"])
        return f"#{n} resolved: rebased onto main as {head[:9]}"
    except ProbeFailed:
        # The tools are down, not the PR: the worker backs off, and no try is counted.
        raise
    except Exception as exc:
        now = m.store.rec(n)
        if not now or now["state"] != "resolve":
            raise
        why = (str(exc).strip().split("\n") or [""])[-1][:300]
        tries = now["conflict"]["tries"] + 1
        counted = {**now, "conflict": {**now["conflict"], "tries": tries}}
        log.warning("merger_resolve_failed", pr=n, tries=tries, error=why)
        settled = settle_push(m, counted)
        if settled:
            return settled
        if tries >= TRIES:
            return _draft(m, counted, why)
        m.store.put_rec(counted)
        return f"#{n} error: {why}"
    finally:
        m.git.remove_worktree(wt_name(n))
        m.store.clear_reviewing(n)


class _Meta:
    """What a resolution's agent sessions show."""

    def __init__(self, rec: dict) -> None:
        self.issue = rec.get("issue")
        self.title = rec.get("title", "")
        self.branch = rec["branch"]


def resolve_next(m: Merger, policy: Policy, team: dict, owner: str) -> str | None:
    """The oldest conflict waiting, resolved under the PR's claim and a fix slot; None when
    there is none, every one is held, or every slot is busy."""
    waiting = sorted((r for r in m.store.recs().values() if r["state"] == "resolve"),
                     key=lambda r: r["reviewed_at"])
    if not waiting:
        return None
    for r in waiting:
        if not m.store.claim(r["pr"], owner):
            continue
        try:
            for k in range(1, policy.fix_slots + 1):
                # The lease's heartbeat keeps it for the resolution; a dead worker's slot frees in a minute.
                with m.store.lease(f"fixslot:{k}", owner, 60_000) as slot:
                    if not slot.held:
                        continue
                    now = m.store.rec(r["pr"])
                    if not now or now["state"] != "resolve":
                        break
                    return resolve(m, policy, team, now, owner)
            else:
                return None
        finally:
            m.store.unclaim(r["pr"], owner)
    return None
